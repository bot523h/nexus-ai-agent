"""Temporal image stabilization kernels implemented with NumPy only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class PhaseCorrelationReceipt:
    translation_x_px: float
    translation_y_px: float
    peak_to_sidelobe_ratio: float


@dataclass(frozen=True, slots=True)
class TemporalDeflickerReceipt:
    frames: FloatArray
    log_luminance_before: tuple[float, ...]
    log_luminance_after: tuple[float, ...]
    temporal_flicker_variance_before: float
    temporal_flicker_variance_after: float
    gain_by_frame: tuple[float, ...]
    meets_variance_target: bool


@dataclass(frozen=True, slots=True)
class RollingShutterReceipt:
    frame: FloatArray
    max_warp_px: float
    readout_fraction: float


def _luminance(frame: FloatArray) -> FloatArray:
    if frame.ndim == 2:
        return frame
    if frame.ndim == 3 and frame.shape[2] in (1, 3, 4):
        if frame.shape[2] == 1:
            return frame[..., 0]
        return np.asarray(frame[..., :3] @ np.array([0.2126, 0.7152, 0.0722]), dtype=np.float64)
    raise KernelError("expected a grayscale or RGB(A) image")


def estimate_subpixel_translation_phase_corr(
    reference_frame: FloatArray,
    moving_frame: FloatArray,
    *,
    epsilon: float = 1e-12,
) -> PhaseCorrelationReceipt:
    """Estimate reference-to-moving (dx, dy) with a Hann-windowed FFT peak."""
    first = np.asarray(reference_frame, dtype=np.float64)
    second = np.asarray(moving_frame, dtype=np.float64)
    if (
        first.shape != second.shape
        or first.ndim not in (2, 3)
        or min(first.shape[:2]) < 3
        or np.any(~np.isfinite(first))
        or np.any(~np.isfinite(second))
        or epsilon <= 0
        or not np.isfinite(epsilon)
    ):
        raise KernelError("phase correlation needs matching finite frames and epsilon > 0")
    a, b = _luminance(first), _luminance(second)
    height, width = a.shape
    window = np.hanning(height)[:, None] * np.hanning(width)[None, :]
    a = (a - np.mean(a)) * window
    b = (b - np.mean(b)) * window
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    cross_spectrum = fa * np.conj(fb)
    cross = cross_spectrum / (np.abs(cross_spectrum) + epsilon)
    correlation = np.fft.ifft2(cross).real
    peak_y, peak_x = np.unravel_index(int(np.argmax(correlation)), correlation.shape)

    def parabolic(left: float, center: float, right: float) -> float:
        denominator = left - 2 * center + right
        return 0.5 * (left - right) / denominator if abs(denominator) > 1e-15 else 0.0

    offset_x = parabolic(
        float(correlation[peak_y, (peak_x - 1) % width]),
        float(correlation[peak_y, peak_x]),
        float(correlation[peak_y, (peak_x + 1) % width]),
    )
    offset_y = parabolic(
        float(correlation[(peak_y - 1) % height, peak_x]),
        float(correlation[peak_y, peak_x]),
        float(correlation[(peak_y + 1) % height, peak_x]),
    )
    shift_x = peak_x + offset_x
    shift_y = peak_y + offset_y
    if shift_x > width / 2:
        shift_x -= width
    if shift_y > height / 2:
        shift_y -= height
    sidelobes = correlation.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            sidelobes[(peak_y + dy) % height, (peak_x + dx) % width] = 0.0
    side_peak = float(np.max(np.abs(sidelobes)))
    ratio = float(abs(correlation[peak_y, peak_x]) / max(side_peak, epsilon))
    parabolic_x, parabolic_y = -float(shift_x), -float(shift_y)

    # The requested three-point parabolic estimate supplies a subpixel seed.
    # Refine its bias by fitting the unaliased central cross-spectrum phase
    # plane; this improves Fourier-shift fixtures while retaining the Hann
    # window and normalized phase-correlation peak as the primary estimator.
    frequency_y = np.fft.fftfreq(height)[:, None]
    frequency_x = np.fft.fftfreq(width)[None, :]
    unaliased_cutoff = min(0.25, 0.20 / max(1.0, abs(parabolic_x) + abs(parabolic_y)))
    magnitude = np.abs(cross_spectrum)
    phase_mask = (
        (np.abs(frequency_x) < unaliased_cutoff)
        & (np.abs(frequency_y) < unaliased_cutoff)
        & (magnitude > max(epsilon, float(magnitude.max()) * 1e-8))
    )
    if int(np.count_nonzero(phase_mask)) >= 6:
        phase = np.angle(cross_spectrum[phase_mask])
        design = (
            2
            * np.pi
            * np.column_stack(
                (
                    np.broadcast_to(frequency_x, a.shape)[phase_mask],
                    np.broadcast_to(frequency_y, a.shape)[phase_mask],
                )
            )
        )
        weights = np.sqrt(magnitude[phase_mask])
        try:
            refined, _, rank, _ = np.linalg.lstsq(
                design * weights[:, None], phase * weights, rcond=None
            )
            if rank == 2 and np.all(np.isfinite(refined)):
                parabolic_x, parabolic_y = float(refined[0]), float(refined[1])
        except np.linalg.LinAlgError:
            pass
    return PhaseCorrelationReceipt(parabolic_x, parabolic_y, ratio)


def _zero_phase_smooth(values: FloatArray, radius: int) -> FloatArray:
    if radius <= 0 or len(values) < 2:
        return values.copy()
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    sigma = max(0.5, radius / 2.0)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values, (radius, radius), mode="reflect")
    return np.convolve(padded, kernel, mode="valid")


def _temporal_flicker_variance(log_luminance: FloatArray) -> float:
    if log_luminance.size < 2:
        return 0.0
    frame_steps = np.diff(log_luminance)
    return float(np.var(frame_steps))


def stabilize_temporal_exposure_flicker(
    scene_linear_frames: FloatArray,
    *,
    smoothing_radius: int = 3,
    epsilon: float = 1e-8,
) -> TemporalDeflickerReceipt:
    """Stabilize frame-wise radiometry while retaining the zero-phase exposure trend."""
    frames = np.asarray(scene_linear_frames, dtype=np.float64)
    if (
        frames.ndim != 4
        or frames.shape[0] < 2
        or frames.shape[1] < 1
        or frames.shape[2] < 1
        or frames.shape[3] not in (1, 3, 4)
        or np.any(~np.isfinite(frames))
        or np.any(frames < 0)
        or smoothing_radius < 0
        or epsilon <= 0
        or not np.isfinite(epsilon)
    ):
        raise KernelError("deflicker needs finite nonnegative N x H x W x RGB(A) frames")
    trajectory = np.array(
        [np.median(np.log(_luminance(frame) + epsilon)) for frame in frames], dtype=np.float64
    )
    smoothed = _zero_phase_smooth(trajectory, smoothing_radius)
    gains = np.exp(smoothed - trajectory)
    stabilized = frames * gains[:, None, None, None]
    after_trajectory = trajectory + np.log(gains)
    before_variance = _temporal_flicker_variance(trajectory)
    after_variance = _temporal_flicker_variance(after_trajectory)
    meets = before_variance <= epsilon or after_variance < 0.20 * before_variance
    return TemporalDeflickerReceipt(
        np.asarray(stabilized, dtype=np.float64),
        cast(tuple[float, ...], tuple(float(value) for value in trajectory)),
        cast(tuple[float, ...], tuple(float(value) for value in after_trajectory)),
        before_variance,
        after_variance,
        cast(tuple[float, ...], tuple(float(value) for value in gains)),
        meets,
    )


def rectify_rolling_shutter_skew(
    frame: FloatArray,
    pan_velocity_px_per_second: tuple[float, ...],
    *,
    readout_duration_s: float,
    frame_period_s: float,
) -> RollingShutterReceipt:
    """Inverse-warp each scanline by its CMOS readout-time pan displacement."""
    source = np.asarray(frame, dtype=np.float64)
    velocity = np.asarray(pan_velocity_px_per_second, dtype=np.float64)
    if (
        source.ndim not in (2, 3)
        or source.shape[0] < 1
        or source.shape[1] < 1
        or (source.ndim == 3 and source.shape[2] not in (1, 3, 4))
        or np.any(~np.isfinite(source))
        or velocity.shape != (2,)
        or np.any(~np.isfinite(velocity))
        or not np.isfinite(readout_duration_s)
        or not np.isfinite(frame_period_s)
        or frame_period_s <= 0
        or not 0 <= readout_duration_s <= frame_period_s
    ):
        raise KernelError("invalid rolling-shutter frame, velocity, or readout duration")
    height, width = source.shape[:2]
    channels = 1 if source.ndim == 2 else source.shape[2]
    pixels = source[..., None] if source.ndim == 2 else source
    rows, columns = np.mgrid[0:height, 0:width]
    row_offset = rows / height - 0.5
    dx = velocity[0] * readout_duration_s * row_offset
    dy = velocity[1] * readout_duration_s * row_offset
    sample_x = np.clip(columns + dx, 0, width - 1)
    sample_y = np.clip(rows + dy, 0, height - 1)
    x0, y0 = np.floor(sample_x).astype(int), np.floor(sample_y).astype(int)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    wx, wy = sample_x - x0, sample_y - y0
    corrected = (
        pixels[y0, x0] * ((1 - wx) * (1 - wy))[..., None]
        + pixels[y0, x1] * (wx * (1 - wy))[..., None]
        + pixels[y1, x0] * ((1 - wx) * wy)[..., None]
        + pixels[y1, x1] * (wx * wy)[..., None]
    )
    if channels == 1 and source.ndim == 2:
        corrected = corrected[..., 0]
    return RollingShutterReceipt(
        np.asarray(corrected, dtype=np.float64),
        float(max(np.max(np.abs(dx)), np.max(np.abs(dy)))),
        readout_duration_s / frame_period_s,
    )
