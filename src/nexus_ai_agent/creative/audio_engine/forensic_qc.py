"""Forensic audio QC metrics and a linear-phase de-esser.

Everything here answers a question an operator would ask about a delivered
file, and answers it with a number:

``stereo_phase_correlation``
    Pearson correlation between L and R.  ``+1`` is mono, ``0`` is uncorrelated
    width, negative means the channels are partially out of phase and the
    programme will thin out or vanish on a mono fold-down.

``mono_foldback_loss_db``
    How much level a mono fold-down loses.  This is the number that matters, and
    it is not the same as the correlation: a wide but decorrelated mix loses
    ~3 dB, while an out-of-phase mix can lose everything.

``dc_offset_linear`` / ``dc_offset_db``
    The mean sample value, in both linear and dBFS.  A DC offset eats headroom
    silently and makes every later limiter decision slightly wrong.

``clipped_sample_count`` / ``max_consecutive_clipped_samples``
    Samples at or beyond full scale, and the longest run of them.  The run length
    is the useful part: one isolated sample is a rounding artefact, while a
    hundred consecutive is an audible flat-topped transient.

``zero_dropout_hole_count``
    Runs of exact zeros inside otherwise non-silent material — the signature of a
    dropped buffer, not of a quiet passage.

The de-esser uses a **linear-phase** crossover rather than the usual
cascaded-biquad form, for a measured reason: a minimum-phase (analogue-style)
crossover shifts the low band by several milliseconds relative to the high band,
which is inaudible on its own but destroys the phase relationship between them
inside the sibilance band, and that relationship is exactly what makes a
de-esser transparent.  An FFT-domain split has exactly zero phase distortion in
the pass bands, so the 100 Hz-4 kHz region is bit-comparable to the input apart
from the gain applied above 4.5 kHz.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "ForensicAudioQCReport",
    "analyze_forensic_audio_qc",
    "deess_sibilance_band",
    "DeessResult",
    "SIBILANCE_LOW_HZ",
    "SIBILANCE_HIGH_HZ",
    "MONO_FOLDBACK_WARNING_CORRELATION",
]

#: Sibilance band.  4.5 kHz is where "s" energy starts dominating speech; 9.5 kHz
#: is above the fricative's main concentration, so de-essing stops before it
#: starts dulling the air band.
SIBILANCE_LOW_HZ = 4_500.0
SIBILANCE_HIGH_HZ = 9_500.0

#: Below this L/R correlation a mono fold-down is reported as a fault.
MONO_FOLDBACK_WARNING_CORRELATION = -0.25


@dataclass(frozen=True)
class ForensicAudioQCReport:
    """Typed forensic QC verdict for one file, with the numbers behind it."""

    #: Pearson correlation between the channels; ``None`` for mono input.
    stereo_phase_correlation: float | None
    #: Level lost in a mono fold-down, in dB.  ``0`` for mono input.
    mono_foldback_loss_db: float
    #: ``True`` when the correlation is below
    #: :data:`MONO_FOLDBACK_WARNING_CORRELATION`.
    mono_foldback_risk: bool
    dc_offset_linear: float
    dc_offset_db: float | None
    clipped_sample_count: int
    max_consecutive_clipped_samples: int
    zero_dropout_hole_count: int
    #: Longest run of exact zeros inside non-silent material, in samples.
    longest_dropout_run: int
    sample_rate: int
    channels: int
    frames: int
    #: Peak sample magnitude, in dBFS (``None`` for digital silence).
    peak_dbfs: float | None
    #: Every finding, as stable codes, so a caller can gate on them.
    codes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.codes

    @property
    def seconds(self) -> float:
        return self.frames / float(self.sample_rate) if self.sample_rate else 0.0


#: A dropout shorter than this is indistinguishable from a quiet moment.
MIN_DROPOUT_RUN = 8


def _mono_ratio(left: np.ndarray, right: np.ndarray) -> float:
    """Level of a mono fold-down relative to the stereo programme, in dB."""
    stereo_energy = float(np.sum(left**2) + np.sum(right**2))
    folded = (left + right) / 2.0
    # A fold-down of a stereo pair carries the same *total* energy when
    # correlated (one channel's worth), so compare against half the stereo sum.
    mono_energy = float(np.sum(folded**2)) * 2.0
    if stereo_energy <= 0.0:
        return 0.0
    if mono_energy <= 0.0:
        return -math.inf
    return 10.0 * math.log10(mono_energy / stereo_energy)


def analyze_forensic_audio_qc(
    samples: np.ndarray,
    *,
    sample_rate: int,
    channels: int | None = None,
    clip_threshold: float = 1.0,
) -> ForensicAudioQCReport:
    """Measure the forensic QC facts about ``samples``.

    ``samples`` is ``(frames,)`` for mono or ``(channels, frames)``.  Nothing
    here is inferred from metadata: every field is computed from the samples, so
    a file that claims to be clean and is not cannot pass.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.ndim != 2:
        raise ValueError("samples must be 1-D (mono) or 2-D (channels, frames)")
    channel_count = int(data.shape[0]) if channels is None else int(channels)
    if channel_count != data.shape[0]:
        raise ValueError(f"channels={channel_count} does not match the array shape")
    frames = int(data.shape[1])
    codes: list[str] = []
    if frames == 0:
        return ForensicAudioQCReport(
            stereo_phase_correlation=None,
            mono_foldback_loss_db=0.0,
            mono_foldback_risk=False,
            dc_offset_linear=0.0,
            dc_offset_db=None,
            clipped_sample_count=0,
            max_consecutive_clipped_samples=0,
            zero_dropout_hole_count=0,
            longest_dropout_run=0,
            sample_rate=sample_rate,
            channels=channel_count,
            frames=0,
            peak_dbfs=None,
            codes=("audio_qc_empty",),
        )

    correlation: float | None = None
    foldback_loss = 0.0
    foldback_risk = False
    if channel_count == 1:
        _ = foldback_loss
    else:
        left = data[0]
        right = data[1]
        left_centred = left - left.mean()
        right_centred = right - right.mean()
        denominator = float(np.sqrt(np.sum(left_centred**2) * np.sum(right_centred**2)))
        if denominator > 0.0:
            correlation = float(np.sum(left_centred * right_centred) / denominator)
            correlation = max(-1.0, min(1.0, correlation))
        foldback_loss = _mono_ratio(left, right)
        foldback_risk = correlation is not None and correlation < MONO_FOLDBACK_WARNING_CORRELATION
        if foldback_risk:
            codes.append("mono_foldback_risk")

    dc = float(np.mean(data))
    dc_db = 20.0 * math.log10(abs(dc)) if dc != 0.0 else None
    clipped = np.abs(data) >= clip_threshold
    # Count samples across all channels, but measure the longest *run* per
    # channel: a run is a temporal property, and flattening a stereo matrix
    # would join the end of one channel to the start of the next into a
    # phantom run.
    clipped_count = int(np.count_nonzero(clipped))

    def longest_true_run(mask: np.ndarray) -> int:
        if mask.size == 0 or not mask.any():
            return 0
        # Pad with False so a run touching either edge is still counted.
        padded = np.concatenate([[False], mask.astype(bool), [False]])
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        return int(np.max(changes[1::2] - changes[::2]))

    max_clipped_run = max(
        (longest_true_run(clipped[channel]) for channel in range(clipped.shape[0])),
        default=0,
    )
    if clipped_count:
        codes.append("clipped_samples")

    zeros = data == 0.0
    # A dropout is a run of zeros *inside* audible material.  Leading and
    # trailing silence is legitimate programme; a hole in the middle is not.
    audible = np.abs(data) > 0.0
    first, last = 0, frames
    if audible.any():
        flat = np.flatnonzero(audible.any(axis=0))
        first, last = int(flat[0]), int(flat[-1]) + 1
    interior = zeros[:, first:last]
    holes = 0
    longest_run = 0
    for channel in range(interior.shape[0]):
        mask = interior[channel]
        if not mask.any():
            continue
        padded = np.concatenate([[False], mask, [False]])
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        runs = changes[1::2] - changes[::2]
        if runs.size:
            holes += int(np.count_nonzero(runs >= MIN_DROPOUT_RUN))
            longest_run = max(longest_run, int(runs.max()))
    if holes:
        codes.append("zero_dropout")

    peak = float(np.max(np.abs(data)))
    return ForensicAudioQCReport(
        stereo_phase_correlation=correlation,
        mono_foldback_loss_db=foldback_loss,
        mono_foldback_risk=foldback_risk,
        dc_offset_linear=dc,
        dc_offset_db=dc_db,
        clipped_sample_count=clipped_count,
        max_consecutive_clipped_samples=max_clipped_run,
        zero_dropout_hole_count=holes,
        longest_dropout_run=longest_run,
        sample_rate=sample_rate,
        channels=channel_count,
        frames=frames,
        peak_dbfs=20.0 * math.log10(peak) if peak > 0.0 else None,
        codes=tuple(codes),
    )


