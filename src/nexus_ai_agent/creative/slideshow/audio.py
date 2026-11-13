"""Beat detection and WAV decoding for the slideshow pack (numpy only).

The TDD calls for ``librosa``-grade analysis eventually; this module is the
dependency-free baseline that works today and stays behind the same contract::

    BeatGrid = analyse(pcm_audio)

``librosa`` (and any other heavier estimator) can be added later as an optional
adapter that produces the same :class:`BeatGrid` without touching the pack.

The estimator is a classic energy-flux onset detector:

1. frame the signal and compute a per-frame RMS envelope;
2. take the positive derivative (onset strength) and pick peaks above an
   adaptive threshold with a minimum separation;
3. estimate the beat period from the dominant inter-onset interval;
4. emit a regular grid anchored on the strongest onset.

It is deterministic, and it reports honestly: ``alignment_quality`` is
``detected`` only when the onsets are regular enough, otherwise the grid falls
back to the template's nominal BPM and says so.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.packs.slideshow.models import BeatGrid

MICROSECONDS_PER_SECOND = 1_000_000

#: Onset detection is run on a decimated envelope: 10 ms hops, 46 ms frames.
HOP_SECONDS = 0.010
FRAME_SECONDS = 0.046
MIN_ONSET_GAP_SECONDS = 0.15

#: Plausible musical beat period window (200 BPM .. 50 BPM).
MIN_PERIOD_SECONDS = 0.30
MAX_PERIOD_SECONDS = 1.20

#: Regularity threshold below which the tempo is considered undetected.
MAX_PERIOD_VARIATION = 0.30

#: STFT used by the spectral-flux onset estimator.  46 ms at 48 kHz is 2205
#: samples; a power-of-two window keeps the FFT cheap and the bands resolvable.
STFT_WINDOW = 2048
STFT_HOP = 512

#: Weighted sub-bands for onset detection.  Low punch (kick body), mid body
#: (snare/guitar), high transient (hats, consonants).  Percussive attacks live in
#: all three at different times, so a *weighted sum* of per-band flux is a much
#: better onset function than full-band energy flux: a bass swell moves thousands
#: of low bins without being an onset, while a hat moves few bins in a band the
#: full-band sum barely notices.
ONSET_BANDS_HZ: tuple[tuple[str, float, float, float], ...] = (
    ("low", 30.0, 180.0, 1.0),
    ("mid", 180.0, 2_000.0, 0.8),
    ("high", 2_000.0, 11_000.0, 1.2),
)

#: Perceptual tempo prior: listeners perceive a pulse centred near 120 BPM
#: (0.5 s) and lose it towards 40 and 240.  Used ONLY to break octave ties — it
#: never overrides a clearly better autocorrelation peak.
TEMPO_PRIOR_CENTRE_BPM = 120.0
TEMPO_PRIOR_OCTAVES = 1.0

#: A slower period is accepted as the beat when the onsets it fails to explain
#: are at most this fraction of the strength of the ones it explains.
#:
#: The value is measured, not chosen aesthetically.  On the reference material in
#: ``tests/unit/test_omega_wave2_tempo_uncertainty.py`` the ratio is 0.65–0.71
#: for a real subdivision (off-beat hats under a loud kick/snare backbeat) and
#: 0.98–1.01 for an isochronous stream that genuinely is that fast.  0.80 sits
#: with margin on both sides of the gap, which is the only honest place to put a
#: threshold: in the empty part of the measurement, not next to a data point.
SUBDIVISION_STRENGTH_RATIO = 0.80

#: A candidate period qualifies for the tactus only if its comb explains this
#: fraction of the measured onsets.  Set below 1.0 to tolerate the odd missed or
#: spurious onset, high enough that a doubled period (which explains only the
#: accents) cannot qualify.
TACTUS_COVERAGE = 0.88

#: A candidate must beat its octave alternatives by this ratio of comb score to
#: displace them.  Below it the octave is ambiguous and the prior decides.
OCTAVE_DECISION_RATIO = 1.08

#: Rectangular-distribution divisor for the STFT hop quantisation (Type B).
RECTANGULAR_SD = 12.0**0.5

#: Coverage factor for a 95% expanded uncertainty (normal distribution).
COVERAGE_FACTOR_95 = 1.96

#: The onset strength must rise out of the signal's own level for the envelope
#: to carry beats at all: a steady/noisy track has a high level and tiny flux,
#: a percussive one has sharp spikes.  Without this gate a normalized noise
#: floor (or a sustained pad) would "detect" a tempo that is not there.
MIN_ONSET_CONTRAST = 1.0
MIN_ONSETS_FOR_TEMPO = 8


class AudioError(ValueError):
    """The audio file could not be decoded."""


@dataclass(frozen=True)
class TempoEvidence:
    """Raw tempo evidence: how a tempo was (or was not) derived from a signal.

    ``status`` is the honest word for the outcome — ``detected`` means the
    inter-onset intervals agreed; ``insufficient_onsets`` / ``irregular_onsets``
    mean no tempo exists in the evidence and the caller must not invent one.
    """

    status: str
    duration_us: int
    onset_count: int
    tempo_bpm: float | None
    beat_period_us: int | None
    confidence: float
    first_beat_us: int
    onset_seconds: tuple[float, ...]
    #: Standard uncertainty of ``tempo_bpm`` in BPM, JCGM GUM combined Type A +
    #: Type B.  ``0.0`` when no tempo was detected (there is nothing to be
    #: uncertain about) — never a small number that could be mistaken for
    #: precision on a measurement that does not exist.
    bpm_std_uncertainty: float = 0.0
    #: Type-A component alone: dispersion of the retained lag intervals.
    bpm_uncertainty_type_a: float = 0.0
    #: Type-B component alone: STFT-hop quantisation of the onset times.
    bpm_uncertainty_type_b: float = 0.0
    #: 95% expanded uncertainty ``1.96 * u_c`` (coverage factor stated, not
    #: implied — a bare "±" is a claim about a distribution nobody declared).
    bpm_ci95_half_width: float = 0.0
    #: Which octave relation the retained period has to the raw autocorrelation
    #: peak: ``"as_is"``, ``"half"``, ``"double"`` or ``"tie_broken_by_prior"``.
    #: A consumer can see that the estimator changed its mind and why.
    octave_relation: str = "as_is"
    #: The autocorrelation comb score of the retained period, normalised to the
    #: best alternative.  ``1.0`` means no alternative came close.
    octave_pulse_clarity: float = 0.0


@dataclass(frozen=True)
class PcmAudio:
    """Mono float32 samples in ``[-1, 1]`` plus the sample rate."""

    samples: np.ndarray
    sample_rate: int

    @property
    def duration_us(self) -> int:
        if self.sample_rate <= 0:
            return 0
        return int(round(self.samples.size / float(self.sample_rate) * MICROSECONDS_PER_SECOND))


def read_wav_mono(path: Path | str) -> PcmAudio:
    """Decode a WAV file to mono float32 using the standard library."""
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, FileNotFoundError) as exc:
        raise AudioError(f"cannot read WAV file {source}: {exc}") from exc

    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8)
        if packed.size % 3:
            packed = packed[: packed.size - (packed.size % 3)]
        triplets = packed.reshape(-1, 3).astype(np.int32)
        values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        # Sign-extend *24* bits, without shifting the value into the 32-bit
        # sign bit (which scales the decoded signal by 256 and overflows).
        signed = (values ^ 0x800000) - 0x800000
        data = signed.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioError(f"unsupported WAV sample width: {width * 8} bit")

    if channels > 1 and data.size >= channels:
        usable = data.size - (data.size % channels)
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return PcmAudio(samples=data.astype(np.float32), sample_rate=sample_rate)


def frame_rms(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Per-frame RMS envelope (frames of ``FRAME_SECONDS``, hops of ``HOP_SECONDS``)."""
    frame = max(int(FRAME_SECONDS * sample_rate), 1)
    hop = max(int(HOP_SECONDS * sample_rate), 1)
    if samples.size < frame:
        return np.zeros(0, dtype=np.float32)
    squared = np.square(samples.astype(np.float64))
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    starts = np.arange(0, samples.size - frame + 1, hop)
    energies = (cumulative[starts + frame] - cumulative[starts]) / frame
    return np.sqrt(np.maximum(energies, 0.0)).astype(np.float32)


