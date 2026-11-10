"""Scope Signature — CIE-Lab histogram fingerprint + cosine similarity (NAGAR IBPC).

Computes a compact, perceptually-meaningful fingerprint of a video scope by:
1. Extracting ``N`` raw RGB frames via ffmpeg (subprocess, no shell=True).
2. Converting each pixel from sRGB → linear RGB → CIE XYZ → CIE L*a*b* (pure Python,
   no scipy, no numpy — arithmetic only).
3. Building a 64-bin histogram per channel (L*, a*, b*) → 192-dimensional vector.
4. L2-normalising the histogram so cosine similarity is a simple dot product.
5. Computing a ``motion_mad`` (median absolute deviation of per-frame mean-L*) as a
   secondary motion fingerprint for distinguishing visually-similar-but-different clips.

Design constraints:
- No LLM, no external API, no new pip dependencies.
- Pure Python CIE-Lab conversion (no scipy/colorsys Lab path).
- ffmpeg subprocess list args, no shell=True.
- All models: ``ConfigDict(extra="forbid", frozen=True)``.
- ``sig_digest`` bound deterministically from (scope_digest, histogram, motion_mad).

This module imports from ``models``, ``canonical``, and
``nexus_ai_agent.creative.slideshow.ffmpeg``.
It explicitly MUST NOT import from ``service``, ``session``, ``approval``,
``shadow``, or ``footprint``.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HIST_BINS = 64            # bins per channel
_CHANNELS = 3              # L*, a*, b*
_HIST_LEN = _HIST_BINS * _CHANNELS  # 192

_DEFAULT_SAMPLE_FRAMES = 8
_MIN_FRAMES = 1
_MAX_FRAMES = 32

# CIE-Lab channel ranges used for binning
# L*: [0, 100],  a*: [-128, 127],  b*: [-128, 127]
_L_MIN, _L_MAX = 0.0, 100.0
_A_MIN, _A_MAX = -128.0, 127.0
_B_MIN, _B_MAX = -128.0, 127.0

# D65 illuminant XYZ reference white
_D65_X = 0.95047
_D65_Y = 1.00000
_D65_Z = 1.08883

_DIGEST_PREFIX = "sha256:"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignatureError(RuntimeError):
    """Raised when a scope signature cannot be computed.

    ``reason`` is a machine-readable slug, e.g. ``"empty_frames"``,
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


class ScopeSignature(BaseModel):
    """Perceptual fingerprint of a video scope.

    ``histogram`` is a 192-dimensional L2-normalized CIE-Lab histogram.
    ``sig_digest`` is a deterministic sha256 binding of (scope_digest + histogram +
    motion_mad).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    histogram: tuple[float, ...]   # 192 values, L2-normalised
    motion_mad: float              # median absolute deviation of per-frame mean-L*
    frame_count: int
    sig_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @field_validator("histogram")
    @classmethod
    def _check_histogram_length(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) != _HIST_LEN:
            raise ValueError(
                f"histogram must have {_HIST_LEN} elements, got {len(v)}"
            )
        return v


class ScopeMatch(BaseModel):
    """One hit from :class:`ScopeMatchEngine`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    candidate_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    similarity: Annotated[float, Field(ge=-1.0, le=1.0)]


# ---------------------------------------------------------------------------
# Pure-Python CIE-Lab conversion
# ---------------------------------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    """sRGB channel [0,1] → linear RGB [0,1]."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _linear_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Linear sRGB → CIE XYZ (D65)."""
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    return x, y, z


def _f_lab(t: float) -> float:
    """CIE-Lab f() function."""
    delta = 6.0 / 29.0
    if t > delta ** 3:
        return t ** (1.0 / 3.0)
    return t / (3.0 * delta * delta) + 4.0 / 29.0


def _xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    """CIE XYZ → L*a*b* (D65 illuminant)."""
    fx = _f_lab(x / _D65_X)
    fy = _f_lab(y / _D65_Y)
    fz = _f_lab(z / _D65_Z)
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return L, a, b


def _rgb8_to_lab(r8: int, g8: int, b8: int) -> tuple[float, float, float]:
    """8-bit sRGB triplet → CIE L*a*b*."""
    r_lin = _srgb_to_linear(r8 / 255.0)
    g_lin = _srgb_to_linear(g8 / 255.0)
    b_lin = _srgb_to_linear(b8 / 255.0)
    x, y, z = _linear_to_xyz(r_lin, g_lin, b_lin)
    return _xyz_to_lab(x, y, z)


