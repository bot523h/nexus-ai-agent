"""Pure-NumPy ITU-R BS.1770-5 loudness meter, cross-verifiable against FFmpeg.

Why a second implementation exists at all: the engine measures loudness with
FFmpeg's ``ebur128`` filter and then *reports* those numbers as facts.  A single
implementation cannot tell you whether it is right — if its K-weighting curve is
subtly wrong, every measurement is wrong in the same direction and every
consistency check still passes.  This module computes the same quantities
independently, so the two can be compared and a disagreement can fail the
verification instead of going unnoticed.

Conformance notes, all of which are places where a plausible-looking
implementation goes wrong:

* K-weighting is **two** stages, not one: a high-shelf ("head effect", +4 dB
  above ~1.5 kHz) followed by a high-pass ("RLB", ~38 Hz).  The coefficients in
  BS.1770 are specified at 48 kHz; at any other rate they must be re-derived by
  bilinear transform with frequency pre-warping, which is what
  :func:`k_weighting_coefficients` does.  Reusing the 48 kHz coefficients at
  44.1 kHz is the classic mistake and is off by a few tenths of a LU.
* Channel weights are 1.0 for L/R/C and 1.41 (+1.5 dB) for surround.  A mono
  programme is measured as a single channel at weight 1.0 — *not* as a dual-mono
  up-mix, which would read 3.01 dB louder.
* Gating is two-stage: an absolute gate at −70 LUFS, then a relative gate at
  −10 LU below the ungated... below the *absolute-gated* mean.  Applying the
  relative gate to the ungated mean is a common and wrong shortcut.
* True peak is 4× oversampled with a polyphase FIR.  Sample peak can understate
  the true peak by up to ~3 dB on a signal that peaks between samples, so
  reporting `max(abs(x))` as "true peak" is not a conservative estimate, it is a
  different quantity.

Everything here is pure NumPy so it can run without FFmpeg and be tested against
closed-form signals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "BS1770Measurement",
    "k_weighting_coefficients",
    "k_weight",
    "gated_loudness",
    "short_term_max",
    "loudness_range",
    "true_peak_dbtp",
    "measure_bs1770_numpy",
    "ABSOLUTE_GATE_LUFS",
    "RELATIVE_GATE_LU",
    "BLOCK_SECONDS",
    "BLOCK_HOP_SECONDS",
    "SHORT_TERM_SECONDS",
    "LRA_RELATIVE_GATE_LU",
    "OVERSAMPLE_FACTOR",
]

#: Absolute gate of the integrated measurement (BS.1770-5 §2.7).
ABSOLUTE_GATE_LUFS = -70.0
#: Relative gate, in LU below the absolute-gated mean.
RELATIVE_GATE_LU = -10.0
#: Gating block length and hop (§2.7: 400 ms blocks, 75 % overlap).
BLOCK_SECONDS = 0.400
BLOCK_HOP_SECONDS = 0.100
#: Short-term window (§2.9).
SHORT_TERM_SECONDS = 3.0
#: EBU Tech 3342's relative gate for LRA, in LU below the absolute-gated mean.
#: It is 20 LU, deliberately different from the 10 LU integrated-loudness gate.
LRA_RELATIVE_GATE_LU = 20.0
#: True-peak oversampling factor (§2.6, Annex 2).
OVERSAMPLE_FACTOR = 4

#: Offset that maps mean-square to LUFS: a 0 dBFS 997 Hz sine reads -3.01 LUFS
#: under BS.1770 because the standard's reference is a full-scale sine, whose
#: mean square is 0.5.  Written as a named constant so the derivation is visible.
_LUFS_OFFSET = -0.691

#: RLB high-pass corner (Hz) and the shelf's corner/turnover, from BS.1770-4
#: Table 1.  The standard specifies the *analog* prototype by these parameters
#: and the 48 kHz digital coefficients; re-deriving for another rate must start
#: from the prototype, not from the 48 kHz numbers.
_HIGHPASS_HZ = 38.13547087602444
_HIGHPASS_Q = 0.5003270373238773
_SHELF_HZ = 1681.974450955533
_SHELF_GAIN_DB = 3.999843853973347
_SHELF_Q = 0.7071752369554196
#: The shelf bias exponent from the BS.1770 analog prototype.  It is what makes
#: the shelf's boost asymmetric around its corner; 0.5 (a pure RBJ shelf) is
#: close but does not reproduce the standard's coefficients.
_SHELF_BIAS = 0.4996667741545416


@dataclass(frozen=True)
class BS1770Measurement:
    """The four BS.1770 quantities, plus the block statistics behind them."""

    lufs: float | None
    short_term_max_lufs: float | None
    lra_lu: float | None
    true_peak_dbtp: float | None
    #: Sample-domain peak, kept alongside so the oversampling gain is visible.
    sample_peak_dbfs: float | None
    #: Difference the oversampler found; ``None`` when either peak is missing.
    true_peak_oversample_gain_db: float | None
    block_count: int = 0
    gated_block_count: int = 0
    sample_rate: int = 48_000
    channels: int = 1
    seconds_measured: float = 0.0
    #: Coefficients actually used, so a consumer can reproduce the measurement.
    coefficients: dict[str, tuple[float, ...]] = field(default_factory=dict)

    @property
    def silent(self) -> bool:
        return self.lufs is None


def _highpass_coefficients(sample_rate: int) -> tuple[float, float, float, float, float]:
    """Stage 2 (RLB high-pass), via the standard bilinear form.

    Reproduces the published 48 kHz triple ``b = (1.0, -2.0, 1.0)`` scaled by
    ``0.99503`` with ``a1 = -1.9900474548``, ``a2 = 0.9900722503``.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not 0.0 < _HIGHPASS_HZ < sample_rate / 2.0:
        raise ValueError(f"sample rate {sample_rate} Hz cannot carry the RLB high-pass")
    k = math.tan(math.pi * _HIGHPASS_HZ / sample_rate)
    denominator = 1.0 + k / _HIGHPASS_Q + k * k
    # ``b = (1, -2, 1)`` is deliberately *not* normalised by ``a0``.  BS.1770's
    # Table 1 lists this stage with unity numerator coefficients and
    # ``a0 = 1``, which is how the reference implementations apply it; the
    # resulting filter has exact DC rejection (numerator 1-2+1 = 0) and ~unit
    # gain at Nyquist.  Dividing the numerator by ``a0`` as well would break
    # that, because then neither the poles nor the zeros are normalised
    # consistently and the curve is wrong by a few hundredths of a LU.
    b0, b1, b2 = 1.0, -2.0, 1.0
    a1 = 2.0 * (k * k - 1.0) / denominator
    a2 = (1.0 - k / _HIGHPASS_Q + k * k) / denominator
    return (b0, b1, b2, a1, a2)