def _pick_onsets(envelope: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Return onset times (seconds) and their strengths."""
    if envelope.size < 3:
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    flux = np.diff(envelope, prepend=envelope[:1])
    flux = np.maximum(flux, 0.0)
    level = float(envelope.mean())
    if not np.any(flux > 0):
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    if float(flux.max()) < MIN_ONSET_CONTRAST * max(level, 1e-9):
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    threshold = float(flux.mean() + 1.0 * flux.std())
    min_gap_frames = max(int(MIN_ONSET_GAP_SECONDS / HOP_SECONDS), 1)
    times: list[float] = []
    strengths: list[float] = []
    last_index = -min_gap_frames
    for index in range(1, flux.size - 1):
        value = float(flux[index])
        if value < threshold or value < flux[index - 1] or value < flux[index + 1]:
            continue
        if index - last_index < min_gap_frames:
            continue
        times.append(index * HOP_SECONDS)
        strengths.append(value)
        last_index = index
    return np.asarray(times, dtype=np.float64), np.asarray(strengths, dtype=np.float64)


def spectral_flux_envelope(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
    """Half-wave-rectified, weighted multi-band log-magnitude spectral flux.

    Returns ``(onset_strength, hop_seconds)``.  The signal is framed with a Hann
    window at ``STFT_HOP``, each sub-band in :data:`ONSET_BANDS_HZ` gets its own
    magnitude sum, and the flux is the positive change of the **log** magnitude:

        flux(m) = Σ_b w_b · max(0, S_b(m) − S_b(m−1)) / L

    Using ``log`` rather than magnitude is what makes a quiet hat count as much
    as a loud kick: a linear sum is dominated by whatever band carries the most
    energy, so an attack in a quiet band is invisible.  The divisor ``L`` normalises
    by the number of frames so the strength is comparable across file lengths.
    """
    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    window = int(STFT_WINDOW)
    hop = int(STFT_HOP)
    hop_seconds = hop / float(sample_rate) if sample_rate > 0 else 0.0
    if sample_rate <= 0 or data.size < window:
        return np.zeros(0, dtype=np.float64), hop_seconds
    frames = 1 + (data.size - window) // hop
    if frames < 2:
        return np.zeros(0, dtype=np.float64), hop_seconds
    taper = np.hanning(window)
    indices = np.arange(frames) * hop
    framed = np.lib.stride_tricks.sliding_window_view(data, window)[indices]
    spectrum = np.abs(np.fft.rfft(framed * taper, axis=1))
    freqs = np.fft.rfftfreq(window, d=1.0 / sample_rate)
    bands: list[np.ndarray] = []
    for _name, low, high, weight in ONSET_BANDS_HZ:
        mask = (freqs >= low) & (freqs < high)
        if not mask.any():
            bands.append(np.zeros(frames, dtype=np.float64))
            continue
        total = spectrum[:, mask].sum(axis=1)
        # log1p keeps a silent band finite; log(0) would poison the whole flux.
        bands.append(weight * np.log1p(total))
    stacked = np.vstack(bands)
    delta = np.diff(stacked, axis=1, prepend=stacked[:, :1])
    flux = np.maximum(delta, 0.0).sum(axis=0) / max(frames, 1)
    return flux, hop_seconds


def energy_novelty_envelope(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
    """Half-wave-rectified novelty of the frame-energy envelope.

    The fourth detector alongside the three spectral bands.  Energy novelty
    catches a *sustained* level change (a section boundary, a held chord that
    enters) which no spectral-flux band reports, because flux only fires on
    change **within** a band while this fires on change of total level.  It is
    reported separately rather than fused so a caller can weigh it; fusing it
    into the flux would make "onset" mean two different things.
    """
    envelope = frame_rms(np.asarray(samples, dtype=np.float32), sample_rate).astype(np.float64)
    if envelope.size < 2:
        return np.zeros(0, dtype=np.float64), HOP_SECONDS
    delta = np.diff(envelope, prepend=envelope[:1])
    return np.maximum(delta, 0.0), HOP_SECONDS


def _pick_onsets_from_flux(flux: np.ndarray, hop_seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """Peak-pick an onset-strength function with an adaptive threshold."""
    if flux.size < 3 or hop_seconds <= 0 or not np.any(flux > 0):
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    threshold = float(flux.mean() + 1.0 * flux.std())
    min_gap_frames = max(int(MIN_ONSET_GAP_SECONDS / hop_seconds), 1)
    times: list[float] = []
    strengths: list[float] = []
    last_index = -min_gap_frames
    for index in range(1, flux.size - 1):
        value = float(flux[index])
        if value < threshold or value < flux[index - 1] or value < flux[index + 1]:
            continue
        if index - last_index < min_gap_frames:
            continue
        times.append(index * hop_seconds)
        strengths.append(value)
        last_index = index
    return np.asarray(times, dtype=np.float64), np.asarray(strengths, dtype=np.float64)


def _timing_envelope(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Onset strength for the harmonic comb.

    Spectral flux is the better *detector* (it sees a quiet hat), but the comb
    below scores periodicity, and the frame-RMS energy envelope is the function
    whose peaks sit exactly on beats.  Both are computed; the flux picks the
    onsets, the energy envelope scores the candidates.
    """
    return frame_rms(np.asarray(samples, dtype=np.float32), sample_rate).astype(np.float64)


def _autocorrelation_period(
    strength: np.ndarray,
    *,
    hop_seconds: float,
    min_period: float,
    max_period: float,
) -> float | None:
    """Period of the strongest autocorrelation peak of an onset-strength curve.

    The inter-onset-interval histogram in :func:`_dominant_period` needs one
    dominant gap; real music mixes subdivisions (8th hats over quarter-note
    backbeats) and the histogram then has no winner at all, which is how the
    estimator used to return "no tempo" on perfectly regular material.  The
    autocorrelation of the onset-strength curve does not care how many distinct
    gaps exist — it asks whether the curve repeats at a lag, which it does for
    any metrical level.
    """
    if strength.size < 4 or hop_seconds <= 0:
        return None
    centred = strength - float(strength.mean())
    if not np.any(np.abs(centred) > 0):
        return None
    spectrum = np.fft.rfft(centred, n=1 << int(np.ceil(np.log2(centred.size * 2))))
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum))[: centred.size]
    if autocorr[0] <= 0:
        return None
    autocorr = autocorr / autocorr[0]
    low = max(1, int(np.floor(min_period / hop_seconds)))
    high = min(autocorr.size - 1, int(np.ceil(max_period / hop_seconds)))
    if high <= low:
        return None
    window = autocorr[low : high + 1]
    peak = int(np.argmax(window))
    if window[peak] <= 0:
        return None
    # Parabolic interpolation of the peak: the lag grid is coarse (one frame is
    # ~10 ms, which is ~0.5 BPM at 120), and rounding to a frame would put a
    # quantisation floor on the very uncertainty this module reports.
    refined = float(peak)
    if 0 < peak < window.size - 1:
        left, centre, right = window[peak - 1], window[peak], window[peak + 1]
        denominator = left - 2.0 * centre + right
        if denominator != 0:
            refined += 0.5 * (left - right) / denominator
    period = (low + refined) * hop_seconds
    # ceil(max_period / hop) includes a boundary bin outside a narrow search
    # window. A quantized peak there is not permission to ignore that window.
    return period if min_period <= period <= max_period else None