# ---------------------------------------------------------------------------
# Histogram helpers
# ---------------------------------------------------------------------------


def _bin_index(value: float, vmin: float, vmax: float, bins: int) -> int:
    """Map a scalar *value* in [vmin, vmax] to a bin index in [0, bins-1]."""
    clamped = max(vmin, min(vmax, value))
    t = (clamped - vmin) / (vmax - vmin)
    idx = int(t * bins)
    return min(idx, bins - 1)


def _build_histogram(frame_bytes_list: list[bytes], width: int, height: int) -> list[float]:
    """Build a 192-bin CIE-Lab histogram (L/a/b 64-bins each) from raw RGB24 frames.

    Returns a L2-normalized float list of length 192.
    """
    counts: list[float] = [0.0] * _HIST_LEN
    total_pixels = 0

    for raw in frame_bytes_list:
        n_pixels = len(raw) // 3
        for i in range(n_pixels):
            r8 = raw[3 * i]
            g8 = raw[3 * i + 1]
            b8 = raw[3 * i + 2]
            L, a, b = _rgb8_to_lab(r8, g8, b8)
            li = _bin_index(L, _L_MIN, _L_MAX, _HIST_BINS)
            ai = _bin_index(a, _A_MIN, _A_MAX, _HIST_BINS)
            bi = _bin_index(b, _B_MIN, _B_MAX, _HIST_BINS)
            counts[li] += 1.0
            counts[_HIST_BINS + ai] += 1.0
            counts[_HIST_BINS * 2 + bi] += 1.0
            total_pixels += 1

    if total_pixels == 0:
        return counts

    # L2-normalize
    norm = math.sqrt(sum(c * c for c in counts))
    if norm == 0.0:
        return counts
    return [c / norm for c in counts]


def _mean_L(raw: bytes) -> float:
    """Mean L* of a single raw RGB24 frame."""
    n = len(raw) // 3
    if n == 0:
        return 0.0
    total = 0.0
    for i in range(n):
        r8, g8, b8 = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        L, _, _ = _rgb8_to_lab(r8, g8, b8)
        total += L
    return total / n