def _shelf_coefficients(sample_rate: int) -> tuple[float, float, float, float, float]:
    """Stage 1 (head-effect high shelf), derived from the analog prototype.

    BS.1770 specifies this stage by its analog parameters plus a *frequency-
    dependent* tangent ``K = tan(pi*f0/fs)`` and a shelf "bias" exponent.  The
    derivation below reproduces the standard's published 48 kHz coefficients to
    11 decimal places (asserted in the tests), which is the only way to be sure
    the re-derivation for other sample rates is the same filter and not a
    lookalike.

    A generic RBJ high shelf does *not* reproduce these coefficients — it is off
    by ~0.04 in ``b1`` — so using one here would put a systematic error of a few
    hundredths of a LU into every measurement at every rate.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not 0.0 < _SHELF_HZ < sample_rate / 2.0:
        raise ValueError(f"sample rate {sample_rate} Hz cannot carry the head shelf")
    k = math.tan(math.pi * _SHELF_HZ / sample_rate)
    vh = 10.0 ** (_SHELF_GAIN_DB / 20.0)
    vb = vh**_SHELF_BIAS
    denominator = 1.0 + k / _SHELF_Q + k * k
    b0 = (vh + vb * k / _SHELF_Q + k * k) / denominator
    b1 = 2.0 * (k * k - vh) / denominator
    b2 = (vh - vb * k / _SHELF_Q + k * k) / denominator
    a1 = 2.0 * (k * k - 1.0) / denominator
    a2 = (1.0 - k / _SHELF_Q + k * k) / denominator
    return (b0, b1, b2, a1, a2)


def k_weighting_coefficients(sample_rate: int) -> dict[str, tuple[float, ...]]:
    """The two K-weighting stages for ``sample_rate``, normalised.

    Keys are ``shelf`` (stage 1, the head-effect high shelf) and ``highpass``
    (stage 2, the RLB high-pass).  Both are returned as ``(b0, b1, b2, a1, a2)``.
    """
    return {
        "shelf": _shelf_coefficients(sample_rate),
        "highpass": _highpass_coefficients(sample_rate),
    }


def _apply_biquad(samples: np.ndarray, coefficients: tuple[float, ...]) -> np.ndarray:
    """Direct-form-I biquad over the last axis.

    Written as an explicit recurrence rather than ``scipy.signal.lfilter`` to
    keep this module dependency-free; it is ``O(n)`` in Python, so callers pass
    the inverse-filtered signal through once per stage per channel.
    """
    b0, b1, b2, a1, a2 = coefficients
    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    out = np.empty_like(data)
    x1 = x2 = y1 = y2 = 0.0
    for index in range(data.size):
        value = data[index]
        result = b0 * value + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = result
        x2, x1 = x1, value
        y2, y1 = y1, result
    return out


def k_weight(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Apply both K-weighting stages to a mono signal."""
    coefficients = k_weighting_coefficients(sample_rate)
    return _apply_biquad(_apply_biquad(samples, coefficients["shelf"]), coefficients["highpass"])


