"""Shadow Preview — deterministic visual keyframe strip from scope pixels (NAGAR IBPC).

Produces a :class:`ShadowPreviewManifest`: a frozen, digest-bound set of JPEG
keyframes extracted from the *actual* source video at the committed scope
boundaries.  Every frame is captured by calling ffmpeg as a subprocess with
explicit list arguments (no ``shell=True``) and the frame bytes are
sha256-digested before base64-encoding so the manifest is fully self-verifying.

Design constraints:
- No LLM, no external API, no new pip dependencies.
- Pure Python CIE-Lab conversion is NOT needed here (that lives in signature.py).
- ffmpeg is invoked via ``subprocess.run`` with a list of args, never shell=True.
- All models: ``ConfigDict(extra="forbid", frozen=True)``.
- ``composite_digest`` is derived from the ordered frame digests via ``canonical.digest_of``.
- ``shadow_digest`` is a ``@property`` derived from the entire manifest (plan + scope
  + composite_digest) — it is NOT stored as a field to stay frozen/pure.

This module imports from ``models``, ``canonical``, and
``nexus_ai_agent.creative.slideshow.ffmpeg`` (for ``resolve_ffmpeg_bin``).
It explicitly MUST NOT import from ``service``, ``session``, ``approval``,
``footprint``, or ``signature``.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_KEYFRAMES = 1
_MAX_KEYFRAMES = 16
_DEFAULT_MAX_KEYFRAMES = 5
_DEFAULT_JPEG_QUALITY = 4   # vframes vf scale quality: 2=high, 31=low; 4 is good
_DEFAULT_TARGET_HEIGHT = 120  # px — small enough for fast transfers
_DIGEST_PREFIX = "sha256:"
_HEX64_PATTERN = r"^sha256:[0-9a-f]{64}$"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ShadowRenderError(RuntimeError):
    """Raised when a shadow preview frame cannot be captured.

    ``reason`` is a machine-readable slug, e.g. ``"empty_frame"``,
    ``"ffmpeg_not_found"``, ``"ffmpeg_error"``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        msg = reason if not detail else f"{reason}: {detail}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ShadowKeyframe(BaseModel):
    """One JPEG keyframe extracted from the source at ``timestamp_us``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp_us: int
    jpeg_b64: str = Field(description="Base64-encoded JPEG bytes of the frame.")
    frame_digest: Annotated[str, Field(pattern=_HEX64_PATTERN)]


class ShadowPreviewManifest(BaseModel):
    """Immutable manifest of all keyframes for one scope preview.

    ``shadow_digest`` is a computed property (not a stored field) derived from
    ``plan_digest``, ``scope_digest``, and ``composite_digest``; it
    changes whenever any of those change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_digest: Annotated[str, Field(pattern=_HEX64_PATTERN)]
    scope_digest: Annotated[str, Field(pattern=_HEX64_PATTERN)]
    source_duration_us: int
    scope_start_us: int
    scope_end_us: int
    keyframes: tuple[ShadowKeyframe, ...]
    composite_digest: Annotated[str, Field(pattern=_HEX64_PATTERN)]

    @property
    def shadow_digest(self) -> str:
        """Digest of the entire manifest (plan + scope + composite_digest)."""
        return digest_of(
            {
                "plan_digest": self.plan_digest,
                "scope_digest": self.scope_digest,
                "composite_digest": self.composite_digest,
            }
        )


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
) -> bytes:
    """Extract one JPEG frame from *source_path* at *timestamp_us* via ffmpeg.

    Returns raw JPEG bytes.  Raises :class:`ShadowRenderError` on failure.
    """
    ts_s = timestamp_us / 1_000_000.0
    cmd = [
        ffmpeg_bin,
        "-ss", f"{ts_s:.6f}",
        "-i", str(source_path),
        "-vframes", "1",
        "-vf", f"scale=-2:{target_height}",
        "-q:v", str(jpeg_quality),
        "-f", "image2",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=30,
    )
    if not result.stdout:
        raise ShadowRenderError("empty_frame", f"at t={ts_s:.3f}s (returncode={result.returncode})")
    return result.stdout