def _comb_score(
    envelope: np.ndarray,
    *,
    lag_frames: float,
    hop_seconds: float,
    onsets: np.ndarray | None = None,
    weights: np.ndarray | None = None,
) -> tuple[float, float]:
    """Harmonic comb score of ``lag_frames``, plus its onset coverage.

    Returns ``(score, coverage)`` where ``score`` is energy × coverage and
    ``coverage`` is the fraction of onset weight explained by the comb teeth.

    Two independent terms, and the product is what distinguishes the octaves:

    *energy* — the mean envelope height at the teeth ``lag, 2·lag, …``.  A beat
    period puts every tooth on an attack; a period that is too **short** puts
    most teeth between attacks and loses energy.

    *coverage* — the fraction of detected onsets explained by some tooth.  A
    period that is too **long** still scores full energy (its teeth hit real
    attacks) but leaves the onsets in between unexplained, and loses here.

    Energy alone is degenerate in both directions: at 180 BPM the lag and its
    double both land every tooth on a click, so energy ties and a perceptual
    prior would arbitrarily halve the tempo.  Coverage breaks that tie with
    evidence rather than with a preference, because the doubled lag genuinely
    fails to explain half the onsets that were measured.
    """
    if hop_seconds <= 0 or lag_frames <= 0 or envelope.size == 0:
        return 0.0, 0.0
    # Teeth span the *whole* signal, not a fixed count.  With a fixed 8 the
    # short-period candidate is scored over 2 s and its double over 5 s, so the
    # two numbers describe different music and cannot be compared — the octave
    # decision then rests on nothing.  The per-tooth maximum caps the work at
    # the envelope length regardless of how short the candidate period is.
    max_teeth = min(int(envelope.size / lag_frames), 400)
    if max_teeth < 2:
        return 0.0, 0.0
    teeth = np.arange(1, max_teeth + 1, dtype=np.float64) * lag_frames
    positions = np.round(teeth).astype(int)
    positions = positions[(positions > 0) & (positions < envelope.size)]
    if positions.size < 2:
        return 0.0, 0.0
    # Sample the envelope with a one-frame tolerance: a beat that lands between
    # frames still counts, which keeps the score stable across hop choices.
    energy = 0.0
    for position in positions:
        low = max(0, position - 1)
        high = min(envelope.size, position + 2)
        energy += float(envelope[low:high].max())
    energy /= positions.size
    if energy <= 0:
        return 0.0, 0.0
    if onsets is None or onsets.size == 0:
        return energy, 1.0
    # Tolerance: a tooth explains an onset if it is within a quarter period
    # (bounded to at least one frame, so a coarse hop does not blind the test).
    tolerance = max(1.0, 0.25 * lag_frames)
    tooth_seconds = positions * hop_seconds
    # Coverage is weighted by onset *strength*, not by onset count.  A drummer
    # playing 8th-note hats over kick/snare backbeats produces twice as many hat
    # onsets as backbeats, so counting them lets the subdivision outvote the
    # pulse and locks the estimator onto double time.  Strength weighting asks
    # the musically correct question instead: does this period explain the
    # *accents*, the onsets a listener would tap along to?
    weights = weights if weights is not None and weights.size == onsets.size else None
    total = float(weights.sum()) if weights is not None else float(onsets.size)
    if total <= 0:
        return energy, 0.0
    explained = 0.0
    for index, onset in enumerate(onsets):
        nearest = float(np.min(np.abs(tooth_seconds - onset)))
        if nearest <= tolerance * hop_seconds:
            explained += float(weights[index]) if weights is not None else 1.0
    coverage = explained / total
    return energy * coverage, coverage