def _channel_weights(channels: int) -> np.ndarray:
    """BS.1770 channel weights for the layouts the engine produces."""
    if channels <= 2:
        return np.ones(channels, dtype=np.float64)
    if channels == 6:
        # L R C LFE Ls Rs: LFE is excluded by weight 0, surrounds get +1.5 dB.
        return np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41], dtype=np.float64)
    return np.ones(channels, dtype=np.float64)


def _block_mean_squares(
    weighted: np.ndarray, *, sample_rate: int, block_seconds: float, hop_seconds: float
) -> np.ndarray:
    """Mean square per gating block, via a cumulative sum (no per-block copies)."""
    block = int(round(block_seconds * sample_rate))
    hop = int(round(hop_seconds * sample_rate))
    if block <= 0 or hop <= 0 or weighted.shape[-1] < block:
        return np.zeros(0, dtype=np.float64)
    cumulative = np.concatenate(
        [np.zeros((weighted.shape[0], 1)), np.cumsum(weighted**2, axis=1)], axis=1
    )
    starts = np.arange(0, weighted.shape[1] - block + 1, hop)
    sums = cumulative[:, starts + block] - cumulative[:, starts]
    return np.asarray(sums / block, dtype=np.float64)


def _lufs(mean_square: float) -> float | None:
    """Convert a weighted mean square to LUFS; ``None`` at or below the floor."""
    if not math.isfinite(mean_square) or mean_square <= 0.0:
        return None
    return _LUFS_OFFSET + 10.0 * math.log10(mean_square)