def _frame_to_keyframe(jpeg_bytes: bytes, timestamp_us: int) -> ShadowKeyframe:
    """Wrap raw JPEG bytes into a :class:`ShadowKeyframe`."""
    digest = _DIGEST_PREFIX + hashlib.sha256(jpeg_bytes).hexdigest()
    return ShadowKeyframe(
        timestamp_us=timestamp_us,
        jpeg_b64=base64.b64encode(jpeg_bytes).decode("ascii"),
        frame_digest=digest,
    )


def _compute_composite_digest(keyframes: tuple[ShadowKeyframe, ...]) -> str:
    """Deterministic digest of all frame digests in order."""
    return digest_of({"frames": [kf.frame_digest for kf in keyframes]})


def _sample_timestamps(
    scope_start_us: int,
    scope_end_us: int,
    n: int,
) -> list[int]:
    """Return *n* evenly-spaced timestamps in ``[scope_start_us, scope_end_us]``.

    With n=1 returns the midpoint; with n≥2 includes the first and last
    frames of the scope.
    """
    if n == 1:
        return [(scope_start_us + scope_end_us) // 2]
    step = (scope_end_us - scope_start_us) // (n - 1)
    return [scope_start_us + step * i for i in range(n)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_shadow_preview(
    source_path: Path,
    scope: VideoTimeScope,
    plan_digest: str,
    *,
    ffmpeg_bin: str | None = None,
    max_keyframes: int = _DEFAULT_MAX_KEYFRAMES,
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    target_height: int = _DEFAULT_TARGET_HEIGHT,
    source_duration_us: int,
) -> ShadowPreviewManifest:
    """Capture a keyframe strip for *scope* from *source_path*.

    Parameters
    ----------
    source_path:
        Path to the source video file.
    scope:
        The :class:`VideoTimeScope` whose boundaries define the preview window.
    plan_digest:
        Digest of the compiled plan this preview belongs to (sha256:hex64).
    ffmpeg_bin:
        Explicit path to the ffmpeg binary; resolved automatically when ``None``.
    max_keyframes:
        Number of JPEG frames to extract (1–16, default 5).
    jpeg_quality:
        ffmpeg ``-q:v`` value (2=best, 31=worst).
    target_height:
        Target frame height in pixels.
    source_duration_us:
        Total duration of the source asset in microseconds (required for
        manifest metadata; not validated here — the caller is responsible).

    Returns
    -------
    ShadowPreviewManifest
        Frozen, digest-bound manifest of all extracted keyframes.

    Raises
    ------
    ShadowRenderError
        On empty frame output or ffmpeg binary not found.
    ValueError
        If *max_keyframes* is outside the allowed range 1–16.
    """
    if not (_MIN_KEYFRAMES <= max_keyframes <= _MAX_KEYFRAMES):
        raise ValueError(
            f"max_keyframes must be {_MIN_KEYFRAMES}–{_MAX_KEYFRAMES}, got {max_keyframes}"
        )

    # Resolve binary first (raises FileNotFoundError if missing)
    try:
        resolved_bin = resolve_ffmpeg_bin(ffmpeg_bin)
    except (FileNotFoundError, OSError):
        raise ShadowRenderError("ffmpeg_not_found", "ffmpeg binary could not be resolved")

    timestamps = _sample_timestamps(scope.start_us, scope.end_us, max_keyframes)

    keyframes: list[ShadowKeyframe] = []
    for ts_us in timestamps:
        jpeg_bytes = _capture_frame(
            source_path,
            ts_us,
            ffmpeg_bin=resolved_bin,
            jpeg_quality=jpeg_quality,
            target_height=target_height,
        )
        keyframes.append(_frame_to_keyframe(jpeg_bytes, ts_us))

    kf_tuple = tuple(keyframes)
    return ShadowPreviewManifest(
        plan_digest=plan_digest,
        scope_digest=scope.scope_digest,
        source_duration_us=source_duration_us,
        scope_start_us=scope.start_us,
        scope_end_us=scope.end_us,
        keyframes=kf_tuple,
        composite_digest=_compute_composite_digest(kf_tuple),
    )


__all__ = [
    "ShadowKeyframe",
    "ShadowPreviewManifest",
    "ShadowRenderError",
    "build_shadow_preview",
]