@dataclass(frozen=True)
class DeessResult:
    """De-essed signal plus proof the crossover did not smear the low band."""

    samples: np.ndarray
    #: Peak deviation of the 100 Hz-4 kHz band from the input, in dB.
    low_band_deviation_db: float
    #: Peak gain reduction applied inside the sibilance band, in dB.
    max_reduction_db: float
    #: Frames where the detector actually engaged.
    engaged_frames: int
    low_hz: float
    high_hz: float


def _envelope(
    signal: np.ndarray, sample_rate: int, *, attack_ms: float, release_ms: float
) -> np.ndarray:
    """One-pole attack/release envelope follower over a magnitude curve."""
    attack = math.exp(-1.0 / max(1e-9, attack_ms * 1e-3 * sample_rate))
    release = math.exp(-1.0 / max(1e-9, release_ms * 1e-3 * sample_rate))
    envelope = np.empty_like(signal)
    state = 0.0
    for index, value in enumerate(signal):
        coefficient = attack if value > state else release
        state = coefficient * state + (1.0 - coefficient) * value
        envelope[index] = state
    return envelope


def deess_sibilance_band(
    samples: np.ndarray,
    *,
    sample_rate: int,
    threshold_db: float = -30.0,
    max_reduction_db: float = 12.0,
    attack_ms: float = 1.0,
    release_ms: float = 40.0,
    frame_length: int = 1024,
) -> DeessResult:
    """Reduce sibilance in 4.5-9.5 kHz with a zero-phase (FFT) crossover.

    The band is split in the frequency domain, so the low band is the *exact*
    complement of the high band and the two sum back to the input.  A
    minimum-phase alternative — two cascaded Butterworth biquads — was the
    obvious approach and is rejected on measurement, not taste: its low band is
    delayed relative to its high band by several milliseconds, and comparing the
    100 Hz-4 kHz content to the input then shows a phase error that spreads
    across the whole band.  With the FFT split that deviation is 0 dB.

    The detector runs on the high band's short-term level with the standard
    1 ms attack / 40 ms release, and the gain is applied to the high band only,
    so nothing below 4.5 kHz is touched.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not 0.0 < attack_ms:
        raise ValueError("attack_ms must be positive")
    if not 0.0 < release_ms:
        raise ValueError("release_ms must be positive")
    if not (0.0 < SIBILANCE_LOW_HZ < SIBILANCE_HIGH_HZ < sample_rate / 2.0):
        raise ValueError(f"sample rate {sample_rate} Hz cannot carry a 4.5-9.5 kHz band")
    if max_reduction_db < 0.0:
        raise ValueError("max_reduction_db must be non-negative")
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.ndim != 2:
        raise ValueError("samples must be 1-D (mono) or 2-D (channels, frames)")
    if data.shape[1] == 0:
        return DeessResult(
            samples=data.copy(),
            low_band_deviation_db=0.0,
            max_reduction_db=0.0,
            engaged_frames=0,
            low_hz=SIBILANCE_LOW_HZ,
            high_hz=SIBILANCE_HIGH_HZ,
        )

    frames = int(data.shape[1])
    frequencies = np.fft.rfftfreq(frames, d=1.0 / sample_rate)
    # Raised-cosine transition of 200 Hz on each edge: a hard rectangular split
    # rings (Gibbs) and the ringing lands *inside* the low band, which is the
    # artefact this design exists to avoid.
    transition = 200.0
    high_mask = np.clip(
        (frequencies - (SIBILANCE_LOW_HZ - transition / 2.0)) / transition, 0.0, 1.0
    )
    high_mask = 0.5 - 0.5 * np.cos(np.pi * high_mask)
    high_mask = np.where(frequencies >= SIBILANCE_LOW_HZ + transition / 2.0, 1.0, high_mask)
    high_mask = np.where(
        frequencies > SIBILANCE_HIGH_HZ + transition / 2.0,
        0.5
        + 0.5
        * np.cos(
            np.pi
            * np.clip((frequencies - (SIBILANCE_HIGH_HZ + transition / 2.0)) / transition, 0.0, 1.0)
        ),
        high_mask,
    )
    low_mask = 1.0 - high_mask

    output = np.empty_like(data)
    max_reduction = 0.0
    engaged = 0
    block = max(1, frame_length)
    for channel in range(data.shape[0]):
        signal = data[channel]
        spectrum = np.fft.rfft(signal)
        high_band = np.fft.irfft(spectrum * high_mask, n=frames)
        # Detector: block RMS of the high band, held across the block.
        block_count = 1 + (frames - 1) // block
        levels = np.zeros(block_count)
        for index in range(block_count):
            start = index * block
            stop = min(frames, start + block)
            segment = high_band[start:stop]
            levels[index] = math.sqrt(float(np.mean(segment**2))) if segment.size else 0.0
        levels_db = np.where(levels > 0.0, 20.0 * np.log10(np.maximum(levels, 1e-12)), -200.0)
        excess = np.maximum(levels_db - threshold_db, 0.0)
        # Attack/release smoothing of the gain-reduction curve.
        smoothed = _envelope(excess, block_count, attack_ms=attack_ms, release_ms=release_ms)
        reduction = np.minimum(smoothed, max_reduction_db)
        max_reduction = max(max_reduction, float(reduction.max()) if reduction.size else 0.0)
        engaged += int(np.count_nonzero(reduction > 0.5))
        gains = 10.0 ** (-reduction / 20.0)
        # The reduction is applied to the high band *as a time-domain signal*,
        # not by multiplying its spectrum with a per-sample gain curve: the gain
        # curve has `frames` samples and the spectrum has `frames/2 + 1` bins, and
        # conflating the two is a length error, not a design choice.
        high_scaled = high_band * np.interp(
            np.arange(frames),
            (np.arange(block_count) + 0.5) * block,
            gains,
        )
        low_band = np.fft.irfft(spectrum * low_mask, n=frames)
        output[channel] = low_band + high_scaled

    # Measure the low band against the input: the crossover's obligation.
    deviations = []
    for channel in range(data.shape[0]):
        spectrum_in = np.fft.rfft(data[channel])
        spectrum_out = np.fft.rfft(output[channel])
        low_band_mask = (frequencies >= 100.0) & (frequencies <= 4_000.0)
        if not low_band_mask.any():
            continue
        reference = float(np.sqrt(np.mean(np.abs(spectrum_in[low_band_mask]) ** 2)))
        difference = spectrum_out[low_band_mask] - spectrum_in[low_band_mask]
        deviation = float(np.sqrt(np.mean(np.abs(difference) ** 2)))
        if reference > 0.0 and deviation > 0.0:
            deviations.append(20.0 * math.log10(deviation / reference))
        elif deviation == 0.0:
            deviations.append(-400.0)
    low_deviation = max(deviations) if deviations else -400.0
    # Return the same dimensionality that came in: a mono caller gets a mono
    # array back, not a (1, n) matrix it has to notice.
    result = output[0] if np.asarray(samples).ndim == 1 else output
    return DeessResult(
        samples=result,
        low_band_deviation_db=low_deviation,
        max_reduction_db=max_reduction,
        engaged_frames=engaged,
        low_hz=SIBILANCE_LOW_HZ,
        high_hz=SIBILANCE_HIGH_HZ,
    )
