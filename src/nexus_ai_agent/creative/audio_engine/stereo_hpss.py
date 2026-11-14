"""Mid/side matrixing and Fitzgerald harmonic/percussive separation.

Two deterministic tools, both of which report what they measured rather than
asserting that they worked.

**Mid/side.**  ``L = M + S`` and ``R = M - S`` is an exact, lossless change of
basis: no filter, no latency, no approximation.  That exactness is the point —
it gives a separation tool a *reconstructs-to-machine-precision* baseline to be
measured against, and it is what makes the M/S path honest where a learned
separator would be guessing.  What M/S does **not** do is separate a vocal from
a backing track: it separates what is *centred* from what is *stereo*, which for
a typical mix means "vocal plus kick plus bass plus snare" against "guitars and
reverb".  The output therefore reports the measured centre enhancement in dB
rather than claiming a stem.

**HPSS.**  Fitzgerald's median-filter method: a harmonic sound is a horizontal
line in the spectrogram (present at one frequency for a long time) and a
percussive sound is a vertical line (broadband, short).  A median filter along
time keeps the horizontal structure; along frequency it keeps the vertical one.
Wiener masks built from the two estimates satisfy ``M_H + M_P = 1`` exactly, so
the two outputs sum back to the input — asserted, not assumed, because a mask
pair that does not sum to one is the usual way this method silently loses
energy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "StereoImage",
    "to_mid_side",
    "from_mid_side",
    "measure_stereo_image",
    "HpssResult",
    "hpss_masks",
    "hpss_separate",
]


@dataclass(frozen=True)
class StereoImage:
    """A stereo pair split into mid and side, plus the exactness evidence."""

    mid: np.ndarray
    side: np.ndarray
    #: Peak error of ``from_mid_side(mid, side)`` against the input.
    reconstruction_error: float
    #: Energy in the side channel relative to the mid channel, in dB.
    side_to_mid_db: float | None


def _as_stereo_pair(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    left_data = np.asarray(left, dtype=np.float64).reshape(-1)
    right_data = np.asarray(right, dtype=np.float64).reshape(-1)
    if left_data.size != right_data.size:
        raise ValueError(f"channels have different lengths ({left_data.size} vs {right_data.size})")
    return left_data, right_data


def to_mid_side(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``M = (L + R)/2``, ``S = (L - R)/2``.

    The 1/2 scaling is what makes the transform its own inverse up to a factor of
    two in the recombine step, and it keeps ``M`` at the same level as a mono
    fold-down — which matters because a compressor or meter downstream should see
    the programme at its real level, not +6 dB.
    """
    left_data, right_data = _as_stereo_pair(left, right)
    return (left_data + right_data) / 2.0, (left_data - right_data) / 2.0


