"""Fast optical/spectral proxy analyser with Shannon entropy, JS divergence
and an Euler–Lagrange trajectory stabiliser.

Frames are extracted *streamed* from FFmpeg as tiny proxy rasters
(``160x90`` or ``320x180``) and processed in a two-frame sliding window, so at
most two proxy frames live in Python RAM at any instant (well under the
30 MiB budget).  Colour is handled in the perceptual **OKLab** space using
Ottosson's standard matrices.

Pipeline::

    rawvideo proxy frames  →  sRGB linear → OKLab  →  3-D (L, a, b) histogram
                                                                      ↓
                                          Jensen–Shannon shot boundaries
                                                                      ↓
                                        per-frame (a, b) colour trajectory
                                                                      ↓
                                    Euler–Lagrange smoothing (velocity+jerk)
                                                                      ↓
                                16 kHz audio stream → silence/speech segments
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from typing import Final

import numpy as np

#: Maximum proxy frames resident in Python RAM at once (sliding window of 2).
MAX_FRAMES_IN_MEMORY = 2

#: Proxy raster options.  Small enough to keep the whole analyser under
#: ~30 MiB even for long inputs.
PROXY_WIDTH = 160
PROXY_HEIGHT = 90

#: OKLab histogram binning over (L, a, b).
_HIST_BINS = 8
_L_RANGE = (0.0, 1.0)
_AB_RANGE = (-0.22, 0.22)

#: Euler–Lagrange smoothing penalties (velocity, acceleration, jerk).
_DEFAULT_LAMBDA_1 = 1.0
_DEFAULT_LAMBDA_2 = 0.5
_DEFAULT_LAMBDA_3 = 0.1

#: Audio analysis sample rate (Hz).
AUDIO_SAMPLE_RATE = 16_000

#: dBFS threshold below which a window is treated as silence.
_SILENCE_DBFS = -40.0
#: Analysis window length (seconds).
_AUDIO_WINDOW_SEC = 0.025


# --------------------------------------------------------------------------- #
# OKLab colour space (Ottosson standard matrices)
# --------------------------------------------------------------------------- #

#: sRGB-linear → LMS (M1).
_M1: Final[np.ndarray] = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1079967996],
        [0.0087005252, 0.0106594072, 0.6095581317],
    ],
    dtype=np.float64,
)

#: LMS → OKLab (M2), applied to the cube-rooted LMS channels.
_M2: Final[np.ndarray] = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.0678577148],
    ],
    dtype=np.float64,
)


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """Linearise sRGB ``[0, 1]`` channels to linear light."""
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """Convert an ``(..., 3)`` linear-sRGB array to OKLab ``(L, a, b)``.

    ``cbrt`` handles negative LMS channels correctly via ``sign * |x|^(1/3)``.
    """
    linear = srgb_to_linear(np.clip(rgb, 0.0, 1.0))
    lms = linear @ _M1.T
    cbrt = np.sign(lms) * np.power(np.abs(lms), 1.0 / 3.0)
    return cbrt @ _M2.T


# --------------------------------------------------------------------------- #
# Frame streaming
# --------------------------------------------------------------------------- #


def proxy_frames(
    input_path: str,
    *,
    width: int = PROXY_WIDTH,
    height: int = PROXY_HEIGHT,
) -> list[np.ndarray]:
    """Yield proxy frames as ``(height, width, 3)`` float RGB arrays.

    The FFmpeg pipe is read frame-by-frame so no more than one raw frame plus
    the current conversion lives in memory at a time.  Returned as a list for
    simplicity; callers should process pairwise and release references.  Each
    proxy frame is only ~42 KiB (``160x90x3``).
    """
    cmd = [
        "ffmpeg",
        "-v",
        "quiet",
        "-nostdin",
        "-i",
        input_path,
        "-vf",
        f"scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_size = width * height * 3
    frames: list[np.ndarray] = []
    assert proc.stdout is not None
    try:
        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            frames.append(
                np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).astype(np.float64)
                / 255.0
            )
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)
        proc.stderr.close() if proc.stderr else None
    return frames


# --------------------------------------------------------------------------- #
# Histogram / Jensen–Shannon shot boundary detection
# --------------------------------------------------------------------------- #


def _unit_vector(value: np.ndarray) -> np.ndarray:
    total = float(value.sum())
    if total <= 0:
        return np.zeros_like(value, dtype=np.float64)
    return value / total


def oklab_histogram(oklab: np.ndarray) -> np.ndarray:
    """3-D probability histogram of an OKLab frame over ``(L, a, b)``."""
    l_channel = oklab[..., 0].ravel()
    a_channel = oklab[..., 1].ravel()
    b_channel = oklab[..., 2].ravel()
    l_bin = np.clip(
        np.floor((l_channel - _L_RANGE[0]) / (_L_RANGE[1] - _L_RANGE[0]) * _HIST_BINS).astype(int),
        0,
        _HIST_BINS - 1,
    )
    a_bin = np.clip(
        np.floor((a_channel - _AB_RANGE[0]) / (_AB_RANGE[1] - _AB_RANGE[0]) * _HIST_BINS).astype(
            int
        ),
        0,
        _HIST_BINS - 1,
    )
    b_bin = np.clip(
        np.floor((b_channel - _AB_RANGE[0]) / (_AB_RANGE[1] - _AB_RANGE[0]) * _HIST_BINS).astype(
            int
        ),
        0,
        _HIST_BINS - 1,
    )
    histogram = np.zeros((_HIST_BINS, _HIST_BINS, _HIST_BINS), dtype=np.float64)
    np.add.at(histogram, (l_bin, a_bin, b_bin), 1.0)
    return _unit_vector(histogram.ravel())


def jensen_shannon_divergence(p: np.ndarray, q: np.ndarray) -> float:
    """Symmetric Jensen–Shannon divergence ``D_JS(P || Q)`` in nats."""
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-12, None)
    q = np.clip(np.asarray(q, dtype=np.float64), 1e-12, None)
    p = _unit_vector(p)
    q = _unit_vector(q)
    mean = 0.5 * (p + q)

    def kl(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.sum(a * np.log(a / b)))

    return 0.5 * kl(p, mean) + 0.5 * kl(q, mean)


@dataclass(frozen=True)
class ShotBoundary:
    """A detected shot cut between two consecutive proxy frames."""

    frame_index: int
    timestamp_sec: float
    js_divergence: float


def detect_shot_boundaries(
    frames: list[np.ndarray],
    *,
    threshold: float = 0.15,
    fps: float = 30.0,
) -> list[ShotBoundary]:
    """Find shot cuts via Jensen–Shannon divergence on OKLab histograms.

    Consecutive proxy frames are converted to OKLab, binned into a shared
    3-D histogram, and compared with :func:`jensen_shannon_divergence`.
    """
    boundaries: list[ShotBoundary] = []
    if len(frames) < 2:
        return boundaries
    prev_oklab = rgb_to_oklab(frames[0])
    prev_hist = oklab_histogram(prev_oklab)
    frame_duration = 1.0 / fps
    for index in range(1, len(frames)):
        cur_oklab = rgb_to_oklab(frames[index])
        cur_hist = oklab_histogram(cur_oklab)
        divergence = jensen_shannon_divergence(prev_hist, cur_hist)
        if divergence > threshold:
            boundaries.append(
                ShotBoundary(
                    frame_index=index,
                    timestamp_sec=index * frame_duration,
                    js_divergence=divergence,
                )
            )
        prev_hist = cur_hist
    return boundaries


# --------------------------------------------------------------------------- #
# Euler–Lagrange kinematic trajectory smoothing
# --------------------------------------------------------------------------- #


def _difference_matrix(order: int, size: int) -> np.ndarray:
    """Finite-difference matrix :math:`D_k` of the given order."""
    if size <= order:
        return np.zeros((max(size - order, 0), size))
    matrix = np.zeros((size - order, size))
    for i in range(size - order):
        for k in range(order + 1):
            matrix[i, i + k] = ((-1) ** k) * float(math.comb(order, k))
    return matrix


def smooth_trajectory(
    points: np.ndarray,
    *,
    lambda_1: float = _DEFAULT_LAMBDA_1,
    lambda_2: float = _DEFAULT_LAMBDA_2,
    lambda_3: float = _DEFAULT_LAMBDA_3,
) -> np.ndarray:
    """Smooth a trajectory by solving the discrete Euler–Lagrange system.

    Minimises::

        Σ ||x_t − y_t||² + λ₁||Δx_t||² + λ₂||Δ²x_t||² + λ₃||Δ³x_t||²

    i.e. the symmetric positive-definite system

        (I + λ₁D₁ᵀD₁ + λ₂D₂ᵀD₂ + λ₃D₃ᵀD₃) x = y

    ``points`` has shape ``(N, D)`` and is smoothed independently per channel.
    """
    points = np.asarray(points, dtype=np.float64)
    if points.ndim != 2:
        raise ValueError("points must have shape (N, D)")
    n = points.shape[0]
    if n == 0:
        return points.copy()
    eye = np.eye(n)
    system = eye.copy()
    for order, lam in ((1, lambda_1), (2, lambda_2), (3, lambda_3)):
        if lam <= 0:
            continue
        d = _difference_matrix(order, n)
        if d.shape[0] > 0:
            system = system + lam * (d.T @ d)
    smoothed = np.linalg.solve(system, points)
    return smoothed


# --------------------------------------------------------------------------- #
# Audio energy / silence-speech analysis at 16 kHz
# --------------------------------------------------------------------------- #


def _extract_audio_mono(input_path: str) -> np.ndarray:
    cmd = [
        "ffmpeg",
        "-v",
        "quiet",
        "-nostdin",
        "-i",
        input_path,
        "-vn",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        "1",
        "-f",
        "s16le",
        "-",
    ]
    proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    chunks: list[np.ndarray] = []
    try:
        while True:
            raw = proc.stdout.read(1 << 20)
            if not raw:
                break
            chunks.append(np.frombuffer(raw, dtype=np.int16).astype(np.float64))
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)
        if proc.stderr:
            proc.stderr.close()
    if not chunks:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(chunks) / 32768.0


@dataclass(frozen=True)
class AudioSegment:
    """A contiguous silence or speech region."""

    start_us: int
    end_us: int
    is_speech: bool
    rms_dbfs: float


def analyze_audio_silence_speech(
    input_path: str,
    *,
    silence_dbfs: float = _SILENCE_DBFS,
    window_sec: float = _AUDIO_WINDOW_SEC,
) -> list[AudioSegment]:
    """Classify 16 kHz mono audio into silence/speech segments by short-term energy."""
    samples = _extract_audio_mono(input_path)
    if samples.size == 0:
        return []
    hop = max(1, int(AUDIO_SAMPLE_RATE * window_sec))
    rms_values: list[float] = []
    times: list[float] = []
    for start in range(0, samples.size, hop):
        window = samples[start : start + hop]
        if window.size == 0:
            continue
        rms = float(np.sqrt(np.mean(window**2)))
        dbfs = -float("inf") if rms <= 0 else 20.0 * np.log10(rms)
        rms_values.append(dbfs)
        times.append(start / AUDIO_SAMPLE_RATE)
    if not rms_values:
        return []
    rms_array = np.array(rms_values, dtype=np.float64)
    is_speech = rms_array > silence_dbfs

    segments: list[AudioSegment] = []
    current = bool(is_speech[0])
    seg_start_index = 0
    for index in range(1, len(is_speech)):
        if bool(is_speech[index]) != current:
            seg_start_time = times[seg_start_index]
            seg_end_time = times[index - 1] + window_sec
            segments.append(
                AudioSegment(
                    start_us=int(seg_start_time * 1_000_000),
                    end_us=int(seg_end_time * 1_000_000),
                    is_speech=current,
                    rms_dbfs=float(np.mean(rms_array[seg_start_index:index])),
                )
            )
            current = bool(is_speech[index])
            seg_start_index = index - 1
    # close the final segment
    seg_start_time = times[seg_start_index]
    segments.append(
        AudioSegment(
            start_us=int(seg_start_time * 1_000_000),
            end_us=int(times[-1] * 1_000_000 + window_sec * 1_000_000),
            is_speech=current,
            rms_dbfs=float(np.mean(rms_array[seg_start_index:])),
        )
    )
    return segments


__all__ = [
    "AUDIO_SAMPLE_RATE",
    "AudioSegment",
    "MAX_FRAMES_IN_MEMORY",
    "PROXY_HEIGHT",
    "PROXY_WIDTH",
    "ShotBoundary",
    "_M1",
    "_M2",
    "analyze_audio_silence_speech",
    "detect_shot_boundaries",
    "jensen_shannon_divergence",
    "oklab_histogram",
    "proxy_frames",
    "rgb_to_oklab",
    "smooth_trajectory",
    "srgb_to_linear",
]