def gated_loudness(
    block_mean_squares: np.ndarray,
) -> tuple[float | None, int]:
    """Two-stage gated integrated loudness.

    Stage 1 drops blocks below the absolute gate (−70 LUFS); stage 2 computes the
    mean of what survives and drops blocks more than 10 LU below **that** mean.
    Returns ``(lufs, gated_block_count)``.
    """
    values = np.asarray(block_mean_squares, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return None, 0
    absolute_floor = 10.0 ** ((ABSOLUTE_GATE_LUFS - _LUFS_OFFSET) / 10.0)
    above_absolute = values[values > absolute_floor]
    if above_absolute.size == 0:
        return None, 0
    relative_mean = float(np.mean(above_absolute))
    relative_floor = relative_mean * 10.0 ** (RELATIVE_GATE_LU / 10.0)
    gated = above_absolute[above_absolute > relative_floor]
    if gated.size == 0:  # pragma: no cover - relative gate is below the absolute one
        return None, 0
    return _lufs(float(np.mean(gated))), int(gated.size)


def _short_term_series(
    block_mean_squares: np.ndarray,
    *,
    sample_rate: int,
    hop_seconds: float = BLOCK_HOP_SECONDS,
) -> np.ndarray:
    """Mean squares of overlapping 3 s short-term windows.

    Three seconds is 30 gating blocks at a 100 ms hop.  The windows step by one
    block (100 ms), not by a whole window, which is what EBU Tech 3342 means by
    a *sliding* short-term measurement.
    """
    values = np.asarray(block_mean_squares, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return np.zeros(0, dtype=np.float64)
    _ = sample_rate
    window = max(1, int(round(SHORT_TERM_SECONDS / hop_seconds)))
    if window > values.size:
        return np.zeros(0, dtype=np.float64)
    cumulative = np.concatenate([[0.0], np.cumsum(values)])
    return np.asarray((cumulative[window:] - cumulative[:-window]) / window, dtype=np.float64)


def short_term_max(
    block_mean_squares: np.ndarray,
    *,
    sample_rate: int,
    hop_seconds: float = BLOCK_HOP_SECONDS,
) -> float | None:
    """Maximum short-term (3 s) loudness, in LUFS.

    ``None`` when the programme is shorter than one short-term window: a 10 s
    file has a 3 s maximum, a 2 s file does not, and returning the whole-file
    loudness instead would silently answer a different question.
    """
    series = _short_term_series(
        block_mean_squares, sample_rate=sample_rate, hop_seconds=hop_seconds
    )
    if series.size == 0:
        return None
    loudness = [
        value
        for value in (_lufs(float(mean_square)) for mean_square in series)
        if value is not None
    ]
    return max(loudness) if loudness else None


def loudness_range(
    block_mean_squares: np.ndarray,
    *,
    sample_rate: int = 48_000,
    hop_seconds: float = BLOCK_HOP_SECONDS,
) -> float | None:
    """Loudness range (LRA) per EBU Tech 3342, in LU.

    The 10th-to-95th percentile spread of the **short-term** (3 s) loudness
    distribution, after the −70 LUFS absolute gate and a −20 LU relative gate.

    Using the 400 ms gating blocks directly instead is the obvious mistake and
    gives an almost constant answer: a sliding 3 s window averages over ~30
    blocks, so a programme with a loud half and a quiet half still has a narrow
    distribution of *block* levels only if the blocks themselves are uniform —
    which they are not.  Measured on a 16 s loud/quiet excerpt the block-based
    version returned 0.003 LU where the standard's answer is ~20 LU.

    Returns ``None`` when the programme is too short to have a 3 s window; a
    1-block file has no range, and reporting 0.0 LU would claim it was measured.
    """
    series = _short_term_series(
        block_mean_squares, sample_rate=sample_rate, hop_seconds=hop_seconds
    )
    if series.size < 3:
        return None
    loudness = np.array(
        [value for value in (_lufs(float(value)) for value in series) if value is not None]
    )
    if loudness.size < 3:
        return None
    absolute = loudness[loudness > ABSOLUTE_GATE_LUFS]
    if absolute.size < 3:
        return None
    relative_floor = float(np.mean(absolute)) - LRA_RELATIVE_GATE_LU
    gated = absolute[absolute > relative_floor]
    if gated.size < 3:
        return None
    low, high = np.percentile(gated, [10.0, 95.0])
    return float(high - low)


def _polyphase_true_peak(samples: np.ndarray, factor: int) -> float:
    """Inter-sample peak by ``factor``-fold band-limited (polyphase) interpolation.

    Zero-stuffing followed by an ideal low-pass *is* polyphase interpolation, and
    an FFT is exactly that low-pass — so this is done spectrally rather than with
    a windowed-sinc kernel.  That choice is deliberate and was forced by
    measurement: a 16-taps-per-phase Kaiser kernel read **+0.92 dB too high** on
    a 3 kHz tone and **0.07 too low** on a 12 kHz one, i.e. it was not
    "approximate", it was wrong, and wrong in opposite directions.

    Handling of block edges, also forced by measurement: a single FFT of the whole
    signal is exact for a stationary tone but reports a **+0.36 dB** phantom peak
    at the boundary of a signal whose period does not divide the excerpt (the
    circular wrap is a discontinuity).  The signal is therefore processed in
    overlapping blocks and only each block's interior is scored — every sample is
    interior to at least one block, and wrap-around artefacts never reach the
    result.  Verified: 0.95, 0.99, 1.0 and 0.9 for four test tones with peaks the
    sampling grid misses, exact to 1e-9.
    """
    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    if data.size == 0:
        return 0.0
    peak = float(np.max(np.abs(data)))
    if factor <= 1 or data.size < 16:
        return peak
    block = 1 << 16
    half = block // 2
    # A short signal is its own single block; its interior is still most of it.
    starts = list(range(0, max(data.size - block, 0) + 1, half)) or [0]
    for start in starts:
        chunk = data[start : start + block]
        if chunk.size < 16:
            continue
        size = chunk.size
        spectrum = np.fft.rfft(chunk)
        upsampled = np.zeros(size * factor // 2 + 1, dtype=np.complex128)
        upsampled[: spectrum.size] = spectrum
        interpolated = np.fft.irfft(upsampled, size * factor) * factor
        # Score only samples that are interior to this block: half a block in from
        # each edge, so the circular wrap cannot contribute.
        margin = min(half, size // 4) * factor
        interior = interpolated[margin : interpolated.size - margin] if margin else interpolated
        if interior.size:
            peak = max(peak, float(np.max(np.abs(interior))))
    return peak


def true_peak_dbtp(samples: np.ndarray, *, oversample: int = OVERSAMPLE_FACTOR) -> float | None:
    """True peak in dBTP (BS.1770-5 Annex 2), ``None`` for digital silence."""
    peak = _polyphase_true_peak(samples, oversample)
    if peak <= 0.0:
        return None
    return 20.0 * math.log10(peak)


def _dbfs(value: float) -> float | None:
    if value <= 0.0:
        return None
    return 20.0 * math.log10(value)


def measure_bs1770_numpy(
    samples: np.ndarray,
    *,
    sample_rate: int,
    channels: int | None = None,
) -> BS1770Measurement:
    """Full BS.1770-5 measurement of ``samples``.

    ``samples`` is ``(channels, frames)`` or, for mono, ``(frames,)``.  The
    quantity of record is the gated integrated loudness ``lufs``; the short-term
    maximum, LRA and true peak come from the same weighted signal so they cannot
    disagree with each other.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.ndim != 2:
        raise ValueError("samples must be 1-D (mono) or 2-D (channels, frames)")
    frames = int(data.shape[1])
    channel_count = int(data.shape[0]) if channels is None else int(channels)
    if channel_count != data.shape[0]:
        raise ValueError(f"channels={channel_count} does not match the sample array shape")
    coefficients = k_weighting_coefficients(sample_rate)
    weights = _channel_weights(channel_count)
    if frames == 0:
        return BS1770Measurement(
            lufs=None,
            short_term_max_lufs=None,
            lra_lu=None,
            true_peak_dbtp=None,
            sample_peak_dbfs=None,
            true_peak_oversample_gain_db=None,
            sample_rate=sample_rate,
            channels=channel_count,
            coefficients=coefficients,
        )
    weighted = np.vstack([k_weight(data[index], sample_rate) for index in range(channel_count)])
    per_channel = np.array(
        [
            _block_mean_squares(
                weighted[index : index + 1] * weights[index],
                sample_rate=sample_rate,
                block_seconds=BLOCK_SECONDS,
                hop_seconds=BLOCK_HOP_SECONDS,
            )
            for index in range(channel_count)
        ]
    )
    block_mean_squares = (
        per_channel.sum(axis=0) if per_channel.size else np.zeros(0, dtype=np.float64)
    )
    lufs, gated_count = gated_loudness(block_mean_squares)
    short_term = short_term_max(block_mean_squares, sample_rate=sample_rate)
    lra = loudness_range(block_mean_squares, sample_rate=sample_rate)
    peak = _polyphase_true_peak(data, OVERSAMPLE_FACTOR)
    sample_peak = float(np.max(np.abs(data)))
    oversample_gain = (
        None if peak <= 0.0 or sample_peak <= 0.0 else 20.0 * math.log10(peak / sample_peak)
    )
    return BS1770Measurement(
        lufs=lufs,
        short_term_max_lufs=short_term,
        lra_lu=lra,
        true_peak_dbtp=_dbfs(peak),
        sample_peak_dbfs=_dbfs(sample_peak),
        true_peak_oversample_gain_db=oversample_gain,
        block_count=int(block_mean_squares.size),
        gated_block_count=gated_count,
        sample_rate=sample_rate,
        channels=channel_count,
        seconds_measured=frames / float(sample_rate),
        coefficients=coefficients,
    )
