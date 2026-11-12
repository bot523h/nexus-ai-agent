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
        values = (triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)) << 8
        signed = values.astype(np.int32)
        signed[signed >= 0x800000] -= 0x1000000
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
    envelope = frame_rms(audio.samples, audio.sample_rate)
    onsets, strengths = _pick_onsets(envelope, audio.sample_rate)
    period, variation = _dominant_period(
        onsets, min_period_seconds=min_period, max_period_seconds=max_period
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
        )
    tempo = 60.0 / period
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