def _explained_split(
    *,
    envelope: np.ndarray,
    lag_frames: float,
    hop_seconds: float,
    onsets: np.ndarray,
    weights: np.ndarray | None,
) -> tuple[float, float]:
    """Mean strength of the onsets a comb explains vs. the ones it does not.

    This is the measurement that separates the two ways a pulse can be too slow,
    which coverage alone cannot:

    * **a subdivision** — the onsets between the teeth are weak (hats under a
      backbeat), so the ratio is small and the *slower* period is the beat;
    * **an isochronous faster pulse** — the onsets between the teeth are just as
      loud as the ones on them (a 240 BPM click), so the ratio is ≈1 and the
      faster pulse is the real tempo.

    Returns ``(explained_mean, unexplained_mean)``; ``unexplained_mean`` is 0.0
    when every onset is explained.
    """
    if envelope.size == 0 or lag_frames <= 0 or onsets.size == 0:
        return 0.0, 0.0
    max_teeth = min(int(envelope.size / lag_frames), 400)
    if max_teeth < 2:
        return 0.0, 0.0
    teeth = np.arange(1, max_teeth + 1, dtype=np.float64) * lag_frames
    positions = np.round(teeth).astype(int)
    positions = positions[(positions > 0) & (positions < envelope.size)]
    if positions.size < 2:
        return 0.0, 0.0
    tooth_seconds = positions * hop_seconds
    tolerance = max(1.0, 0.25 * lag_frames) * hop_seconds
    strength = (
        weights if weights is not None and weights.size == onsets.size else np.ones_like(onsets)
    )
    on: list[float] = []
    off: list[float] = []
    for index, onset in enumerate(onsets):
        nearest = float(np.min(np.abs(tooth_seconds - onset)))
        (on if nearest <= tolerance else off).append(float(strength[index]))
    return (
        float(np.mean(on)) if on else 0.0,
        float(np.mean(off)) if off else 0.0,
    )


