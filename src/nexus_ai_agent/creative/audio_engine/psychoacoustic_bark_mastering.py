"""K4: 24-band Zwicker masking-aware stems and measured broadcast mastering.

The masking model is explicit: lower-side slope +25 dB/Bark, upper-side
-27..-10 dB/Bark depending on masker level. A caller-declared dBFS-to-SPL
calibration is a model parameter, NOT a measured listener threshold. Only
800..4500 Hz competing music/SFX bins may be attenuated. Protected bass/air
can make a requested speech margin infeasible; that is reported, not hidden.

The final master is measured with the canonical independent BS.1770 meter.
LUFS and LKFS name the same integrated BS.1770 quantity here. Silence or an
unattainable loudness/peak pair refuses instead of returning a target as if it
were an observation. These are offline, channel-linked classical operations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import (
    BS1770Measurement,
    measure_bs1770_numpy,
    true_peak_dbtp,
)
from nexus_ai_agent.creative.audio_engine.spectral_voice_restoration import (
    wola_analyse,
    wola_synthesise,
)

BARK_EDGES_HZ = np.array(
    [
        0,
        100,
        200,
        300,
        400,
        510,
        630,
        770,
        920,
        1080,
        1270,
        1480,
        1720,
        2000,
        2320,
        2700,
        3150,
        3700,
        4400,
        5300,
        6400,
        7700,
        9500,
        12000,
        15500,
    ],
    dtype=np.float64,
)
SPEECH_MARGIN_DB = 6.0
BROADCAST_LKFS = -24.0
TRUE_PEAK_LIMIT_DBTP = -1.0


def zwicker_bark(frequency_hz: np.ndarray) -> np.ndarray:
    frequency = np.asarray(frequency_hz, dtype=np.float64)
    if not np.isfinite(frequency).all() or np.any(frequency < 0):
        raise ValueError("Bark frequency must be finite and nonnegative")
    return np.asarray(
        13 * np.arctan(0.00076 * frequency) + 3.5 * np.arctan((frequency / 7500) ** 2),
        dtype=np.float64,
    )


def bark_band_weights(frequencies_hz: np.ndarray) -> np.ndarray:
    """Disjoint 24 critical bands, partition of unity including DC/Nyquist.

    The top critical band includes the >15500 Hz remainder, which is protected
    from carving regardless. No input bin disappears between rounded edges.
    """
    frequencies = np.asarray(frequencies_hz, dtype=np.float64)
    zwicker_bark(frequencies)  # validate; coordinates below use the fixed bands
    if frequencies.ndim != 1:
        raise ValueError("frequency grid must be one-dimensional")
    indices = np.clip(np.searchsorted(BARK_EDGES_HZ, frequencies, side="right") - 1, 0, 23)
    return np.asarray(np.arange(24)[:, None] == indices[None, :], dtype=np.float64)


def spreading_db(
    target_bark: np.ndarray, masker_bark: np.ndarray, masker_level_db_spl: np.ndarray
) -> np.ndarray:
    """Asymmetric spreading: a lower target loses 25 dB per Bark distance."""
    target, masker, level = np.broadcast_arrays(
        np.asarray(target_bark, float),
        np.asarray(masker_bark, float),
        np.asarray(masker_level_db_spl, float),
    )
    if (
        not np.isfinite(target).all()
        or not np.isfinite(masker).all()
        or not np.isfinite(level).all()
    ):
        raise ValueError("spreading coordinates and levels must be finite")
    difference = target - masker
    upper_slope = -np.clip(27 - 0.37 * np.maximum(level - 40, 0), 10, 27)
    return np.asarray(
        np.where(difference < 0, 25 * difference, upper_slope * difference), dtype=np.float64
    )


def _matrix(samples: np.ndarray) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data[None, :]
    if data.ndim != 2 or data.shape[0] not in (1, 2) or not np.isfinite(data).all():
        raise ValueError("stems must be finite mono/stereo channel-first PCM")
    return data


@dataclass(frozen=True)
class BarkCarveResult:
    dialogue: np.ndarray
    music: np.ndarray
    sfx_foley: np.ndarray
    band_gains: np.ndarray
    input_margin_db: np.ndarray
    output_margin_db: np.ndarray
    active_speech_bands: np.ndarray
    margin_reached: bool
    spl_at_full_scale_db: float


def carve_speech_masking(
    dialogue: np.ndarray,
    music: np.ndarray,
    sfx_foley: np.ndarray,
    *,
    sample_rate: int = 48000,
    margin_db: float = SPEECH_MARGIN_DB,
    max_cut_db: float = 36.0,
    spl_at_full_scale_db: float = 100.0,
    frame_length: int = 2048,
) -> BarkCarveResult:
    """Constrained Bark-domain music/SFX attenuation; dialogue is never changed.

    Gains are shared between stereo channels. Each active target constraint
    reduces only eligible source bands actually contributing to its masker.
    Frequencies outside 800..4500 Hz retain gain one, even if that makes the
    requested +6 dB margin impossible. WOLA does not introduce hard block seams.
    """
    stems = [_matrix(x) for x in (dialogue, music, sfx_foley)]
    if any(stem.shape != stems[0].shape for stem in stems) or stems[0].shape[1] == 0:
        raise ValueError("stems must be nonempty, aligned and have identical channel geometry")
    if (
        not 16000 <= sample_rate <= 192000
        or not np.isfinite([margin_db, max_cut_db, spl_at_full_scale_db]).all()
    ):
        raise ValueError("invalid rate or masking calibration")
    if (
        not 0 <= margin_db <= 18
        or not 0 < max_cut_db <= 60
        or not 60 <= spl_at_full_scale_db <= 140
    ):
        raise ValueError("masking margin, attenuation or SPL calibration exceeds the bounded model")
    analyses = [
        [wola_analyse(channel, frame_length=frame_length) for channel in stem] for stem in stems
    ]
    spectra = np.stack([np.stack([a.spectrum for a in group]) for group in analyses])
    frequencies = np.fft.rfftfreq(frame_length, 1 / sample_rate)
    bands = bark_band_weights(frequencies)
    centers = zwicker_bark((BARK_EDGES_HZ[:-1] + BARK_EDGES_HZ[1:]) / 2)
    eligible_bins = (frequencies >= 800) & (frequencies <= 4500)
    power = np.mean(np.abs(spectra) ** 2, axis=1)
    speech = power[0] @ bands.T
    # The stems can be phase-correlated (e.g. duplicate Foley/music). Summing
    # powers assumes incoherence and understates the actual mixed masker by
    # up to 3 dB. Gains are shared, so meter their coherent sum instead.
    competing = np.mean(np.abs(spectra[1] + spectra[2]) ** 2, axis=0)
    adjustable = (competing * eligible_bins) @ bands.T
    protected = (competing * ~eligible_bins) @ bands.T
    active = (speech > max(float(speech.max()) * 1e-4, 1e-20)) & (
        (BARK_EDGES_HZ[:-1] < 4500) & (BARK_EDGES_HZ[1:] > 800)
    )
    gains = np.ones_like(speech)
    minimum = 10 ** (-max_cut_db / 20)
    normalizer = frame_length * float(np.sum(analyses[0][0].window ** 2))

    def masker(gain: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        source_power = adjustable * gain**2 + protected
        levels = 10 * np.log10(np.maximum(source_power / normalizer, 1e-20)) + spl_at_full_scale_db
        spread = 10 ** (
            spreading_db(centers[None, :, None], centers[None, None, :], levels[:, None, :]) / 10
        )
        return np.einsum("fti,fi->ft", spread, source_power), spread

    before, _ = masker(gains)
    budget = speech / 10 ** (margin_db / 10)
    for _iteration in range(8):
        current, spread = masker(gains)
        violations = np.argwhere(active & (current > budget * 1.0001))
        if violations.size == 0:
            break
        for frame, target in violations:
            contributions = spread[frame, target] * adjustable[frame] * gains[frame] ** 2
            contributors = contributions > max(float(contributions.max()) * 1e-3, 1e-20)
            if not np.any(contributors):
                continue
            reserved = float(spread[frame, target] @ protected[frame])
            available = max(float(budget[frame, target]) - reserved, 1e-30)
            reduction = min(1.0, math.sqrt(available / max(float(contributions.sum()), 1e-30)))
            gains[frame, contributors] = np.maximum(minimum, gains[frame, contributors] * reduction)
    after, _ = masker(gains)
    input_margin = 10 * np.log10(np.maximum(speech, 1e-30) / np.maximum(before, 1e-30))
    output_margin = 10 * np.log10(np.maximum(speech, 1e-30) / np.maximum(after, 1e-30))
    bins_gain = gains @ bands
    bins_gain[:, ~eligible_bins] = 1.0
    outputs = [
        np.stack(
            [
                wola_synthesise(spectra[s, channel] * bins_gain, analyses[s][channel])
                for channel in range(stems[0].shape[0])
            ]
        )
        for s in (1, 2)
    ]
    return BarkCarveResult(
        stems[0].copy(),
        outputs[0],
        outputs[1],
        gains,
        input_margin,
        output_margin,
        active,
        bool(np.all(output_margin[active] >= margin_db - 0.05)),
        spl_at_full_scale_db,
    )


def _linked_peak_limit(samples: np.ndarray, *, sample_rate: int, ceiling: float) -> np.ndarray:
    """Offline lookahead peak envelope, instantaneous attack, 30 ms release."""
    peak = np.max(np.abs(samples), axis=0)
    radius = max(1, round(sample_rate * 0.001))
    padded = np.pad(peak, (radius, radius))
    envelope = np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1).max(axis=1)
    required = np.minimum(1.0, ceiling / np.maximum(envelope, 1e-20))
    release = math.exp(-1 / (sample_rate * 0.03))
    gain = 1.0
    for index in range(required.size):
        gain = min(float(required[index]), release * gain + (1 - release))
        required[index] = gain
    return np.asarray(samples * required, dtype=np.float64)


@dataclass(frozen=True)
class BarkMasterResult:
    samples: np.ndarray
    measurement: BS1770Measurement
    masking: BarkCarveResult
    gain_db: float
    limiter_passes: int


def master_dialogue_music_sfx(
    dialogue: np.ndarray,
    music: np.ndarray,
    sfx_foley: np.ndarray,
    *,
    sample_rate: int = 48000,
) -> BarkMasterResult:
    """Three-stem Bark master to -24 LKFS +/-0.1 LU, true peak <=-1 dBTP.

    Limiting is DYNAMIC_ALLOWED and channel-linked. Each iteration remeasures
    integrated loudness; a target is never copied into a measurement field.
    A final, independent post-encode passport check is the execution layer's job.
    """
    masking = carve_speech_masking(dialogue, music, sfx_foley, sample_rate=sample_rate)
    output = masking.dialogue + masking.music + masking.sfx_foley
    if output.shape[1] < math.ceil(0.4 * sample_rate):
        raise ValueError("broadcast mastering needs at least one 400 ms loudness block")
    gain_db = 0.0
    passes = 0
    for _iteration in range(16):
        measurement = measure_bs1770_numpy(output, sample_rate=sample_rate)
        if measurement.lufs is None:
            raise ValueError("silence/below-gate audio has no broadcast loudness target")
        correction = BROADCAST_LKFS - measurement.lufs
        output *= 10 ** (correction / 20)
        gain_db += correction
        peak = true_peak_dbtp(output)
        if peak is not None and peak <= TRUE_PEAK_LIMIT_DBTP - 0.02:
            final = measure_bs1770_numpy(output, sample_rate=sample_rate)
            if final.lufs is not None and abs(final.lufs - BROADCAST_LKFS) <= 0.1:
                return BarkMasterResult(output, final, masking, gain_db, passes)
        # Intersample headroom is measured, not assumed equal to sample peak.
        assert peak is not None  # non-silent measured programme above
        ceiling = 10 ** (
            (TRUE_PEAK_LIMIT_DBTP - 0.08 - max(0, peak - TRUE_PEAK_LIMIT_DBTP) * 0.1) / 20
        )
        output = _linked_peak_limit(output, sample_rate=sample_rate, ceiling=ceiling)
        passes += 1
    raise ValueError("broadcast loudness/true-peak constraints did not converge; refuse master")


__all__ = [
    "BARK_EDGES_HZ",
    "BROADCAST_LKFS",
    "SPEECH_MARGIN_DB",
    "TRUE_PEAK_LIMIT_DBTP",
    "BarkCarveResult",
    "BarkMasterResult",
    "bark_band_weights",
    "carve_speech_masking",
    "master_dialogue_music_sfx",
    "spreading_db",
    "zwicker_bark",
]
