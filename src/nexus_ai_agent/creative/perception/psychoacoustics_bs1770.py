"""ITU-R BS.1770-4 / EBU R128 loudness and Zwicker Bark psychoacoustics.

The K-weighting cascade is synthesised analytically for any sample rate in the
16-192 kHz range, then momentary/short-term/integrated loudness, LRA, true peak
and Bark spectral descriptors are derived from it.  Silence is measured, not
assumed: every value here is computed from samples.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "AudioAnalysisError",
    "BiquadCoefficients",
    "CHANNEL_GAINS",
    "bark_spectrum_centroid",
    "bark_scale",
    "biquad_process",
    "block_loudness",
    "design_k_weighting",
    "integrated_loudness",
    "k_weighting_filter",
    "loudness_range",
    "momentary_loudness",
    "short_term_loudness",
    "silence_segments",
    "true_peak_dbtp",
]

_SHELF_F0: Final[float] = 1681.974450955533
_SHELF_GAIN_DB: Final[float] = 3.999843853973347
_SHELF_Q: Final[float] = 0.7071752369554196
_SHELF_VB_EXPONENT: Final[float] = 0.4996667741545416
_HIGHPASS_F0: Final[float] = 38.13547087602444
_HIGHPASS_Q: Final[float] = 0.5003270373238773
_ABSOLUTE_GATE_LKFS: Final[float] = -70.0
_RELATIVE_GATE_LU: Final[float] = -10.0
_LRA_RELATIVE_GATE_LU: Final[float] = -20.0
_MIN_SAMPLE_RATE: Final[float] = 16000.0
_MAX_SAMPLE_RATE: Final[float] = 192000.0

# BS.1770-4 channel gains for L, R, C, Ls, Rs.
CHANNEL_GAINS: Final[tuple[float, ...]] = (1.0, 1.0, 1.0, 1.41, 1.41)


class AudioAnalysisError(ValueError):
    """Raised when audio input violates an acoustic precondition."""


@dataclass(frozen=True, slots=True)
class BiquadCoefficients:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    def __post_init__(self) -> None:
        for name in ("b0", "b1", "b2", "a1", "a2"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise AudioAnalysisError(f"biquad coefficient {name} is not finite")


def _validate_sample_rate(fs: float) -> float:
    if isinstance(fs, bool) or not isinstance(fs, (int, float)):
        raise TypeError("sample rate must be a real number")
    rate = float(fs)
    if not math.isfinite(rate):
        raise AudioAnalysisError("sample rate must be finite")
    if not _MIN_SAMPLE_RATE <= rate <= _MAX_SAMPLE_RATE:
        raise AudioAnalysisError(
            f"sample rate {rate} Hz is outside [{_MIN_SAMPLE_RATE}, {_MAX_SAMPLE_RATE}]"
        )
    return rate


def _validate_channels(channels: Sequence[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
    if not channels:
        raise AudioAnalysisError("at least one channel is required")
    lengths = []
    result: list[tuple[float, ...]] = []
    for channel in channels:
        values: list[float] = []
        for sample in channel:
            if isinstance(sample, bool) or not isinstance(sample, (int, float)):
                raise AudioAnalysisError("audio samples must be real numbers")
            value = float(sample)
            if not math.isfinite(value):
                raise AudioAnalysisError("audio samples must be finite")
            values.append(value)
        if not values:
            raise AudioAnalysisError("channels must not be empty")
        lengths.append(len(values))
        result.append(tuple(values))
    if len(set(lengths)) != 1:
        raise AudioAnalysisError("all channels must have the same number of samples")
    return tuple(result)


def _validate_gains(weights: Sequence[float] | None, channel_count: int) -> tuple[float, ...]:
    if weights is None:
        return (1.0,) * channel_count
    if len(weights) != channel_count:
        raise AudioAnalysisError("channel weight count does not match channel count")
    gains = []
    for weight in weights:
        value = float(weight)
        if not math.isfinite(value) or value < 0.0:
            raise AudioAnalysisError("channel weights must be finite and non-negative")
        gains.append(value)
    return tuple(gains)


def design_k_weighting(fs: float) -> tuple[BiquadCoefficients, BiquadCoefficients]:
    """Return the BS.1770-4 high-shelf and RLB high-pass biquads for ``fs``."""

    rate = _validate_sample_rate(fs)
    shelf_k = math.tan(math.pi * _SHELF_F0 / rate)
    vh = 10.0 ** (_SHELF_GAIN_DB / 20.0)
    vb = vh**_SHELF_VB_EXPONENT
    shelf_a0 = 1.0 + shelf_k / _SHELF_Q + shelf_k * shelf_k
    shelf = BiquadCoefficients(
        b0=(vh + vb * shelf_k / _SHELF_Q + shelf_k * shelf_k) / shelf_a0,
        b1=2.0 * (shelf_k * shelf_k - vh) / shelf_a0,
        b2=(vh - vb * shelf_k / _SHELF_Q + shelf_k * shelf_k) / shelf_a0,
        a1=2.0 * (shelf_k * shelf_k - 1.0) / shelf_a0,
        a2=(1.0 - shelf_k / _SHELF_Q + shelf_k * shelf_k) / shelf_a0,
    )
    hp_k = math.tan(math.pi * _HIGHPASS_F0 / rate)
    hp_a0 = 1.0 + hp_k / _HIGHPASS_Q + hp_k * hp_k
    highpass = BiquadCoefficients(
        b0=1.0 / hp_a0,
        b1=-2.0 / hp_a0,
        b2=1.0 / hp_a0,
        a1=2.0 * (hp_k * hp_k - 1.0) / hp_a0,
        a2=(1.0 - hp_k / _HIGHPASS_Q + hp_k * hp_k) / hp_a0,
    )
    return shelf, highpass


def biquad_process(coeffs: BiquadCoefficients, samples: Sequence[float]) -> tuple[float, ...]:
    """Direct-form-I biquad difference equation with unit sample delay state."""

    out: list[float] = []
    x1 = x2 = y1 = y2 = 0.0
    for sample in samples:
        value = (
            coeffs.b0 * sample + coeffs.b1 * x1 + coeffs.b2 * x2 - coeffs.a1 * y1 - coeffs.a2 * y2
        )
        x2, x1 = x1, sample
        y2, y1 = y1, value
        out.append(value)
    return tuple(out)


def k_weighting_filter(
    channels: Sequence[Sequence[float]], fs: float
) -> tuple[tuple[float, ...], ...]:
    shelf, highpass = design_k_weighting(fs)
    filtered = []
    for channel in _validate_channels(channels):
        filtered.append(biquad_process(highpass, biquad_process(shelf, channel)))
    return tuple(filtered)


def _block_energies(
    channels: Sequence[Sequence[float]],
    block_size: int,
    hop: int,
    gains: tuple[float, ...],
) -> tuple[float, ...]:
    sample_count = len(channels[0])
    energies: list[float] = []
    start = 0
    while start + block_size <= sample_count:
        total = 0.0
        for channel, gain in zip(channels, gains, strict=True):
            square_sum = 0.0
            for index in range(start, start + block_size):
                square_sum += channel[index] * channel[index]
            total += gain * (square_sum / block_size)
        energies.append(total)
        start += hop
    return tuple(energies)


def _loudness(energy: float) -> float:
    if energy <= 0.0:
        return -math.inf
    return -0.691 + 10.0 * math.log10(energy)


def _validate_block_args(fs: float, block_ms: float, overlap: float) -> tuple[float, int, int]:
    rate = _validate_sample_rate(fs)
    if not math.isfinite(block_ms) or block_ms <= 0.0:
        raise AudioAnalysisError("block_ms must be finite and positive")
    if not 0.0 <= overlap < 1.0:
        raise AudioAnalysisError("overlap must lie in [0, 1)")
    block_size = int(round(block_ms * rate / 1000.0))
    if block_size <= 0:  # pragma: no cover - positive block_ms guarantees >= 1 at >=16 kHz
        raise AudioAnalysisError("block size collapsed to zero")
    hop = max(1, int(round(block_size * (1.0 - overlap))))
    return rate, block_size, hop


def block_loudness(
    channels: Sequence[Sequence[float]],
    fs: float,
    *,
    block_ms: float = 400.0,
    overlap: float = 0.75,
    weights: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """K-weighted gating-block loudness in LKFS for the configured block size."""

    validated = _validate_channels(channels)
    _rate, block_size, hop = _validate_block_args(fs, block_ms, overlap)
    gains = _validate_gains(weights, len(validated))
    filtered = k_weighting_filter(validated, fs)
    return tuple(_loudness(energy) for energy in _block_energies(filtered, block_size, hop, gains))


def momentary_loudness(
    channels: Sequence[Sequence[float]], fs: float, *, weights: Sequence[float] | None = None
) -> tuple[float, ...]:
    return block_loudness(channels, fs, block_ms=400.0, overlap=0.75, weights=weights)


def short_term_loudness(
    channels: Sequence[Sequence[float]], fs: float, *, weights: Sequence[float] | None = None
) -> tuple[float, ...]:
    return block_loudness(channels, fs, block_ms=3000.0, overlap=2.0 / 3.0, weights=weights)


def integrated_loudness(
    channels: Sequence[Sequence[float]], fs: float, *, weights: Sequence[float] | None = None
) -> float:
    """Gated integrated loudness with absolute and relative thresholds."""

    validated = _validate_channels(channels)
    _rate, block_size, hop = _validate_block_args(fs, 400.0, 0.75)
    gains = _validate_gains(weights, len(validated))
    filtered = k_weighting_filter(validated, fs)
    energies = _block_energies(filtered, block_size, hop, gains)
    above_absolute = [energy for energy in energies if _loudness(energy) > _ABSOLUTE_GATE_LKFS]
    if not above_absolute:
        return -math.inf
    ungated = _loudness(sum(above_absolute) / len(above_absolute))
    relative_gate = ungated + _RELATIVE_GATE_LU
    above_relative = [energy for energy in above_absolute if _loudness(energy) > relative_gate]
    if not above_relative:  # pragma: no cover - the ungated mean is always >= this gate
        return -math.inf
    return _loudness(sum(above_relative) / len(above_relative))


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise AudioAnalysisError("cannot take a percentile of an empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def loudness_range(
    channels: Sequence[Sequence[float]], fs: float, *, weights: Sequence[float] | None = None
) -> float:
    """EBU R128 LRA: 10th-95th percentile spread of gated short-term loudness."""

    values = [
        value
        for value in short_term_loudness(channels, fs, weights=weights)
        if value > _ABSOLUTE_GATE_LKFS
    ]
    if len(values) < 2:
        return 0.0
    ungated = sum(values) / len(values)
    gated = sorted(value for value in values if value > ungated + _LRA_RELATIVE_GATE_LU)
    if len(gated) < 2:
        return 0.0
    return max(0.0, _percentile(gated, 0.95) - _percentile(gated, 0.10))


def _sinc(value: float) -> float:
    if value == 0.0:
        return 1.0
    argument = math.pi * value
    return math.sin(argument) / argument


def true_peak_dbtp(channels: Sequence[Sequence[float]], fs: float, *, oversample: int = 4) -> float:
    """Inter-sample true peak via windowed-sinc polyphase interpolation."""

    _validate_sample_rate(fs)
    if isinstance(oversample, bool) or not isinstance(oversample, int):
        raise TypeError("oversample must be an int")
    if not 1 <= oversample <= 16:
        raise AudioAnalysisError("oversample must lie in [1, 16]")
    validated = _validate_channels(channels)
    taps = 8
    peak = 0.0
    for channel in validated:
        count = len(channel)
        for sample in channel:
            peak = max(peak, abs(sample))
        if oversample == 1 or count < 2:
            continue
        for phase in range(1, oversample):
            fraction = phase / oversample
            for index in range(count):
                accumulator = 0.0
                for offset in range(-taps, taps + 1):
                    neighbour = index + offset
                    if 0 <= neighbour < count:
                        window = 0.5 + 0.5 * math.cos(math.pi * (offset - fraction) / (taps + 1))
                        accumulator += channel[neighbour] * _sinc(fraction - offset) * window
                peak = max(peak, abs(accumulator))
    if peak <= 0.0:
        return -math.inf
    return 20.0 * math.log10(peak)


def bark_scale(frequency_hz: float) -> float:
    """Zwicker/Traunmuller Bark transform."""

    if not math.isfinite(frequency_hz) or frequency_hz < 0.0:
        raise AudioAnalysisError("frequency must be finite and non-negative")
    return 13.0 * math.atan(0.00076 * frequency_hz) + 3.5 * math.atan((frequency_hz / 7500.0) ** 2)


def _fft(values: Sequence[complex]) -> list[complex]:
    count = len(values)
    if count == 0 or count & (count - 1):
        raise AudioAnalysisError("FFT length must be a non-zero power of two")
    data = list(values)
    index = 0
    for position in range(1, count):
        bit = count >> 1
        while index & bit:
            index ^= bit
            bit >>= 1
        index ^= bit
        if position < index:
            data[position], data[index] = data[index], data[position]
    length = 2
    while length <= count:
        angle = -2.0 * math.pi / length
        twiddle = complex(math.cos(angle), math.sin(angle))
        for start in range(0, count, length):
            factor = 1.0 + 0.0j
            for offset in range(length // 2):
                even = data[start + offset]
                odd = data[start + offset + length // 2] * factor
                data[start + offset] = even + odd
                data[start + offset + length // 2] = even - odd
                factor *= twiddle
        length <<= 1
    return data


def _power_spectrum(
    samples: Sequence[float], fs: float, n_fft: int
) -> tuple[tuple[float, float], ...]:
    rate = _validate_sample_rate(fs)
    if isinstance(n_fft, bool) or not isinstance(n_fft, int) or n_fft < 2:
        raise AudioAnalysisError("n_fft must be an int >= 2")
    if n_fft & (n_fft - 1):
        raise AudioAnalysisError("n_fft must be a power of two")
    windowed: list[complex] = []
    for index in range(n_fft):
        if index < len(samples):
            window = 0.5 - 0.5 * math.cos(2.0 * math.pi * index / (n_fft - 1))
            windowed.append(complex(samples[index] * window, 0.0))
        else:
            windowed.append(0j)
    transformed = _fft(windowed)
    bins: list[tuple[float, float]] = []
    for index in range(1, n_fft // 2 + 1):
        magnitude = transformed[index]
        power = (magnitude.real * magnitude.real + magnitude.imag * magnitude.imag) / n_fft
        bins.append((index * rate / n_fft, power))
    return tuple(bins)


def bark_spectrum_centroid(samples: Sequence[float], fs: float, *, n_fft: int = 1024) -> float:
    """Energy-weighted Bark centroid of the magnitude spectrum."""

    if not samples:
        raise AudioAnalysisError("samples must not be empty")
    total = 0.0
    weighted = 0.0
    for frequency, power in _power_spectrum(samples, fs, n_fft):
        total += power
        weighted += bark_scale(frequency) * power
    if total <= 0.0:
        return 0.0
    return weighted / total


def silence_segments(
    channels: Sequence[Sequence[float]],
    fs: float,
    *,
    threshold_dbfs: float = -60.0,
    min_ms: float = 200.0,
) -> tuple[tuple[float, float], ...]:
    """Return merged silent intervals (seconds) lasting at least ``min_ms``."""

    validated = _validate_channels(channels)
    rate = _validate_sample_rate(fs)
    if not math.isfinite(threshold_dbfs):
        raise AudioAnalysisError("threshold_dbfs must be finite")
    if not math.isfinite(min_ms) or min_ms < 0.0:
        raise AudioAnalysisError("min_ms must be finite and non-negative")
    frame = max(1, int(round(0.02 * rate)))
    hop = max(1, int(round(0.01 * rate)))
    sample_count = len(validated[0])
    silent_flags: list[tuple[float, float, bool]] = []
    start = 0
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    while start + frame <= sample_count:
        total = 0.0
        for channel in validated:
            for index in range(start, start + frame):
                total += channel[index] * channel[index]
        rms = math.sqrt(total / (frame * len(validated)))
        silent_flags.append((start / rate, (start + frame) / rate, rms <= threshold))
        start += hop
    segments: list[tuple[float, float]] = []
    active_start: float | None = None
    active_end = 0.0
    for begin, end, silent in silent_flags:
        if silent:
            if active_start is None:
                active_start = begin
            active_end = end
        elif active_start is not None:
            if (active_end - active_start) * 1000.0 >= min_ms:
                segments.append((active_start, active_end))
            active_start = None
    if active_start is not None and (active_end - active_start) * 1000.0 >= min_ms:
        segments.append((active_start, active_end))
    return tuple(segments)