def _tempo_prior(bpm: float) -> float:
    """Gaussian weight around the perceptual 120 BPM centre (log2 distance)."""
    if bpm <= 0:
        return 0.0
    octaves = np.log2(bpm / TEMPO_PRIOR_CENTRE_BPM)
    return float(np.exp(-0.5 * (octaves / TEMPO_PRIOR_OCTAVES) ** 2))


def _resolve_octave(
    period: float,
    *,
    envelope: np.ndarray,
    hop_seconds: float,
    min_period: float,
    max_period: float,
    onsets: np.ndarray | None = None,
    weights: np.ndarray | None = None,
) -> tuple[float, str, float]:
    """Resolve the octave of ``period`` using measured onset coverage.

    Returns ``(chosen_period, relation, clarity)``.

    The estimator starts from the dominant inter-onset interval, which is
    reliable on regular and syncopated material.  Two octave errors remain
    possible, and each is corrected only when the onset stream contains
    **evidence** for the correction — never on a preference:

    *doubling* (reported tempo too high) — if the reported pulse is faster than
    :data:`TACTUS_CEILING_BPM` and the doubled period explains at least as many
    onsets as the reported one, the reported pulse is a subdivision.  If the
    doubled period leaves onsets unexplained (coverage below
    :data:`TACTUS_COVERAGE`), there is no evidence of a slower beat and the fast
    pulse is kept: an isochronous 240 BPM click really is 240 BPM.

    *halving* (reported tempo too low) — only when the halved period explains a
    **strictly larger** share of onset weight than the reported period by
    :data:`OCTAVE_DECISION_RATIO`.  Half of an isochronous pulse leaves every
    other onset unexplained, so this cannot fire on clean material; it fires when
    the reported period was a multiple of the true one.

    ``clarity`` is the retained coverage divided by the best rival's, so a value
    near 1.0 tells the caller the octaves were genuinely ambiguous and the
    answer should be treated as such.  A prior over tempo exists in
    :func:`_tempo_prior` but is applied only to break an *exact* coverage tie.
    """
    if period <= 0 or hop_seconds <= 0:
        return period, "as_is", 0.0
    _score, base_coverage = _comb_score(
        envelope,
        lag_frames=period / hop_seconds,
        hop_seconds=hop_seconds,
        onsets=onsets,
        weights=weights,
    )
    if onsets is None or onsets.size == 0:
        return period, "as_is", 0.0

    def coverage_of(value: float) -> float:
        if not (min_period <= value <= max_period):
            return -1.0
        _s, coverage = _comb_score(
            envelope,
            lag_frames=value / hop_seconds,
            hop_seconds=hop_seconds,
            onsets=onsets,
            weights=weights,
        )
        return coverage

    doubled = period * 2.0
    if min_period <= doubled <= max_period:
        doubled_coverage = coverage_of(doubled)
        explained, unexplained = _explained_split(
            envelope=envelope,
            lag_frames=doubled / hop_seconds,
            hop_seconds=hop_seconds,
            onsets=onsets,
            weights=weights,
        )
        # The measurement that decides the octave, applied at *every* tempo:
        # take the slower period when the onsets it leaves unexplained are
        # materially weaker than the ones it explains, because that is what a
        # subdivision looks like — an off-beat hat under a loud backbeat.  When
        # the leftover onsets are just as strong, the faster pulse is the real
        # tempo (an isochronous stream) and is kept.  Coverage is the second,
        # independent route: a slower period that explains essentially
        # everything is simply the beat.
        subdivision_ratio = (
            unexplained / explained if explained > 0.0 and unexplained > 0.0 else 0.0
        )
        if unexplained > 0.0 and subdivision_ratio <= SUBDIVISION_STRENGTH_RATIO:
            clarity = max(1.0 - subdivision_ratio, doubled_coverage)
            return doubled, "double", float(clarity)
        if doubled_coverage >= TACTUS_COVERAGE:
            return doubled, "double", float(doubled_coverage)
        if not (unexplained > 0.0):
            # Every onset sits on this comb: nothing to explain, nothing to gain
            # by slowing down.
            return period, "as_is", 2.0
    halved = period / 2.0
    halved_coverage = coverage_of(halved)
    if halved_coverage > base_coverage * OCTAVE_DECISION_RATIO:
        prior_fast = _tempo_prior(60.0 / halved)
        prior_slow = _tempo_prior(60.0 / period)
        if prior_fast >= prior_slow * 0.5:
            clarity = halved_coverage / max(base_coverage, 1e-12)
            return halved, "half", float(clarity)
    if halved_coverage > 0:
        return period, "as_is", float(base_coverage / max(halved_coverage, 1e-12))
    return period, "as_is", 2.0


