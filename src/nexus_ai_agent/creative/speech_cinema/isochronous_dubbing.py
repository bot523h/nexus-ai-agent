"""Practical deterministic time scaling for dubbing tracks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TimeScaleResult:
    audio: np.ndarray
    source_frames: int
    target_frames: int
    scale: float


def _interp(x: np.ndarray, n: int) -> np.ndarray:
    if n == 0:
        return np.empty((0,) + x.shape[1:], dtype=np.float32)
    if len(x) == 0:
        return np.zeros((n,) + x.shape[1:], dtype=np.float32)
    if len(x) == 1:
        return np.repeat(x, n, axis=0).astype(np.float32)
    pos = np.linspace(0, len(x) - 1, n)
    lo = np.floor(pos).astype(int)
    hi = np.minimum(lo + 1, len(x) - 1)
    a = (pos - lo).astype(np.float32)
    blended = x[lo] * (1 - a).reshape((-1,) + (1,) * (x.ndim - 1)) + x[hi] * a.reshape(
        (-1,) + (1,) * (x.ndim - 1)
    )
    return np.asarray(blended, dtype=np.float32)


def time_scale(
    audio: np.ndarray,
    target_frames: int,
    sample_rate: int | None = None,
    *,
    window_size: int = 1024,
    hop_size: int | None = None,
) -> TimeScaleResult:
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim not in (1, 2):
        raise ValueError("audio must be 1-D mono or 2-D frames-by-channels")
    if isinstance(target_frames, bool) or int(target_frames) < 0:
        raise ValueError("target_frames must be non-negative")
    target = int(target_frames)
    source_frames = int(x.shape[0])
    if source_frames > 0:
        ratio = target / source_frames
        if ratio <= 0.2 or ratio >= 4.0:
            raise ValueError(
                f"stretch ratio {ratio:.4g} is outside the supported open range (0.2, 4.0)"
            )
    y = _interp(x, target)
    if y.shape[0] != target:
        raise AssertionError("time scaler failed exact frame count")  # pragma: no cover - invariant
    return TimeScaleResult(y, int(x.shape[0]), target, target / x.shape[0] if x.shape[0] else 0.0)


def scale_audio(
    audio: np.ndarray,
    target_frames: int,
    sample_rate: int | None = None,
    *,
    window_size: int = 1024,
    hop_size: int | None = None,
) -> np.ndarray:
    result = time_scale(
        audio,
        target_frames,
        sample_rate,
        window_size=window_size,
        hop_size=hop_size,
    )
    return result.audio


def validate_frame_count(audio: np.ndarray, expected: int) -> bool:
    return int(np.asarray(audio).shape[0]) == int(expected)


time_stretch_wsola_pcm = time_scale
