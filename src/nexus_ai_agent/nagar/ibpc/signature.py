"""Scope Signature — deterministic, model-free visual + motion fingerprint (NAGAR IBPC).

A :class:`ScopeSignature` is a reproducible fingerprint of one
:class:`~nexus_ai_agent.nagar.ibpc.models.VideoTimeScope`, computed entirely
from real FFmpeg output (no OpenCV, no scipy, no LLM, no new pip deps):

* A **64-bin CIE L\\*a\\*b\\* histogram** — L\\*∈[0,100] → 32 bins,
  a\\*∈[-128,127] → 16 bins, b\\*∈[-128,127] → 16 bins — L2-normalised.
  sRGB (8-bit) → linear RGB → CIE XYZ (D65) → CIE L\\*a\\*b\\*, pure Python.
* A **motion magnitude** = mean absolute difference between consecutive
  grayscale (Rec.601 luma) frames, in ``[0, 255]``.
* A **stable signature digest** over immutable content; ``computed_at`` is
  deliberately **excluded** so two identical runs produce the same digest.

No file writes; frames flow through ffmpeg stdout (``format=rgb24``).  This
module imports from ``models``, ``canonical`` and ``creative.slideshow.ffmpeg``;
it explicitly MUST NOT import from ``service``, ``session``, ``approval``,
``footprint`` or ``shadow``.
"""

from __future__ import annotations

import math
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fixed split of the 64 histogram bins: L*=32, a*=16, b*=16 (Spec §3.3).
_L_BINS = 32
_A_BINS = 16
_B_BINS = 16
_HIST_LEN = _L_BINS + _A_BINS + _B_BINS  # 64

_L_MIN, _L_MAX = 0.0, 100.0
_A_MIN, _A_MAX = -128.0, 127.0
_B_MIN, _B_MAX = -128.0, 127.0

# D65 illuminant reference white
_D65_X = 0.95047
_D65_Y = 1.00000
_D65_Z = 1.08883

_DEFAULT_MAX_FRAMES = 8
_MIN_FRAMES = 2
_MAX_FRAMES = 32
_DEFAULT_FRAME_WIDTH = 160
_DEFAULT_FRAME_HEIGHT = 90
_DEFAULT_THRESHOLD = 0.80
_DEFAULT_MAX_RESULTS = 5
_PER_FRAME_TIMEOUT_S = 30
_TOTAL_TIMEOUT_S = 120
_DIGEST_PREFIX = "sha256:"

#: Bumped whenever the digest payload / model shape changes (ADR-0006).
SIGNATURE_SCHEMA_VERSION = "nexus.ibpc.scope_signature.v1"
#: Bumped whenever the colour/motion mathematics changes.
SIGNATURE_ALGORITHM_VERSION = "lab_l32a16b16_mad_rec601_v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignatureError(RuntimeError):
    """Raised when a scope signature cannot be computed.

    ``reason`` is a machine-readable slug, e.g. ``"empty_scope"``,
    ``"empty_frames"``, ``"ffmpeg_not_found"``, ``"timeout"``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        msg = reason if not detail else f"{reason}: {detail}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Models (Spec §3.3)
# ---------------------------------------------------------------------------


def _reject_non_finite(value: tuple[float, ...]) -> None:
    for v in value:
        if not math.isfinite(v):
            raise ValueError("histogram contains a non-finite value")
        if v < 0.0:
            raise ValueError("histogram contains a negative value")


class ScopeSignature(BaseModel):
    """Deterministic, model-free visual + motion fingerprint of one scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    asset_id: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    color_histogram: tuple[float, ...]  # len == 64, L2-normalised
    # mean luma difference between consecutive frames, bounded by the 8-bit range
    motion_magnitude: float = Field(ge=0.0, le=255.0, allow_inf_nan=False)
    sample_frames: int = Field(ge=1)
    computed_at: str
    signature_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("color_histogram")
    @classmethod
    def _check_histogram(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) != _HIST_LEN:
            raise ValueError(f"color_histogram must have {_HIST_LEN} elements, got {len(v)}")
        _reject_non_finite(v)
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> ScopeSignature:
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")
        energy = sum(x * x for x in self.color_histogram)
        # L2-normalised, or the zero vector (an all-black scope)
        if energy != 0.0 and abs(energy - 1.0) > 1e-9:
            raise ValueError("color_histogram must be L2-normalised (or the zero vector)")
        return self