def tempo_uncertainty(
    intervals: np.ndarray, *, mean_period: float, hop_seconds: float
) -> tuple[float, float, float, float, float]:
    """JCGM GUM combined standard uncertainty of the derived BPM.

    ``u_A`` — the dispersion of the ``M`` retained lag intervals:

        u_A(BPM) = (60 / τ̄²) · s_τ / √M

    ``u_B`` — the STFT hop quantisation, modelled as a rectangular distribution
    of half-width ``Δt_hop/2`` (standard deviation ``Δt_hop/√12``) propagated
    through ``BPM = 60/τ̄`` with the mean skip ``k̄`` accounted for:

        u_B(BPM) = (60 / (k̄·τ̄²)) · Δt_hop / √12

    Combined in quadrature, ``u_c = √(u_A² + u_B²)``, expanded as ``1.96·u_c``.

    Returns ``(u_a, u_b, u_c, u95, mean_skip)``.  Every term is a *stated*
    distribution, not a guessed tolerance: that is the difference between an
    uncertainty and a fudge factor.
    """
    values = np.asarray(intervals, dtype=np.float64).reshape(-1)
    count = int(values.size)
    if count < 2 or mean_period <= 0 or hop_seconds <= 0:
        return 0.0, 0.0, 0.0, 0.0, 1.0
    dispersion = float(values.std(ddof=1))
    type_a = (60.0 / mean_period**2) * dispersion / np.sqrt(count)
    # The mean skip of the retained intervals: a candidate produced by the
    # k-skip estimator has been averaged over k gaps, so its quantisation error
    # is divided by k as well.
    mean_skip = 1.0
    unique = np.unique(np.round(values, 6))
    if unique.size and mean_period > 0:
        mean_skip = max(1.0, float(np.round(np.mean(values) / max(mean_period, 1e-12))))
    type_b = (60.0 / (mean_skip * mean_period**2)) * hop_seconds / RECTANGULAR_SD
    combined = float(np.sqrt(type_a**2 + type_b**2))
    return float(type_a), float(type_b), combined, COVERAGE_FACTOR_95 * combined, mean_skip


def _dominant_period(
    onset_times: np.ndarray,
    *,
    min_period_seconds: float = MIN_PERIOD_SECONDS,
    max_period_seconds: float = MAX_PERIOD_SECONDS,
) -> tuple[float | None, float]:
    """Dominant inter-onset period in seconds and its relative variation.

    The search window is a parameter, not a constant: a caller that asks for
    ``min_bpm=140`` must not receive a 60 BPM answer from a hard-coded window
    (that is precisely the "constant instead of analysis" failure this pack is
    being fixed for).  ``collect_tempo_candidates`` exposes the raw evidence.
    """
    if onset_times.size < MIN_ONSETS_FOR_TEMPO:
        return None, 1.0
    values = _interval_candidates(onset_times, min_period_seconds, max_period_seconds)
    if values.size < 4:
        return None, 1.0
    bins = np.arange(min_period_seconds, max_period_seconds + 0.02, 0.02)
    histogram, edges = np.histogram(values, bins=bins)
    peak = int(np.argmax(histogram))
    low, high = float(edges[peak]), float(edges[peak + 1])
    in_bin = values[(values >= low) & (values < high)]
    if in_bin.size == 0:
        return None, 1.0
    period = float(np.median(in_bin))
    variation = float(in_bin.std() / period) if period > 0 else 1.0
    return (period, variation) if variation <= MAX_PERIOD_VARIATION else (None, variation)


def _interval_candidates(
    onset_times: np.ndarray, min_period_seconds: float, max_period_seconds: float
) -> np.ndarray:
    """Every inter-onset interval inside the window, averaged over skips 1..4.

    The k-skip candidate is the **mean k-lag gap** ``(t[i+k] - t[i]) / k``: it
    recovers the beat period when intermediate onsets were missed (an accent
    pattern, a weak hit under the noise floor).

    This used to be ``np.diff(onset_times, n=step)``, which is the k-th *finite
    difference* — a quantity that is identically zero for evenly spaced onsets
    and grows with the k-th derivative otherwise.  Dividing it by ``step`` does
    not turn a finite difference into a gap, so skips 2..4 contributed either
    zero (filtered out by the window) or a number with no physical meaning as a
    period, and the octave-disambiguation they exist for did nothing.
    """
    ordered = np.sort(np.asarray(onset_times, dtype=np.float64).reshape(-1))
    differences: list[float] = []
    for step in (1, 2, 3, 4):
        if ordered.size > step:
            gaps = (ordered[step:] - ordered[:-step]) / step
            differences.extend(
                float(gap) for gap in gaps if min_period_seconds <= gap <= max_period_seconds
            )
    return np.asarray(differences, dtype=np.float64)


