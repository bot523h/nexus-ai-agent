"""Shadow Preview — deterministic visual keyframe strip from scope pixels (NAGAR IBPC).

Produces a :class:`~nexus_ai_agent.nagar.ibpc.models.ShadowPreviewManifest`: a
frozen, digest-bound set of JPEG keyframes extracted from the *actual* source
video inside the committed scope.  Every frame is captured by calling ffmpeg as
a subprocess with explicit list arguments (no ``shell=True``); the frame bytes
are sha256-digested before base64-encoding, so the manifest is fully
self-verifying (decode the base64, hash it, compare to ``frame_digest``).

Contract (Spec §2):

* Sampling is **half-open** ``[start, start + duration)``: no keyframe is ever
  taken at or beyond the scope end.
* ``max_keyframes`` ∈ [1, 16]; outside → ``ValueError``.
* ``scope_duration_s <= 0`` → :class:`ShadowRenderError` ``"empty_scope"``.
* Timeout is enforced per frame (30 s) **and** in total (120 s); a hit raises
  :class:`ShadowRenderError` ``"timeout"``.
* A non-zero ffmpeg exit is ``"ffmpeg_failed"`` even when it wrote bytes; output
  that is not a complete JPEG (SOI … EOI) is ``"invalid_jpeg"``. Partial output is
  never a frame.
* ``verify_shadow_manifest`` recomputes every digest from the carried bytes and
  checks timestamps; ``build_shadow_preview`` runs it on its own result.
* Frame count is ``min(max_keyframes, scope length in us)`` so timestamps are
  strictly increasing on any scope.
* ``composite_digest = "sha256:" + sha256(b"".join(sorted(fd.encode() for fd in
  frame_digests))).hexdigest()`` — order-independent over the scope, and it is
  what an approval binds.  ``ShadowPreviewManifest.shadow_digest`` is a
  *different* concept (digest of the whole manifest).

No LLM, no external API, no new pip dependencies, no disk writes.  This module
imports from ``models``, ``canonical`` and ``creative.slideshow.ffmpeg``; it
explicitly MUST NOT import from ``service``, ``session``, ``approval``,
``footprint`` or ``signature``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import subprocess
import time
from pathlib import Path

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ShadowKeyframe,
    ShadowPreviewManifest,
    VideoTimeScope,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_KEYFRAMES = 1
_MAX_KEYFRAMES = 16
_DEFAULT_MAX_KEYFRAMES = 8
_DEFAULT_JPEG_QUALITY = 23  # ffmpeg -q:v (2 = best, 31 = worst)
_DEFAULT_TARGET_HEIGHT = 480  # px (480p per Spec §2.3)
_PER_FRAME_TIMEOUT_S = 30
_TOTAL_TIMEOUT_S = 120
_DIGEST_PREFIX = "sha256:"
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ShadowRenderError(RuntimeError):
    """Raised when a shadow preview cannot be captured.

    ``reason`` is a machine-readable slug, e.g. ``"empty_scope"``,
    ``"empty_frame"``, ``"ffmpeg_not_found"``, ``"timeout"``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        msg = reason if not detail else f"{reason}: {detail}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _capture_frame(
    source_path: Path,
    timestamp_us: int,
    *,
    ffmpeg_bin: str,
    jpeg_quality: int,
    target_height: int,
    timeout_s: float,
) -> bytes:
    """Extract one JPEG frame from *source_path* at *timestamp_us* via ffmpeg.

    Returns the raw JPEG bytes.  Raises :class:`ShadowRenderError` on an empty
    result or a timeout.  The call always uses a list of arguments, never
    ``shell=True``, and reads the frame from stdout (no disk writes).
    """
    ts_s = timestamp_us / 1_000_000.0
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-ss",
        f"{ts_s:.6f}",
        "-i",
        str(source_path),
        "-vframes",
        "1",
        "-vf",
        f"scale=-2:{target_height}",
        "-q:v",
        str(jpeg_quality),
        "-f",
        "image2",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(0.001, timeout_s),
        )
    except subprocess.TimeoutExpired as exc:
        raise ShadowRenderError(
            "timeout", f"frame capture exceeded {timeout_s:.1f}s at t={ts_s:.3f}s"
        ) from exc
    if not result.stdout:
        raise ShadowRenderError("empty_frame", f"at t={ts_s:.3f}s (returncode={result.returncode})")
    if result.returncode != 0:
        # Bytes from a failed run are never a frame, even when they look like one.
        raise ShadowRenderError("ffmpeg_failed", f"returncode={result.returncode} at t={ts_s:.3f}s")
    if not _is_jpeg(result.stdout):
        raise ShadowRenderError(
            "invalid_jpeg", f"ffmpeg output at t={ts_s:.3f}s is not a complete JPEG"
        )
    return result.stdout


