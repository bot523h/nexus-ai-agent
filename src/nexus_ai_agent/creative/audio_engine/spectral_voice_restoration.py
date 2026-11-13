"""K2: MMSE log-spectral amplitude, constrained de-clipping and delayed WPE.

All transforms share the engine's periodic-Hann, exact weighted overlap-add
windowing. No clean reference is accepted by any processing algorithm. Metrics
are a separate function: a reference can evaluate an output, never train/tune
it at runtime. These are classical, bounded, offline estimators, not a claim
that arbitrary clipped/noisy/reverberant speech is exactly recoverable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from nexus_ai_agent.creative.audio_engine.wiener_denoise import (
    NOISE_FLOOR_POWER,
    _frame_signal,
    _hop_for,
    cola_window,
    estimate_noise_psd,
)


@dataclass(frozen=True)
class WolaFrames:
    spectrum: np.ndarray
    window: np.ndarray
    hop: int
    lead: int
    length: int


def _mono(samples: np.ndarray) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim != 1 or not np.isfinite(data).all():
        raise ValueError("restoration requires finite one-dimensional PCM")
    return data


def wola_analyse(
    samples: np.ndarray, *, frame_length: int = 1024, overlap: float = 0.75
) -> WolaFrames:
    data = _mono(samples)
    if not 16 <= frame_length <= 16384 or frame_length % 4:
        raise ValueError("frame_length must be a multiple of four in [16, 16384]")
    window = cola_window(frame_length, overlap)
    hop = _hop_for(frame_length, overlap)
    framed, lead, _count = _frame_signal(data, frame_length, hop)
    spectrum = np.fft.rfft(framed * window, axis=1)
    return WolaFrames(spectrum, window, hop, lead, data.size)


def wola_synthesise(spectrum: np.ndarray, frames: WolaFrames) -> np.ndarray:
    transformed = np.asarray(spectrum, dtype=np.complex128)
    if transformed.shape != frames.spectrum.shape or not np.isfinite(transformed).all():
        raise ValueError("synthesis spectrum must match the finite analysis geometry")
    if frames.length == 0:
        return np.zeros(0, dtype=np.float64)
    length = frames.window.size
    total = (transformed.shape[0] - 1) * frames.hop + length
    output, normalizer = np.zeros(total), np.zeros(total)
    windows = np.fft.irfft(transformed, n=length, axis=1) * frames.window
    for index, window in enumerate(windows):
        start = index * frames.hop
        output[start : start + length] += window
        normalizer[start : start + length] += frames.window**2
    output /= np.maximum(normalizer, 1e-20)
    return np.asarray(output[frames.lead : frames.lead + frames.length], dtype=np.float64)


def exponential_integral_e1(values: np.ndarray) -> np.ndarray:
    """E1(x), x>0, using a convergent series / Lentz rational (Padé) fraction.

    The series avoids cancellation below one. Above one the continued
    fraction converges without subtracting large exponentials. Both use
    float64, and the independently tabulated error is <3e-13 relative at the
    transition. exp(-x) underflow for x>~745 is the correct limiting zero.
    """
    x = np.asarray(values, dtype=np.float64)
    if not np.isfinite(x).all() or np.any(x <= 0):
        raise ValueError("E1 requires finite positive arguments")
    result = np.zeros_like(x)
    small = x <= 1
    a = x[small]
    term, series = -a.copy(), -a.copy()
    for k in range(2, 80):
        term *= -a / k
        update = term / k
        series += update
        if np.all(np.abs(update) < 2e-16):
            break
    result[small] = -np.euler_gamma - np.log(a) - series
    a = x[~small]
    b = a + 1
    c = np.full_like(a, 1e300)
    d = 1 / b
    h = d.copy()
    for k in range(1, 250):
        numerator = -float(k * k)
        b = b + 2
        d = 1 / (numerator * d + b)
        c = b + numerator / c
        delta = c * d
        h *= delta
        if np.all(np.abs(delta - 1) < 3e-15):
            break
    result[~small] = h * np.exp(-a)
    return result


def mmse_lsa_gain(
    prior_snr: np.ndarray, posterior_snr: np.ndarray, *, floor: float = 0.03
) -> np.ndarray:
    if not 0 < floor < 1:
        raise ValueError("LSA musical-noise gain floor must be in (0, 1)")
    xi, gamma = np.broadcast_arrays(np.asarray(prior_snr, float), np.asarray(posterior_snr, float))
    if (
        not np.isfinite(xi).all()
        or not np.isfinite(gamma).all()
        or np.any(xi < 0)
        or np.any(gamma < 0)
    ):
        raise ValueError("SNR estimates must be finite and nonnegative")
    ratio = xi / (1 + xi)
    v = np.maximum(ratio * gamma, 1e-12)
    gain = ratio * np.exp(0.5 * exponential_integral_e1(v))
    return np.asarray(np.clip(gain, floor, 1.0), dtype=np.float64)


@dataclass(frozen=True)
class LsaDenoiseResult:
    samples: np.ndarray
    noise_psd: np.ndarray
    mean_gain: np.ndarray
    reconstruction_error: float


def mmse_lsa_denoise(
    samples: np.ndarray,
    *,
    sample_rate: int,
    noise_reference: np.ndarray | None = None,
    frame_length: int = 1024,
    floor: float = 0.03,
    smoothing: float = 0.96,
    strength: float = 1.0,
) -> LsaDenoiseResult:
    """Decision-directed Ephraim-Malah LSA with stationary/minimum-statistics PSD.

    A supplied reference must be a *noise-only recording*, not clean speech.
    Otherwise the engine's low-percentile + spectral-neighborhood minimum
    initializes the noise PSD. Quiet-bin updates track a slowly varying floor
    without learning loud speech peaks as noise.
    """
    data = _mono(samples)
    if sample_rate <= 0 or not 0 < smoothing < 1 or not 0 <= strength <= 1 or not 0 < floor < 1:
        raise ValueError("invalid sample rate, smoothing, strength or gain floor")
    frames = wola_analyse(data, frame_length=frame_length)
    if data.size == 0:
        return LsaDenoiseResult(data.copy(), np.zeros(0), np.zeros(0), 0.0)
    power = np.abs(frames.spectrum) ** 2
    if noise_reference is None:
        noise = estimate_noise_psd(power, percentile=20)
    else:
        noise_frames = wola_analyse(noise_reference, frame_length=frame_length)
        if noise_frames.length < frame_length:
            raise ValueError("noise-only reference must contain at least one full frame")
        noise = np.maximum(np.mean(np.abs(noise_frames.spectrum) ** 2, axis=0), NOISE_FLOOR_POWER)
    initial_noise = noise.copy()
    previous = np.maximum(power[0] - noise, 0)
    gains = np.empty_like(power)
    for index, current in enumerate(power):
        gamma = np.clip(current / np.maximum(noise, NOISE_FLOOR_POWER), 0, 1e12)
        xi = smoothing * np.clip(previous / noise, 0, 1e12) + (1 - smoothing) * np.maximum(
            gamma - 1, 0
        )
        gain = mmse_lsa_gain(xi, gamma, floor=floor)
        gain = 1 - strength * (1 - gain)
        gains[index] = gain
        previous = gain**2 * current
        quiet = gamma < 1.5
        noise = np.where(quiet, 0.995 * noise + 0.005 * current, noise)
        noise = np.maximum(noise, NOISE_FLOOR_POWER)
    output = wola_synthesise(frames.spectrum * gains, frames)
    reconstruction_error = float(np.max(np.abs(wola_synthesise(frames.spectrum, frames) - data)))
    return LsaDenoiseResult(output, initial_noise, gains.mean(axis=0), reconstruction_error)


@dataclass(frozen=True)
class DeclipResult:
    samples: np.ndarray
    clipped_mask: np.ndarray
    iterations: int
    relative_change: float


def consistent_declip(
    samples: np.ndarray,
    *,
    sample_rate: int,
    threshold: float = 0.98,
    bandwidth_hz: float | None = None,
    iterations: int = 100,
) -> DeclipResult:
    """Band-limited trigonometric POCS, with exact known-sample/inequality clamps.

    Reliable samples are equality constraints. Positive/negative clipped
    samples remain respectively >=threshold / <=-threshold after every
    iteration. No normalization may undo those constraints. The caller must
    state a plausible bandwidth; destroyed information is not invented by
    feeding a clean reference to the reconstruction.
    """
    data = _mono(samples)
    cutoff = min(7000.0, sample_rate * 0.4) if bandwidth_hz is None else bandwidth_hz
    if sample_rate <= 0 or not 0 < threshold <= 1 or not 0 < cutoff < sample_rate / 2:
        raise ValueError("invalid clipping threshold, bandwidth or sample rate")
    if not 1 <= iterations <= 500:
        raise ValueError("de-clipping iterations must be in [1, 500]")
    positive, negative = data >= threshold - 1e-10, data <= -threshold + 1e-10
    missing = positive | negative
    if not np.any(missing):
        return DeclipResult(data.copy(), missing, 0, 0.0)
    if np.count_nonzero(~missing) < 4:
        raise ValueError("insufficient reliable samples for constrained de-clipping")
    # Reflection padding keeps the FFT's periodic boundary away from real ends.
    pad = min(data.size - 1, 1024)
    padded = np.pad(data, pad, mode="reflect")
    signs_pos = np.pad(positive, pad, mode="reflect")
    signs_neg = np.pad(negative, pad, mode="reflect")
    reliable = ~(signs_pos | signs_neg)
    frequencies = np.fft.rfftfreq(padded.size, 1 / sample_rate)
    passband = frequencies <= cutoff
    estimate = padded.copy()
    change = math.inf
    completed = 0
    for iteration in range(iterations):
        completed = iteration + 1
        projected = np.fft.irfft(np.fft.rfft(estimate) * passband, n=padded.size)
        projected[reliable] = padded[reliable]
        projected[signs_pos] = np.maximum(projected[signs_pos], threshold)
        projected[signs_neg] = np.minimum(projected[signs_neg], -threshold)
        change = float(np.linalg.norm(projected - estimate)) / max(
            float(np.linalg.norm(estimate)), 1e-20
        )
        estimate = projected
        if change < 1e-10:
            break
    return DeclipResult(estimate[pad : pad + data.size], missing, completed, change)


@dataclass(frozen=True)
class WpeResult:
    samples: np.ndarray
    prediction_delay_frames: int
    prediction_delay_ms: float
    residual_power: tuple[float, ...]


def wpe_dereverberate(
    samples: np.ndarray,
    *,
    sample_rate: int,
    taps: int = 6,
    prediction_delay_ms: float = 40.0,
    iterations: int = 3,
    frame_length: int = 1024,
    regularization: float = 1e-5,
) -> WpeResult:
    """Multichannel delayed weighted prediction error, with ridge-stable solves.

    At least 35 ms prediction delay preserves the early acoustic contribution.
    Frame power is re-estimated from the residual each iteration; no inverse
    reverberation filter or clean reference is smuggled in as prior evidence.
    """
    data = np.asarray(samples, dtype=np.float64)
    was_mono = data.ndim == 1
    if was_mono:
        data = data[None, :]
    if data.ndim != 2 or not 1 <= data.shape[0] <= 4 or not np.isfinite(data).all():
        raise ValueError("WPE input must be finite (1..4 channels, samples)")
    if sample_rate <= 0 or not 35 <= prediction_delay_ms <= 250:
        raise ValueError("WPE prediction delay must preserve at least 35 ms of early reflections")
    if not 1 <= taps <= 16 or not 1 <= iterations <= 10 or not 0 < regularization < 1:
        raise ValueError("WPE taps/iterations/regularization exceed the bounded solver")
    analyses = [wola_analyse(channel, frame_length=frame_length) for channel in data]
    hop = analyses[0].hop
    delay = math.ceil(prediction_delay_ms * sample_rate / (1000 * hop))
    actual_delay_ms = delay * hop * 1000 / sample_rate
    y = np.stack([analysis.spectrum for analysis in analyses], axis=0)
    start = delay + taps - 1
    if data.shape[1] < frame_length + start * hop or y.shape[1] <= start:
        # Reflection padding is synthesis context, not additional prediction
        # evidence. A sub-window clip must not train a filter on its own pad.
        return WpeResult(np.array(samples, dtype=np.float64, copy=True), delay, actual_delay_ms, ())
    # (frequency, frame, channel*tap); no wrap across time or channel axes.
    predictors = np.concatenate(
        [y[:, start - delay - tap : y.shape[1] - delay - tap, :] for tap in range(taps)], axis=0
    ).transpose(2, 1, 0)
    target = y[:, start:, :].transpose(2, 1, 0)
    estimate = target.copy()
    history: list[float] = []
    dimension = predictors.shape[2]
    for _iteration in range(iterations):
        variance = np.mean(np.abs(estimate) ** 2, axis=2)
        variance = np.maximum(
            variance, np.maximum(variance.mean(axis=1, keepdims=True) * 1e-5, 1e-20)
        )
        weighted = predictors / variance[:, :, None]
        covariance = np.einsum("ftd,fte->fde", predictors.conj(), weighted)
        correlation = np.einsum("ftd,ftc->fdc", weighted.conj(), target)
        ridge = regularization * np.maximum(
            np.trace(covariance, axis1=1, axis2=2).real / dimension, 1
        )
        covariance += ridge[:, None, None] * np.eye(dimension)
        coefficients = np.linalg.solve(covariance, correlation)
        estimate = target - np.einsum("ftd,fdc->ftc", predictors, coefficients)
        history.append(float(np.mean(np.abs(estimate) ** 2)))
    result_spectrum = y.copy()
    result_spectrum[:, start:, :] = estimate.transpose(2, 1, 0)
    output = np.stack(
        [wola_synthesise(result_spectrum[k], analyses[k]) for k in range(data.shape[0])]
    )
    preserve = min(data.shape[1], math.floor(0.035 * sample_rate))
    output[:, :preserve] = data[:, :preserve]
    return WpeResult(output[0] if was_mono else output, delay, actual_delay_ms, tuple(history))


@dataclass(frozen=True)
class RestorationMetrics:
    snr_before_db: float
    snr_after_db: float
    snr_gain_db: float
    reference_crest_db: float
    input_crest_db: float
    output_crest_db: float
    input_modulation_correlation: float
    output_modulation_correlation: float


def _snr(clean: np.ndarray, estimate: np.ndarray) -> float:
    signal = float(np.sum(clean**2))
    error = float(np.sum((clean - estimate) ** 2))
    return 10 * math.log10(max(signal, 1e-30) / max(error, 1e-30))


def _crest(samples: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(samples**2)))
    return 20 * math.log10(max(float(np.max(np.abs(samples))), 1e-20) / max(rms, 1e-20))


def modulation_correlation(
    reference: np.ndarray, estimate: np.ndarray, *, sample_rate: int
) -> float:
    clean, output = _mono(reference), _mono(estimate)
    if clean.shape != output.shape or clean.size == 0 or sample_rate <= 0:
        raise ValueError(
            "modulation comparison requires aligned, non-empty PCM and a positive rate"
        )
    hop = max(1, round(sample_rate * 0.01))
    count = clean.size // hop
    if count < 2:
        raise ValueError("at least two modulation frames are required")
    a = np.sqrt(np.mean(clean[: count * hop].reshape(count, hop) ** 2, axis=1))
    b = np.sqrt(np.mean(output[: count * hop].reshape(count, hop) ** 2, axis=1))
    a, b = a - a.mean(), b - b.mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.clip(a @ b / denominator, -1, 1)) if denominator > 1e-20 else 0.0


def restoration_metrics(
    reference: np.ndarray, degraded: np.ndarray, restored: np.ndarray, *, sample_rate: int
) -> RestorationMetrics:
    """Evaluation only. This reference is never forwarded into a processing path."""
    clean, before, after = _mono(reference), _mono(degraded), _mono(restored)
    if clean.size == 0 or clean.shape != before.shape or clean.shape != after.shape:
        raise ValueError("restoration metrics require aligned non-empty signals")
    snr_before, snr_after = _snr(clean, before), _snr(clean, after)
    return RestorationMetrics(
        snr_before,
        snr_after,
        snr_after - snr_before,
        _crest(clean),
        _crest(before),
        _crest(after),
        modulation_correlation(clean, before, sample_rate=sample_rate),
        modulation_correlation(clean, after, sample_rate=sample_rate),
    )


__all__ = [
    "DeclipResult",
    "LsaDenoiseResult",
    "RestorationMetrics",
    "WolaFrames",
    "WpeResult",
    "consistent_declip",
    "exponential_integral_e1",
    "mmse_lsa_denoise",
    "mmse_lsa_gain",
    "modulation_correlation",
    "restoration_metrics",
    "wola_analyse",
    "wola_synthesise",
    "wpe_dereverberate",
]