def collect_tempo_candidates(
    audio: PcmAudio,
    *,
    min_bpm: float = 40.0,
    max_bpm: float = 240.0,
) -> TempoEvidence:
    """The raw onset/tempo evidence behind an estimate (no fallback involved).

    This is what a host adapter uses to *report* how a tempo was derived —
    ``detected`` versus ``insufficient_onsets`` — instead of a caller having to
    guess from a boolean.
    """
    if min_bpm <= 0 or max_bpm <= min_bpm:
        raise AudioError(f"invalid tempo window: min_bpm={min_bpm}, max_bpm={max_bpm}")
    min_period = 60.0 / max_bpm
    max_period = 60.0 / min_bpm
    duration_us = audio.duration_us
    if duration_us <= 0:
        return TempoEvidence(
            status="empty_audio",
            duration_us=0,
            onset_count=0,
            tempo_bpm=None,
            beat_period_us=None,
            confidence=0.0,
            first_beat_us=0,
            onset_seconds=(),
        )
    # Two onset functions, for two different jobs (see ``_timing_envelope``):
    # spectral flux decides *where* the onsets are, the energy envelope scores
    # periodicity for the harmonic comb.
    flux, hop_seconds = spectral_flux_envelope(audio.samples, audio.sample_rate)
    if flux.size:
        onsets, strengths = _pick_onsets_from_flux(flux, hop_seconds)
    else:  # pragma: no cover - only a sub-window signal reaches this
        envelope_fallback = frame_rms(audio.samples, audio.sample_rate)
        onsets, strengths = _pick_onsets(envelope_fallback, audio.sample_rate)
    timing = _timing_envelope(audio.samples, audio.sample_rate)
    intervals = _interval_candidates(onsets, min_period, max_period)
    period, variation = _dominant_period(
        onsets, min_period_seconds=min_period, max_period_seconds=max_period
    )
    if period is None:
        # The interval histogram had no dominant gap.  That is not "no tempo" —
        # it is "several metrical levels at once", so ask the autocorrelation of
        # the onset-strength curve instead, which has an answer at every level.
        period = _autocorrelation_period(
            np.asarray(flux, dtype=np.float64),
            hop_seconds=hop_seconds or HOP_SECONDS,
            min_period=min_period,
            max_period=max_period,
        )
        if period is not None:
            intervals = _interval_candidates(onsets, min_period, max_period)
            variation = float(intervals.std() / period) if intervals.size >= 2 else 0.0
    octave_relation = "as_is"
    pulse_clarity = 0.0
    if period is not None:
        envelope_hop = HOP_SECONDS
        resolved, octave_relation, pulse_clarity = _resolve_octave(
            period,
            envelope=timing,
            hop_seconds=envelope_hop,
            min_period=min_period,
            max_period=max_period,
            onsets=onsets,
            weights=strengths,
        )
        if resolved != period:
            # Re-derive the dispersion around the *retained* period.  The window
            # has to stay wide: narrowing it to start at the new period would
            # discard every shorter interval — which for a doubled period is all
            # of them, yielding an empty set, a dispersion of exactly 0.0 and a
            # false "zero uncertainty".  The selection is therefore by proximity
            # to the retained period, not by the search window.
            period = resolved
            all_candidates = _interval_candidates(onsets, min_period, max_period)
            if all_candidates.size:
                near = all_candidates[np.abs(all_candidates - period) <= 0.5 * period]
                if near.size >= 2:
                    # Project onto the retained octave so the dispersion is in
                    # the same units as ``period`` (a 2×-lag gap contributes the
                    # same fractional error as its half).
                    halved = np.where(near > period * 1.5, near / 2.0, near)
                    intervals = halved
            variation = (
                float(intervals.std() / period) if intervals.size >= 2 and period > 0 else variation
            )
    anchor = float(onsets[int(np.argmax(strengths))]) if onsets.size and strengths.size else 0.0
    if period is None:
        return TempoEvidence(
            status=(
                "insufficient_onsets" if onsets.size < MIN_ONSETS_FOR_TEMPO else "irregular_onsets"
            ),
            duration_us=duration_us,
            onset_count=int(onsets.size),
            tempo_bpm=None,
            beat_period_us=None,
            confidence=round(float(max(0.0, min(1.0, 1.0 - variation))), 4),
            first_beat_us=int(round(anchor * MICROSECONDS_PER_SECOND)),
            onset_seconds=tuple(round(float(value), 6) for value in onsets[:200]),
            octave_relation=octave_relation,
            octave_pulse_clarity=round(float(pulse_clarity), 6),
        )
    tempo = 60.0 / period
    type_a, type_b, combined, expanded, _mean_skip = tempo_uncertainty(
        intervals, mean_period=period, hop_seconds=hop_seconds or HOP_SECONDS
    )
    confidence = max(
        0.0,
        min(1.0, (1.0 - variation) * min(1.0, onsets.size / 24.0)),
    )
    return TempoEvidence(
        status="detected",
        duration_us=duration_us,
        onset_count=int(onsets.size),
        tempo_bpm=round(tempo, 4),
        beat_period_us=int(round(period * MICROSECONDS_PER_SECOND)),
        confidence=round(confidence, 4),
        first_beat_us=int(round(anchor * MICROSECONDS_PER_SECOND)),
        onset_seconds=tuple(round(float(value), 6) for value in onsets[:200]),
        bpm_std_uncertainty=round(combined, 6),
        bpm_uncertainty_type_a=round(type_a, 6),
        bpm_uncertainty_type_b=round(type_b, 6),
        bpm_ci95_half_width=round(expanded, 6),
        octave_relation=octave_relation,
        octave_pulse_clarity=round(float(pulse_clarity), 6),
    )