def _is_jpeg(data: bytes) -> bool:
    """A complete-looking JPEG envelope: SOI marker at the start, EOI at the end."""
    return len(data) >= 4 and data.startswith(_JPEG_SOI) and data.endswith(_JPEG_EOI)


def _frame_to_keyframe(jpeg_bytes: bytes, timestamp_us: int) -> ShadowKeyframe:
    """Wrap raw JPEG bytes into a :class:`ShadowKeyframe`."""
    digest = _DIGEST_PREFIX + hashlib.sha256(jpeg_bytes).hexdigest()
    return ShadowKeyframe(
        timestamp_us=timestamp_us,
        jpeg_b64=base64.b64encode(jpeg_bytes).decode("ascii"),
        frame_digest=digest,
    )


def _compute_composite_digest(keyframes: tuple[ShadowKeyframe, ...]) -> str:
    """Spec §2.4 step 5 — order-independent digest of the frame digests."""
    return (
        _DIGEST_PREFIX
        + hashlib.sha256(
            b"".join(sorted(fd.encode() for fd in (kf.frame_digest for kf in keyframes)))
        ).hexdigest()
    )


def _sample_timestamps(scope_start_us: int, scope_end_us: int, n: int) -> list[int]:
    """Return *n* evenly-spaced timestamps in the half-open ``[start, end)``.

    ``timestamps[i] = start + floor(i * duration / n)`` for ``i`` in
    ``[0, n)`` — every sample is strictly inside the scope (``< end``).
    """
    duration_us = scope_end_us - scope_start_us
    return [scope_start_us + (i * duration_us) // n for i in range(n)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_shadow_manifest(manifest: ShadowPreviewManifest) -> None:
    """Recompute every digest from the carried bytes; raise on the first mismatch.

    The consumer-side half of the self-verifying contract. It needs no ffmpeg
    and no disk, only the manifest: each keyframe must decode to a complete
    JPEG whose sha256 equals its ``frame_digest``; timestamps must lie in the
    half-open scope and strictly increase; and ``composite_digest`` must equal
    the Spec §2.4 formula over those frame digests.

    Raises
    ------
    ShadowRenderError
        With a machine-readable ``reason`` naming the first violated check.
    """
    if not manifest.keyframes:
        raise ShadowRenderError("empty_manifest", "no keyframes")
    if manifest.scope_end_us <= manifest.scope_start_us:
        raise ShadowRenderError(
            "empty_scope", f"{manifest.scope_start_us}..{manifest.scope_end_us}"
        )
    previous_ts: int | None = None
    for index, kf in enumerate(manifest.keyframes):
        try:
            raw = base64.b64decode(kf.jpeg_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ShadowRenderError("tampered_frame", f"keyframe {index}: invalid base64") from exc
        if not _is_jpeg(raw):
            raise ShadowRenderError("invalid_jpeg", f"keyframe {index}: not a complete JPEG")
        if _DIGEST_PREFIX + hashlib.sha256(raw).hexdigest() != kf.frame_digest:
            raise ShadowRenderError("frame_digest_mismatch", f"keyframe {index}")
        if not (manifest.scope_start_us <= kf.timestamp_us < manifest.scope_end_us):
            raise ShadowRenderError(
                "timestamp_out_of_scope", f"keyframe {index} at {kf.timestamp_us}us"
            )
        if previous_ts is not None and kf.timestamp_us <= previous_ts:
            raise ShadowRenderError("timestamps_not_increasing", f"keyframe {index}")
        previous_ts = kf.timestamp_us
    if _compute_composite_digest(manifest.keyframes) != manifest.composite_digest:
        raise ShadowRenderError(
            "composite_digest_mismatch", "composite does not match the keyframe digests"
        )


def build_shadow_preview(
    source_path: Path,
    scope: VideoTimeScope,
    plan_digest: str,
    *,
    ffmpeg_bin: str | None = None,
    max_keyframes: int = _DEFAULT_MAX_KEYFRAMES,
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    target_height: int = _DEFAULT_TARGET_HEIGHT,
    source_duration_us: int = 0,
) -> ShadowPreviewManifest:
    """Capture a keyframe strip for *scope* from *source_path*.

    Parameters
    ----------
    source_path:
        Path to the source video file.
    scope:
        The :class:`VideoTimeScope` whose boundaries define the preview window.
    plan_digest:
        Digest of the compiled plan this preview belongs to (``sha256:hex64``).
    ffmpeg_bin:
        Explicit path to the ffmpeg binary; resolved automatically when ``None``.
    max_keyframes:
        Number of JPEG frames to extract (1–16, default 8).
    jpeg_quality:
        ffmpeg ``-q:v`` value (2 = best, 31 = worst, default 23).
    target_height:
        Target frame height in pixels (default 480).
    source_duration_us:
        Total duration of the source asset in microseconds.  Kept as an
        additive keyword-only argument (default 0) so the manifest can record
        source metadata without changing the Spec §2.4 signature.

    Returns
    -------
    ShadowPreviewManifest
        Frozen, digest-bound manifest of all extracted keyframes.

    Raises
    ------
    ValueError
        If *max_keyframes* is outside 1–16.
    ShadowRenderError
        On an empty scope, empty frame output, ffmpeg binary not found, or a
        per-frame / total timeout.
    """
    if not (_MIN_KEYFRAMES <= max_keyframes <= _MAX_KEYFRAMES):
        raise ValueError(
            f"max_keyframes must be {_MIN_KEYFRAMES}–{_MAX_KEYFRAMES}, got {max_keyframes}"
        )

    if scope.end_us <= scope.start_us:
        raise ShadowRenderError("empty_scope", f"{scope.start_us}..{scope.end_us}")
    if source_duration_us > 0 and scope.end_us > source_duration_us:
        raise ShadowRenderError(
            "scope_outside_source", f"scope ends at {scope.end_us}us, source is {source_duration_us}us"
        )

    try:
        resolved_bin = resolve_ffmpeg_bin(ffmpeg_bin)
    except (FfmpegUnavailableError, FileNotFoundError, OSError) as exc:
        # FfmpegUnavailableError is a RuntimeError, not an OSError — it must be
        # translated here so no raw resolver error crosses the public boundary.
        raise ShadowRenderError("ffmpeg_not_found", "ffmpeg binary could not be resolved") from exc

    # Never more frames than microseconds: with fewer, timestamps would collide
    # and the strip would stop being strictly ordered.
    frame_count = min(max_keyframes, scope.end_us - scope.start_us)
    timestamps = _sample_timestamps(scope.start_us, scope.end_us, frame_count)

    deadline = time.monotonic() + _TOTAL_TIMEOUT_S
    keyframes: list[ShadowKeyframe] = []
    for ts_us in timestamps:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ShadowRenderError("timeout", f"total render exceeded {_TOTAL_TIMEOUT_S}s")
        jpeg_bytes = _capture_frame(
            source_path,
            ts_us,
            ffmpeg_bin=resolved_bin,
            jpeg_quality=jpeg_quality,
            target_height=target_height,
            timeout_s=min(_PER_FRAME_TIMEOUT_S, remaining),
        )
        keyframes.append(_frame_to_keyframe(jpeg_bytes, ts_us))

    kf_tuple = tuple(keyframes)
    manifest = ShadowPreviewManifest(
        plan_digest=plan_digest,
        scope_digest=scope.scope_digest,
        source_duration_us=source_duration_us,
        scope_start_us=scope.start_us,
        scope_end_us=scope.end_us,
        keyframes=kf_tuple,
        composite_digest=_compute_composite_digest(kf_tuple),
    )
    # A manifest that fails its own verifier is never returned.
    verify_shadow_manifest(manifest)
    return manifest


__all__ = [
    "ShadowKeyframe",
    "ShadowPreviewManifest",
    "ShadowRenderError",
    "build_shadow_preview",
    "verify_shadow_manifest",
]
