"""K3: sub-frame feature alignment and passive, contact-excited modal Foley.

Offset is audio minus picture: positive means late audio. The ITU production
window used here is [-15,+5] ms. An estimate is a feature measurement, not a
claim that arbitrary pictures identify their sound source. Visual motion must
be sampled densely enough to resolve the underlying event envelope.

Foley amplitudes are relative acoustic units, not calibrated pascals/SPL.
Impulse, contact duration, modal frequency and damping nevertheless have their
physical roles: a force pulse excites decaying resonators, linearly in impulse.
No recording, model, service, or hidden random global state is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

ITU_MIN_MS = -15.0
ITU_MAX_MS = 5.0
Material = Literal["wood", "metal", "glass", "stone", "leather_footstep", "cloth_rustle"]


def _pcm(samples: np.ndarray, *, matrix: bool = False) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float64)
    if (
        data.ndim not in ((1, 2) if matrix else (1,))
        or (data.ndim == 2 and data.shape[0] == 0)
        or not np.isfinite(data).all()
    ):
        raise ValueError("expected finite channel-first PCM/features")
    return data


@dataclass(frozen=True)
class OffsetEstimate:
    offset_samples: float
    offset_seconds: float
    peak_margin: float
    confidence_kind: str = "normalized_peak_margin_not_probability"


def gcc_phat(
    reference: np.ndarray,
    observed: np.ndarray,
    *,
    sample_rate: float,
    max_lag_seconds: float = 0.25,
    interpolation: int = 32,
) -> OffsetEstimate:
    """Linear GCC-PHAT with oversampled correlation and parabolic sub-bin fit.

    `observed(t) = reference(t-delay)` gives a positive delay. Spectral zeros
    are excluded, not whitened into infinite noise. FFT padding holds the full
    linear correlation; the bounded search cannot wrap the end to the start.
    """
    a, b = _pcm(reference), _pcm(observed)
    if (
        min(a.size, b.size) < 8
        or not math.isfinite(sample_rate)
        or sample_rate <= 0
        or not math.isfinite(max_lag_seconds)
        or not 0 < max_lag_seconds < min(a.size, b.size) / sample_rate / 2
        or not isinstance(interpolation, int)
        or not 2 <= interpolation <= 128
        or max_lag_seconds * sample_rate * interpolation < 1
    ):
        raise ValueError("insufficient features or invalid rate/lag/interpolation")
    # Removing the time MEAN before zero-padding creates a shared rectangular
    # edge on sparse envelopes; PHAT then confidently reports that fake zero
    # lag. Remove the endpoint baseline instead and discard only the DC bin.
    a, b = a - 0.5 * (a[0] + a[-1]), b - 0.5 * (b[0] + b[-1])
    size = 1 << (a.size + b.size - 1).bit_length()
    cross = np.fft.rfft(b, n=size) * np.fft.rfft(a, n=size).conj()
    cross[0] = 0
    magnitude = np.abs(cross)
    if float(magnitude.max()) < 1e-20:
        raise ValueError("constant/silent features have no measurable offset")
    usable = magnitude > magnitude.max() * 1e-8
    phase = np.divide(cross, magnitude, out=np.zeros_like(cross), where=usable)
    correlation = np.fft.irfft(phase, n=size * interpolation)
    maximum = min(
        round(max_lag_seconds * sample_rate * interpolation), size * interpolation // 2 - 1
    )
    bounded = np.concatenate((correlation[-maximum:], correlation[: maximum + 1]))
    index = int(np.argmax(bounded))
    fractional = 0.0
    if 0 < index < bounded.size - 1:
        left, center, right = bounded[index - 1 : index + 2]
        denominator = float(left - 2 * center + right)
        if abs(denominator) > 1e-20:
            fractional = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
    lag = (index - maximum + fractional) / interpolation
    competitors = bounded.copy()
    exclusion = 2 * interpolation
    competitors[max(0, index - exclusion) : index + exclusion + 1] = -np.inf
    second = float(np.max(competitors))
    peak = float(bounded[index])
    margin = float(np.clip((peak - max(0.0, second)) / max(abs(peak), 1e-20), 0, 1))
    return OffsetEstimate(lag, lag / sample_rate, margin)


def interpolate_visual_features(
    features: np.ndarray, *, frame_rate: float, envelope_rate: float, length: int
) -> np.ndarray:
    """Timestamped visual frames -> envelope grid, no integer-frame rounding.

    Piecewise cubic Hermite interpolation uses finite-difference slopes. It is
    exact at the actual frame timestamps and uses zero outside their support.
    Sub-frame estimation cannot reconstruct events above the visual Nyquist.
    """
    data = _pcm(features)
    if data.size < 4 or not np.isfinite([frame_rate, envelope_rate]).all():
        raise ValueError("at least four finite visual frames and finite rates are required")
    if frame_rate <= 0 or envelope_rate < frame_rate or length < 0:
        raise ValueError("invalid visual/envelope rate or output length")
    x = np.arange(length) * (frame_rate / envelope_rate)
    i = np.floor(x).astype(int)
    inside = (i >= 0) & (i < data.size - 1)
    i = np.clip(i, 0, data.size - 2)
    u = x - i
    slopes = np.gradient(data)
    interpolated = (
        (2 * u**3 - 3 * u**2 + 1) * data[i]
        + (u**3 - 2 * u**2 + u) * slopes[i]
        + (-2 * u**3 + 3 * u**2) * data[i + 1]
        + (u**3 - u**2) * slopes[i + 1]
    )
    interpolated[~inside] = 0.0
    return np.asarray(interpolated, dtype=np.float64)


def estimate_av_offset(
    visual_motion: np.ndarray,
    audio_envelope: np.ndarray,
    *,
    frame_rate: float,
    envelope_rate: float = 1000.0,
    max_lag_seconds: float = 0.25,
) -> OffsetEstimate:
    envelope = _pcm(audio_envelope)
    visual = interpolate_visual_features(
        visual_motion, frame_rate=frame_rate, envelope_rate=envelope_rate, length=envelope.size
    )
    return gcc_phat(visual, envelope, sample_rate=envelope_rate, max_lag_seconds=max_lag_seconds)


def within_itu_window(offset_ms: float) -> bool:
    if not math.isfinite(offset_ms):
        raise ValueError("offset must be finite milliseconds")
    return ITU_MIN_MS <= offset_ms <= ITU_MAX_MS


def fractional_time_shift(samples: np.ndarray, delay_samples: float) -> np.ndarray:
    """Symmetric windowed-sinc interpolation with zero extension, never roll()."""
    data = _pcm(samples, matrix=True)
    if not math.isfinite(delay_samples):
        raise ValueError("time shift must be finite")
    if data.size == 0:
        return data.copy()
    integer = math.floor(delay_samples)
    fraction = delay_samples - integer
    radius = 16
    taps = np.arange(-radius, radius + 1)
    kernel = np.sinc(taps - fraction) * np.kaiser(taps.size, 8.0)
    kernel /= kernel.sum()
    matrix = np.atleast_2d(data)
    output = np.zeros_like(matrix)
    for channel, values in enumerate(matrix):
        filtered = np.convolve(values, kernel, mode="full")[radius : radius + values.size]
        first, last = max(0, integer), min(values.size, values.size + integer)
        if last > first:
            output[channel, first:last] = filtered[first - integer : last - integer]
    return output[0] if data.ndim == 1 else output


def rms_envelope(
    samples: np.ndarray, *, sample_rate: int, envelope_rate: float = 1000.0
) -> np.ndarray:
    """Centered RMS windows timestamped at their centers (no hidden hop delay)."""
    data = np.atleast_2d(_pcm(samples, matrix=True))
    if not 0 < envelope_rate <= sample_rate or data.shape[1] == 0:
        raise ValueError("non-empty PCM and 0 < envelope_rate <= sample_rate are required")
    width = max(1, round(sample_rate / envelope_rate))
    power = np.mean(data**2, axis=0)
    smoothed = np.convolve(power, np.ones(width) / width, mode="full")
    centers = (
        np.arange(math.ceil(data.shape[1] * envelope_rate / sample_rate))
        * sample_rate
        / envelope_rate
    )
    return np.asarray(
        np.sqrt(
            np.maximum(np.interp(centers + (width - 1) / 2, np.arange(smoothed.size), smoothed), 0)
        ),
        dtype=np.float64,
    )


@dataclass(frozen=True)
class SyncResult:
    samples: np.ndarray
    input_offset_ms: float
    applied_delay_samples: float
    residual_offset_ms: float
    input_within_itu_window: bool


def synchronize_audio_to_visual(
    samples: np.ndarray,
    visual_motion: np.ndarray,
    *,
    sample_rate: int,
    frame_rate: float,
    envelope_rate: float = 1000.0,
    audio_feature: np.ndarray | None = None,
) -> SyncResult:
    """Estimate, advance/delay actual PCM, then remeasure the shifted feature.

    A supplied audio feature is a timestamp-aligned measured event envelope,
    not a clean sound reference. With none, centered RMS is extracted from PCM.
    The residual is measured again; it is not `estimate - correction` by fiat.
    """
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    feature = (
        rms_envelope(samples, sample_rate=sample_rate, envelope_rate=envelope_rate)
        if audio_feature is None
        else _pcm(audio_feature)
    )
    estimate = estimate_av_offset(
        visual_motion, feature, frame_rate=frame_rate, envelope_rate=envelope_rate
    )
    delay = -estimate.offset_seconds * sample_rate
    shifted = fractional_time_shift(samples, delay)
    shifted_feature = (
        rms_envelope(shifted, sample_rate=sample_rate, envelope_rate=envelope_rate)
        if audio_feature is None
        else fractional_time_shift(feature, -estimate.offset_samples)
    )
    residual = estimate_av_offset(
        visual_motion, shifted_feature, frame_rate=frame_rate, envelope_rate=envelope_rate
    )
    return SyncResult(
        shifted,
        estimate.offset_seconds * 1000,
        delay,
        residual.offset_seconds * 1000,
        within_itu_window(estimate.offset_seconds * 1000),
    )


@dataclass(frozen=True)
class ModalMaterial:
    frequencies_hz: tuple[float, ...]
    decay_seconds: tuple[float, ...]
    weights: tuple[float, ...]
    friction: bool = False


MATERIALS: dict[str, ModalMaterial] = {
    "wood": ModalMaterial(
        (180, 367, 641, 1123, 1987), (0.12, 0.09, 0.07, 0.045, 0.025), (1, 0.8, 0.6, 0.35, 0.15)
    ),
    "metal": ModalMaterial(
        (347, 713, 1289, 2477, 4561), (1.2, 1.0, 0.8, 0.6, 0.4), (1, 0.8, 0.7, 0.5, 0.3)
    ),
    "glass": ModalMaterial(
        (1021, 1879, 3251, 5107, 7121), (0.65, 0.55, 0.42, 0.28, 0.18), (1, 0.85, 0.7, 0.55, 0.4)
    ),
    "stone": ModalMaterial(
        (137, 283, 557, 1009, 1789), (0.08, 0.06, 0.04, 0.025, 0.015), (1, 0.7, 0.5, 0.3, 0.1)
    ),
    "leather_footstep": ModalMaterial(
        (73, 149, 311, 619, 1237), (0.07, 0.045, 0.03, 0.02, 0.012), (1, 0.6, 0.35, 0.15, 0.08)
    ),
    "cloth_rustle": ModalMaterial(
        (211, 433, 887, 1777, 3559, 6211),
        (0.045, 0.04, 0.035, 0.03, 0.025, 0.02),
        (0.2, 0.4, 0.6, 0.8, 1, 0.5),
        True,
    ),
}


def synthesize_modal_foley(
    material: str,
    *,
    sample_rate: int = 48000,
    impulse_ns: float = 1.0,
    contact_seconds: float = 0.001,
    duration_seconds: float = 1.0,
    seed: int = 0,
) -> np.ndarray:
    """Impulse/contact force convolved with damped modal displacement responses."""
    if material not in MATERIALS:
        raise ValueError("unknown Foley material")
    if (
        not 16000 <= sample_rate <= 192000
        or not np.isfinite([impulse_ns, contact_seconds, duration_seconds]).all()
    ):
        raise ValueError("invalid Foley rate or nonfinite physical parameters")
    if (
        not 0 < impulse_ns <= 100
        or not 0 <= contact_seconds <= 0.25
        or not 0.001 <= duration_seconds <= 5
    ):
        raise ValueError("Foley impulse/contact/duration exceed the bounded model")
    modes = MATERIALS[material]
    size = round(duration_seconds * sample_rate)
    t = np.arange(size) / sample_rate
    response = np.zeros(size)
    for frequency, decay, weight in zip(
        modes.frequencies_hz, modes.decay_seconds, modes.weights, strict=True
    ):
        response += weight * np.exp(-t / decay) * np.cos(2 * np.pi * frequency * t)
    response /= sum(modes.weights)
    count = max(1, round(contact_seconds * sample_rate))
    force = np.sin(np.pi * (np.arange(count) + 0.5) / count) ** 2
    if modes.friction:
        # Seeded microscopic contact variations excite the same passive modes.
        force *= np.random.default_rng(seed).uniform(0.05, 1.95, size=count)
    force *= impulse_ns / force.sum()
    fft_size = 1 << (size + count - 1).bit_length()
    output = np.fft.irfft(
        np.fft.rfft(response, n=fft_size) * np.fft.rfft(force, n=fft_size), n=fft_size
    )
    return np.asarray(output[:size], dtype=np.float64)


@dataclass(frozen=True)
class FoleyCollision:
    sample_index: int
    material: str
    impulse_ns: float = 1.0
    contact_seconds: float = 0.001
    duration_seconds: float = 1.0
    seed: int = 0


def render_collision_foley(
    collisions: tuple[FoleyCollision, ...], *, length_samples: int, sample_rate: int = 48000
) -> np.ndarray:
    """Place each collision at its integer sample; overlap-add, never normalize."""
    if length_samples < 0 or len(collisions) > 1000 or not 16000 <= sample_rate <= 192000:
        raise ValueError("invalid timeline length/rate or too many Foley collisions")
    output = np.zeros(length_samples)
    for collision in collisions:
        if (
            isinstance(collision.sample_index, bool)
            or not isinstance(collision.sample_index, int)
            or not 0 <= collision.sample_index < length_samples
        ):
            raise ValueError("collision must name a sample inside the timeline")
        sound = synthesize_modal_foley(
            collision.material,
            sample_rate=sample_rate,
            impulse_ns=collision.impulse_ns,
            contact_seconds=collision.contact_seconds,
            duration_seconds=collision.duration_seconds,
            seed=collision.seed,
        )
        stop = min(length_samples, collision.sample_index + sound.size)
        output[collision.sample_index : stop] += sound[: stop - collision.sample_index]
    return output


__all__ = [
    "FoleyCollision",
    "MATERIALS",
    "ModalMaterial",
    "OffsetEstimate",
    "SyncResult",
    "estimate_av_offset",
    "fractional_time_shift",
    "gcc_phat",
    "interpolate_visual_features",
    "render_collision_foley",
    "rms_envelope",
    "synchronize_audio_to_visual",
    "synthesize_modal_foley",
    "within_itu_window",
]