def estimate_beat_grid(
    audio: PcmAudio,
    *,
    fallback_bpm: float,
    bpm_tolerance: float = 0.06,
) -> BeatGrid:
    """Estimate a beat grid; never claims more precision than it has."""
    duration_us = audio.duration_us
    envelope = frame_rms(audio.samples, audio.sample_rate)
    onsets, strengths = _pick_onsets(envelope, audio.sample_rate)
    period, variation = _dominant_period(onsets)

    if period is None:
        period = 60.0 / fallback_bpm
        anchor_index = int(np.argmax(strengths)) if strengths.size else 0
        phase = float(onsets[anchor_index]) if onsets.size else 0.0
        times = _grid(phase, period, duration_us)
        confidence = float(max(0.0, min(1.0, 1.0 - variation)) * 0.4)
        return BeatGrid(
            duration_us=duration_us,
            tempo_bpm=round(fallback_bpm, 3),
            beats_us=times,
            strong_beats_us=tuple(times[::4]),
            tempo_source="fallback_bpm",
            alignment_quality="interpolated",
            confidence=round(confidence, 3),
        )

    tempo_bpm = 60.0 / period
    anchor_index = int(np.argmax(strengths)) if strengths.size else 0
    phase = float(onsets[anchor_index]) if onsets.size else 0.0
    times = _grid(phase, period, duration_us)
    confidence = max(
        0.0,
        min(
            1.0,
            (1.0 - variation)
            * min(1.0, onsets.size / 24.0)
            * (
                1.0
                if abs(tempo_bpm - fallback_bpm) <= bpm_tolerance * max(tempo_bpm, 1.0)
                else 0.85
            ),
        ),
    )
    return BeatGrid(
        duration_us=duration_us,
        tempo_bpm=round(tempo_bpm, 3),
        beats_us=times,
        strong_beats_us=tuple(times[::4]),
        tempo_source="detected",
        alignment_quality="detected" if confidence > 0.35 else "interpolated",
        confidence=round(confidence, 3),
    )


def _grid(
    phase: float, period: float, duration_us: int, *, max_beats: int = 4000
) -> tuple[int, ...]:
    """Regular beat grid starting at ``phase`` (seconds), clipped to the media."""
    if period <= 0:
        return ()
    duration_seconds = duration_us / MICROSECONDS_PER_SECOND
    first = phase - period * np.floor(phase / period) if phase > 0 else 0.0
    count = int(min(max_beats, max(1, np.ceil(duration_seconds / period) + 1)))
    times = first + period * np.arange(count, dtype=np.float64)
    return tuple(
        int(round(value * MICROSECONDS_PER_SECOND)) for value in times if value <= duration_seconds
    )


def beat_grid_for_file(
    path: Path | str, *, fallback_bpm: float, bpm_tolerance: float = 0.06
) -> BeatGrid:
    """Decode a WAV file and estimate its beat grid."""
    audio = read_wav_mono(path)
    grid = estimate_beat_grid(audio, fallback_bpm=fallback_bpm, bpm_tolerance=bpm_tolerance)
    return grid.model_copy(update={"source_path": str(path)})


def tempo_evidence_for_file(
    path: Path | str | None,
    *,
    pcm: PcmAudio | None = None,
    min_bpm: float = 40.0,
    max_bpm: float = 240.0,
) -> TempoEvidence:
    """Decode (or accept already-decoded PCM) and return raw tempo evidence.

    ``path`` may be ``None`` when the caller already holds decoded samples
    (the engine decodes with FFmpeg, so it never needs a WAV on disk).
    """
    audio = pcm if pcm is not None else read_wav_mono(path)  # type: ignore[arg-type]
    return collect_tempo_candidates(audio, min_bpm=min_bpm, max_bpm=max_bpm)