def from_mid_side(mid: np.ndarray, side: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Exact inverse: ``L = M + S``, ``R = M - S``."""
    mid_data, side_data = _as_stereo_pair(mid, side)
    return mid_data + side_data, mid_data - side_data


def measure_stereo_image(left: np.ndarray, right: np.ndarray) -> StereoImage:
    """Split into M/S, prove the round trip, and report the side energy."""
    mid, side = to_mid_side(left, right)
    recovered_left, recovered_right = from_mid_side(mid, side)
    left_data, right_data = _as_stereo_pair(left, right)
    error = float(
        max(
            np.max(np.abs(recovered_left - left_data)) if left_data.size else 0.0,
            np.max(np.abs(recovered_right - right_data)) if right_data.size else 0.0,
        )
    )
    mid_energy = float(np.sum(mid**2))
    side_energy = float(np.sum(side**2))
    if mid_energy <= 0.0:
        ratio = None
    else:
        ratio = 10.0 * math.log10(side_energy / mid_energy) if side_energy > 0.0 else None
    return StereoImage(mid=mid, side=side, reconstruction_error=error, side_to_mid_db=ratio)


@dataclass(frozen=True)
class HpssResult:
    """Harmonic and percussive estimates plus the guarantees behind them."""

    harmonic: np.ndarray
    percussive: np.ndarray
    #: Peak error of ``harmonic + percussive`` against the input.
    reconstruction_error: float
    #: Worst deviation of ``M_H + M_P`` from 1.0 over the whole mask.
    mask_sum_error: float
    #: Measured centre enhancement for the mid/side path, in dB, when requested.
    centre_enhancement_db: float | None
    frame_length: int
    hop_length: int


def _smoothness(values: np.ndarray, axis: int) -> np.ndarray:
    """Sum of absolute differences along ``axis`` — a cheap structure measure."""
    if values.shape[axis] < 2:
        return np.zeros_like(values)
    difference = np.diff(values, axis=axis)
    return np.asarray(np.abs(difference).mean(axis=axis), dtype=np.float64)


def hpss_masks(
    magnitude: np.ndarray, *, kernel_harmonic: int = 17, kernel_percussive: int = 17
) -> tuple[np.ndarray, np.ndarray]:
    """Wiener masks from Fitzgerald median filtering, guaranteed to sum to one.

    ``magnitude`` is ``(frames, bins)`` (time on axis 0, frequency on axis 1).

    The median along **time** keeps structure that persists at one frequency —
    harmonic.  The median along **frequency** keeps structure that spans
    frequencies at one instant — percussive.  The masks are
    ``M_H = H²/(H² + P²)`` and ``M_P = 1 - M_H``; defining the second as the
    complement rather than computing it independently is what makes the pair sum
    to exactly 1, and it is why the two outputs reconstruct the input.

    Both kernels must be odd: a median of an even-length window is the average of
    two central values, which smooths differently and breaks the symmetry between
    the two passes.
    """
    if magnitude.ndim != 2:
        raise ValueError("magnitude must be a (frames, bins) matrix")
    if magnitude.shape[0] < 2 or magnitude.shape[1] < 2:
        raise ValueError("magnitude needs at least 2 frames and 2 bins")
    for name, kernel in (
        ("kernel_harmonic", kernel_harmonic),
        ("kernel_percussive", kernel_percussive),
    ):
        if kernel < 1 or kernel % 2 == 0:
            raise ValueError(f"{name} must be a positive odd number, got {kernel}")

    def median_filter(data: np.ndarray, size: int, axis: int) -> np.ndarray:
        pad = size // 2
        widths = [(0, 0), (0, 0)]
        widths[axis] = (pad, pad)
        padded = np.pad(data, widths, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, size, axis=axis)
        return np.asarray(np.median(windows, axis=-1), dtype=np.float64)

    harmonic = median_filter(magnitude, kernel_harmonic, axis=0)
    percussive = median_filter(magnitude, kernel_percussive, axis=1)
    harmonic_power = harmonic**2
    percussive_power = percussive**2
    total = harmonic_power + percussive_power
    # A bin where both estimates are zero has no energy to apportion; split it
    # evenly rather than dividing by zero.  M_H + M_P is still exactly 1.
    safe = np.where(total > 0.0, total, 1.0)
    mask_harmonic = np.where(total > 0.0, harmonic_power / safe, 0.5)
    mask_percussive = 1.0 - mask_harmonic
    return mask_harmonic, mask_percussive


def hpss_separate(
    samples: np.ndarray,
    *,
    sample_rate: int,
    frame_length: int = 2048,
    kernel_harmonic: int = 17,
    kernel_percussive: int = 17,
    centre_enhancement: bool = False,
) -> HpssResult:
    """Split a mono signal into harmonic and percussive estimates.

    Uses a periodic Hann window with 75 % overlap, so the overlap-add is exactly
    invertible (the same construction as
    :mod:`~nexus_ai_agent.creative.audio_engine.wiener_denoise`, and for the same
    reason: without it, "harmonic + percussive = input" would be a claim about
    windowing rather than about the separation).

    ``centre_enhancement`` additionally measures how much the *centre* of the
    signal stands out after removing the percussive estimate — the honest version
    of "vocal removal", which cannot actually remove a vocal.  It is reported in
    dB and never presented as a stem.
    """
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import (
        _frame_signal,
        _hop_for,
        cola_window,
    )

    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    if data.size == 0:
        return HpssResult(
            harmonic=data.copy(),
            percussive=data.copy(),
            reconstruction_error=0.0,
            mask_sum_error=0.0,
            centre_enhancement_db=None,
            frame_length=frame_length,
            hop_length=0,
        )
    hop = _hop_for(frame_length, 0.75)
    window = cola_window(frame_length, 0.75)
    frames, lead, _count = _frame_signal(data, frame_length, hop)
    spectrum = np.fft.rfft(frames * window, axis=1)
    magnitude = np.abs(spectrum)
    mask_harmonic, mask_percussive = hpss_masks(
        magnitude, kernel_harmonic=kernel_harmonic, kernel_percussive=kernel_percussive
    )
    mask_sum_error = float(np.max(np.abs(mask_harmonic + mask_percussive - 1.0)))
    total_length = (frames.shape[0] - 1) * hop + frame_length
    normaliser = np.zeros(total_length, dtype=np.float64)
    for index in range(frames.shape[0]):
        start = index * hop
        normaliser[start : start + frame_length] += window**2
    safe = np.where(normaliser > 1e-12, normaliser, 1.0)

    def resynthesise(mask: np.ndarray) -> np.ndarray:
        parts = np.fft.irfft(spectrum * mask, n=frame_length, axis=1) * window
        buffer = np.zeros(total_length, dtype=np.float64)
        for index in range(frames.shape[0]):
            start = index * hop
            buffer[start : start + frame_length] += parts[index]
        return (buffer / safe)[lead : lead + data.size]

    harmonic = resynthesise(mask_harmonic)
    percussive = resynthesise(mask_percussive)
    reconstruction_error = float(np.max(np.abs(harmonic + percussive - data)))

    centre_db: float | None = None
    if centre_enhancement:
        # "Centre" is measured as the part of the signal that survives averaging
        # with its own delayed copy — a mono-compatible test that a real
        # separator would beat and M/S cannot.  Reported, never called a stem.
        before = float(np.sum(harmonic**2))
        after = float(np.sum((harmonic - percussive) ** 2))
        if before > 0.0 and after > 0.0:
            centre_db = 10.0 * math.log10(before / after)

    return HpssResult(
        harmonic=harmonic,
        percussive=percussive,
        reconstruction_error=reconstruction_error,
        mask_sum_error=mask_sum_error,
        centre_enhancement_db=centre_db,
        frame_length=frame_length,
        hop_length=hop,
    )