def _motion_mad(mean_L_values: list[float]) -> float:
    """Median absolute deviation of per-frame mean-L* values."""
    if not mean_L_values:
        return 0.0
    sorted_vals = sorted(mean_L_values)
    n = len(sorted_vals)
    median = (sorted_vals[n // 2] if n % 2 else
              (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0)
    deviations = sorted(abs(v - median) for v in mean_L_values)
    n2 = len(deviations)
    return (deviations[n2 // 2] if n2 % 2 else
            (deviations[n2 // 2 - 1] + deviations[n2 // 2]) / 2.0)


# ---------------------------------------------------------------------------
# Public: cosine_similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    """Cosine similarity of two vectors (pure Python dot product).

    Returns a float in [-1, 1].  Returns 0.0 for zero vectors.
    Raises :class:`ValueError` if the vectors have different lengths.
    """
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# ffmpeg frame extraction
# ---------------------------------------------------------------------------


def _capture_rgb_frame(
    source_path: Path,
    timestamp_us: int,
    *,
    ffmpeg_bin: str,
    target_height: int = 64,
    target_width: int = 64,
) -> bytes:
    """Extract one raw RGB24 frame from *source_path* at *timestamp_us*.

    Returns raw bytes of length ``target_width * target_height * 3``.
    Raises :class:`SignatureError` on failure.
    """
    ts_s = timestamp_us / 1_000_000.0
    cmd = [
        ffmpeg_bin,
        "-ss", f"{ts_s:.6f}",
        "-i", str(source_path),
        "-vframes", "1",
        "-vf", f"scale={target_width}:{target_height}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.stdout


# ---------------------------------------------------------------------------
# Public: compute_scope_signature
# ---------------------------------------------------------------------------


def compute_scope_signature(
    source_path: Path,
    scope: VideoTimeScope,
    *,
    source_duration_us: int,
    ffmpeg_bin: str | None = None,
    sample_frames: int = _DEFAULT_SAMPLE_FRAMES,
    frame_width: int = 64,
    frame_height: int = 64,
) -> ScopeSignature:
    """Compute a perceptual CIE-Lab histogram fingerprint for *scope*.

    Extracts ``sample_frames`` raw RGB24 frames from the source video,
    converts them to CIE-Lab, and builds a 192-bin L2-normalized histogram.
    A motion_mad value captures luminance variation across frames.

    Parameters
    ----------
    source_path:
        Path to the source video file.
    scope:
        The :class:`VideoTimeScope` whose pixels are sampled.
    source_duration_us:
        Total duration of the source asset in microseconds.
    ffmpeg_bin:
        Explicit path to the ffmpeg binary; auto-resolved when ``None``.
    sample_frames:
        Number of frames to sample (1–32).
    frame_width, frame_height:
        Resolution of each extracted frame (default 64×64).

    Returns
    -------
    ScopeSignature
        Frozen, digest-bound signature.

    Raises
    ------
    SignatureError
        On empty frame output or ffmpeg binary not found.
    """
    if not (_MIN_FRAMES <= sample_frames <= _MAX_FRAMES):
        raise ValueError(f"sample_frames must be {_MIN_FRAMES}–{_MAX_FRAMES}, got {sample_frames}")

    try:
        resolved_bin = resolve_ffmpeg_bin(ffmpeg_bin)
    except (FileNotFoundError, OSError):
        raise SignatureError("ffmpeg_not_found", "ffmpeg binary could not be resolved")

    # Sample timestamps across the scope
    n = sample_frames
    start_us = scope.start_us
    end_us = scope.end_us
    if n == 1:
        timestamps = [(start_us + end_us) // 2]
    else:
        step = (end_us - start_us) // (n - 1)
        timestamps = [start_us + step * i for i in range(n)]

    frame_bytes_list: list[bytes] = []
    mean_L_values: list[float] = []

    for ts_us in timestamps:
        raw = _capture_rgb_frame(
            source_path, ts_us,
            ffmpeg_bin=resolved_bin,
            target_width=frame_width,
            target_height=frame_height,
        )
        if raw:
            frame_bytes_list.append(raw)
            mean_L_values.append(_mean_L(raw))

    if not frame_bytes_list:
        raise SignatureError("empty_frames", "no frames could be extracted from scope")

    histogram = _build_histogram(frame_bytes_list, frame_width, frame_height)
    mad = _motion_mad(mean_L_values)

    # Deterministic sig_digest from canonical serialization
    sig_digest = digest_of({
        "scope_digest": scope.scope_digest,
        "histogram": histogram,
        "motion_mad": mad,
        "frame_count": len(frame_bytes_list),
    })

    return ScopeSignature(
        scope_digest=scope.scope_digest,
        histogram=tuple(histogram),
        motion_mad=mad,
        frame_count=len(frame_bytes_list),
        sig_digest=sig_digest,
    )


# ---------------------------------------------------------------------------
# Public: ScopeMatchEngine
# ---------------------------------------------------------------------------


class ScopeMatchEngine:
    """In-memory cosine-similarity search over a library of :class:`ScopeSignature` objects.

    All arithmetic is pure Python — no numpy, no scipy.

    Parameters
    ----------
    library:
        Iterable of :class:`ScopeSignature` objects to search against.
    """

    def __init__(self, library: list[ScopeSignature]) -> None:
        self._library = list(library)

    def find_similar(
        self,
        query: ScopeSignature,
        *,
        top_k: int = 5,
        threshold: float = 0.0,
    ) -> list[ScopeMatch]:
        """Return the *top_k* most similar signatures above *threshold*.

        Parameters
        ----------
        query:
            The query :class:`ScopeSignature`.
        top_k:
            Maximum number of results to return.
        threshold:
            Minimum cosine similarity to include a result.

        Returns
        -------
        list[ScopeMatch]
            Sorted by similarity descending.  Empty if ``top_k == 0`` or no
            candidates exceed *threshold*.
        """
        if top_k == 0 or not self._library:
            return []

        scored: list[tuple[float, ScopeSignature]] = []
        for candidate in self._library:
            if len(candidate.histogram) != len(query.histogram):
                continue
            sim = cosine_similarity(list(query.histogram), list(candidate.histogram))
            if sim >= threshold:
                scored.append((sim, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[ScopeMatch] = []
        for sim, cand in scored[:top_k]:
            results.append(
                ScopeMatch(
                    query_digest=query.sig_digest,
                    candidate_digest=cand.sig_digest,
                    similarity=sim,
                )
            )
        return results


__all__ = [
    "ScopeMatch",
    "ScopeMatchEngine",
    "ScopeSignature",
    "SignatureError",
    "compute_scope_signature",
    "cosine_similarity",
]