class ScopeMatch(BaseModel):
    """One similarity hit from :class:`ScopeMatchEngine`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cosine_similarity: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    motion_delta: Annotated[float, Field(ge=0.0, le=255.0, allow_inf_nan=False)]


# ---------------------------------------------------------------------------
# Pure-Python sRGB → CIE-L*a*b* (D65)
# ---------------------------------------------------------------------------

# LUT: 8-bit sRGB value → linear RGB (the expensive pow() done once, not per pixel).
_SRGB_TO_LINEAR: tuple[float, ...] = tuple(
    (c / 255.0 / 12.92) if (c / 255.0) <= 0.04045 else (((c / 255.0 + 0.055) / 1.055) ** 2.4)
    for c in range(256)
)


def _linear_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Linear sRGB → CIE XYZ (D65)."""
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    return x, y, z


def _f_lab(t: float) -> float:
    """CIE-Lab f() function."""
    delta = 6.0 / 29.0
    if t > delta**3:
        return t ** (1.0 / 3.0)
    return t / (3.0 * delta * delta) + 4.0 / 29.0


def _xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    """CIE XYZ → L*a*b* (D65 illuminant)."""
    fx = _f_lab(x / _D65_X)
    fy = _f_lab(y / _D65_Y)
    fz = _f_lab(z / _D65_Z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def _rgb8_to_lab(r8: int, g8: int, b8: int) -> tuple[float, float, float]:
    """8-bit sRGB triplet → CIE L*a*b*."""
    x, y, z = _linear_to_xyz(_SRGB_TO_LINEAR[r8], _SRGB_TO_LINEAR[g8], _SRGB_TO_LINEAR[b8])
    return _xyz_to_lab(x, y, z)


def _luma601(r8: int, g8: int, b8: int) -> float:
    """Rec.601 luma Y' (the *fixed, documented* grayscale definition)."""
    return 0.299 * r8 + 0.587 * g8 + 0.114 * b8


# ---------------------------------------------------------------------------
# Histogram / motion helpers
# ---------------------------------------------------------------------------


def _bin_index(value: float, vmin: float, vmax: float, bins: int) -> int:
    clamped = max(vmin, min(vmax, value))
    idx = int((clamped - vmin) / (vmax - vmin) * bins)
    return min(idx, bins - 1)


def _accumulate_histogram(frames: list[bytes]) -> list[float]:
    """Build the 64-bin L*a*b* histogram (counts) from raw rgb24 frames."""
    counts = [0.0] * _HIST_LEN
    for raw in frames:
        n_pixels = len(raw) // 3
        for i in range(n_pixels):
            lab_l, lab_a, lab_b = _rgb8_to_lab(raw[3 * i], raw[3 * i + 1], raw[3 * i + 2])
            counts[_bin_index(lab_l, _L_MIN, _L_MAX, _L_BINS)] += 1.0
            counts[_L_BINS + _bin_index(lab_a, _A_MIN, _A_MAX, _A_BINS)] += 1.0
            counts[_L_BINS + _A_BINS + _bin_index(lab_b, _B_MIN, _B_MAX, _B_BINS)] += 1.0
    return counts


def _l2_normalize(counts: list[float]) -> list[float]:
    norm = math.sqrt(sum(c * c for c in counts))
    if norm == 0.0:
        return counts  # all-black video → zero vector, stored as-is
    return [c / norm for c in counts]


def _motion_magnitude(frames: list[bytes]) -> float:
    """Mean of per-pair mean-absolute-difference between consecutive luma frames."""
    if len(frames) < 2:
        return 0.0
    mads: list[float] = []
    for prev, cur in zip(frames, frames[1:], strict=False):
        n = min(len(prev), len(cur)) // 3
        if n == 0:
            continue
        total = 0.0
        for i in range(n):
            y_prev = _luma601(prev[3 * i], prev[3 * i + 1], prev[3 * i + 2])
            y_cur = _luma601(cur[3 * i], cur[3 * i + 1], cur[3 * i + 2])
            total += abs(y_cur - y_prev)
        mads.append(total / n)
    if not mads:
        return 0.0
    return sum(mads) / len(mads)


def _stable_digest(
    *,
    scope_digest: str,
    asset_id: str,
    start_us: int,
    end_us: int,
    histogram: tuple[float, ...],
    motion_magnitude: float,
    sample_frames: int,
) -> str:
    """Canonical digest over immutable content — ``computed_at`` NOT included."""
    return digest_of(
        {
            "schema_version": SIGNATURE_SCHEMA_VERSION,
            "algorithm_version": SIGNATURE_ALGORITHM_VERSION,
            "scope_digest": scope_digest,
            "asset_id": asset_id,
            "start_us": start_us,
            "end_us": end_us,
            "color_histogram": list(histogram),
            "motion_magnitude": motion_magnitude,
            "sample_frames": sample_frames,
        }
    )


# ---------------------------------------------------------------------------
# Public: verify_scope_signature
# ---------------------------------------------------------------------------


def verify_scope_signature(signature: ScopeSignature) -> None:
    """Recompute ``signature_digest`` from the carried content; raise on mismatch.

    Consumer-side check: a signature whose histogram, motion or sample count was
    altered after computation no longer matches its digest. ``scope_digest`` is
    bound as given by the caller (the scope itself is not carried here).
    """
    expected = _stable_digest(
        scope_digest=signature.scope_digest,
        asset_id=signature.asset_id,
        start_us=signature.start_us,
        end_us=signature.end_us,
        histogram=signature.color_histogram,
        motion_magnitude=signature.motion_magnitude,
        sample_frames=signature.sample_frames,
    )
    if expected != signature.signature_digest:
        raise SignatureError("signature_digest_mismatch", "content does not match signature_digest")


# ---------------------------------------------------------------------------
# Public: cosine_similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two non-negative vectors, clamped to ``[0, 1]``.

    ``a`` and ``b`` are expected to be L2-normalised histograms, so the dot
    product *is* the cosine.  Returns ``0.0`` if either vector is the zero
    vector (e.g. an all-black frame).  Raises :class:`ValueError` on
    mismatched lengths or a negative / non-finite element.
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    if len(a) == 0:
        raise ValueError("vectors must be non-empty")
    for v in (*a, *b):
        if not math.isfinite(v):
            raise ValueError("vector contains a non-finite value")
        if v < 0.0:
            raise ValueError("vector contains a negative value")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


# ---------------------------------------------------------------------------
# ffmpeg frame extraction
# ---------------------------------------------------------------------------


def _capture_rgb_frame(
    source_path: Path,
    timestamp_us: int,
    *,
    ffmpeg_bin: str,
    width: int,
    height: int,
    timeout_s: float,
) -> bytes:
    """Extract one raw ``rgb24`` frame from *source_path* at *timestamp_us*.

    Always a list of arguments (never ``shell=True``), stdout pipe only (no
    disk writes), timeout enforced and translated to :class:`SignatureError`.
    """
    ts_s = timestamp_us / 1_000_000.0
    expected_bytes = width * height * 3
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
        f"scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=max(0.001, timeout_s))
    except subprocess.TimeoutExpired as exc:
        raise SignatureError(
            "timeout", f"frame capture exceeded {timeout_s:.1f}s at t={ts_s:.3f}s"
        ) from exc
    if not result.stdout:
        raise SignatureError(
            "empty_frames", f"no frame at t={ts_s:.3f}s (returncode={result.returncode})"
        )
    if result.returncode != 0:
        # Bytes from a failed run are never a frame, even when they look like one.
        raise SignatureError("ffmpeg_failed", f"returncode={result.returncode} at t={ts_s:.3f}s")
    if len(result.stdout) != expected_bytes:
        raise SignatureError(
            "bad_frame_size",
            f"got {len(result.stdout)} bytes, expected {expected_bytes} at t={ts_s:.3f}s",
        )
    return result.stdout


def _sample_timestamps(scope_start_us: int, scope_end_us: int, n: int) -> list[int]:
    """Evenly-spaced timestamps in the half-open ``[start, end)``."""
    duration_us = scope_end_us - scope_start_us
    return [scope_start_us + (i * duration_us) // n for i in range(n)]


# ---------------------------------------------------------------------------
# Public: compute_scope_signature
# ---------------------------------------------------------------------------


def compute_scope_signature(
    source_path: Path,
    scope: VideoTimeScope,
    *,
    ffmpeg_bin: str | None = None,
    max_frames: int = _DEFAULT_MAX_FRAMES,
    frame_width: int = _DEFAULT_FRAME_WIDTH,
    frame_height: int = _DEFAULT_FRAME_HEIGHT,
) -> ScopeSignature:
    """Compute a CIE-L*a*b* + motion fingerprint for *scope*.

    Raises
    ------
    ValueError
        If *max_frames* is outside 2–32.
    SignatureError
        On an empty scope, no extractable frames, ffmpeg not found, or timeout.
    """
    if not (_MIN_FRAMES <= max_frames <= _MAX_FRAMES):
        raise ValueError(f"max_frames must be {_MIN_FRAMES}–{_MAX_FRAMES}, got {max_frames}")

    if scope.end_us <= scope.start_us:
        raise SignatureError("empty_scope", f"{scope.start_us}..{scope.end_us}")

    try:
        resolved_bin = resolve_ffmpeg_bin(ffmpeg_bin)
    except (FfmpegUnavailableError, FileNotFoundError, OSError) as exc:
        # FfmpegUnavailableError is a RuntimeError, not an OSError — it must be
        # translated here so no raw resolver error crosses the public boundary.
        raise SignatureError("ffmpeg_not_found", "ffmpeg binary could not be resolved") from exc

    timestamps = _sample_timestamps(scope.start_us, scope.end_us, max_frames)
    deadline = time.monotonic() + _TOTAL_TIMEOUT_S

    frames: list[bytes] = []
    for ts_us in timestamps:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SignatureError("timeout", f"total extraction exceeded {_TOTAL_TIMEOUT_S}s")
        raw = _capture_rgb_frame(
            source_path,
            ts_us,
            ffmpeg_bin=resolved_bin,
            width=frame_width,
            height=frame_height,
            timeout_s=min(_PER_FRAME_TIMEOUT_S, remaining),
        )
        frames.append(raw)

    if not frames:
        raise SignatureError("empty_frames", "no frames could be extracted from the scope")

    histogram = tuple(_l2_normalize(_accumulate_histogram(frames)))
    motion = _motion_magnitude(frames)

    computed_at = datetime.now(timezone.utc).isoformat()
    sig_digest = _stable_digest(
        scope_digest=scope.scope_digest,
        asset_id=scope.asset_id,
        start_us=scope.start_us,
        end_us=scope.end_us,
        histogram=histogram,
        motion_magnitude=motion,
        sample_frames=len(frames),
    )

    return ScopeSignature(
        scope_digest=scope.scope_digest,
        asset_id=scope.asset_id,
        start_us=scope.start_us,
        end_us=scope.end_us,
        color_histogram=histogram,
        motion_magnitude=motion,
        sample_frames=len(frames),
        computed_at=computed_at,
        signature_digest=sig_digest,
    )


# ---------------------------------------------------------------------------
# Public: ScopeMatchEngine
# ---------------------------------------------------------------------------


class ScopeMatchEngine:
    """Stateless cosine-similarity search over candidate :class:`ScopeSignature` objects.

    Deterministic: results are sorted by ``cosine_similarity`` descending and,
    on ties, by ``candidate_scope_digest`` ascending — so the ranking never
    depends on input order or an unstable sort.
    """

    def find_similar(
        self,
        source: ScopeSignature,
        candidates: Sequence[ScopeSignature],
        *,
        threshold: float = _DEFAULT_THRESHOLD,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> list[ScopeMatch]:
        """Return candidates with ``cosine_similarity >= threshold``.

        Sorted descending by similarity (ties broken deterministically), at
        most *max_results* entries.  Raises :class:`ValueError` if *threshold*
        is outside ``[0, 1]`` or *max_results* is negative.
        """
        if not (0.0 <= threshold <= 1.0):
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        if max_results < 0:
            raise ValueError(f"max_results must be >= 0, got {max_results}")

        matches: list[ScopeMatch] = []
        for candidate in candidates:
            if len(candidate.color_histogram) != len(source.color_histogram):
                raise ValueError("candidate histogram length differs from the source")
            sim = cosine_similarity(source.color_histogram, candidate.color_histogram)
            if sim >= threshold:
                matches.append(
                    ScopeMatch(
                        candidate_scope_digest=candidate.scope_digest,
                        cosine_similarity=sim,
                        motion_delta=abs(source.motion_magnitude - candidate.motion_magnitude),
                    )
                )

        matches.sort(key=lambda m: (-m.cosine_similarity, m.candidate_scope_digest))
        return matches[:max_results]


__all__ = [
    "SIGNATURE_ALGORITHM_VERSION",
    "SIGNATURE_SCHEMA_VERSION",
    "ScopeMatch",
    "ScopeMatchEngine",
    "ScopeSignature",
    "SignatureError",
    "compute_scope_signature",
    "cosine_similarity",
    "verify_scope_signature",
]
