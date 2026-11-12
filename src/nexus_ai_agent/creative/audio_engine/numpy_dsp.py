"""Bounded-memory signal analysis over decoded PCM (numpy only, no I/O).

The engine never holds a whole programme in RAM: :meth:`FfmpegEngine.signature`
decodes at most ``max_analysis_seconds`` of the file, and this module analyses
that window in fixed-size frames while keeping only *aggregates* (a spectral
histogram, an energy sum, a zero-crossing count).  Memory is therefore
``O(frame_length × bins)`` regardless of programme length — the property the
OMEGA-01 contract calls ``Bounded-Memory``.

What is computed, and from what:

* ``RMS`` / ``peak`` / ``crest factor`` — directly from the samples;
* ``dynamic range`` — the spread between loud and quiet frame RMS, a *measured*
  short-term range (not EBU LRA; LRA comes from the ``ebur128`` scanner, which
  is the only correct source for it);
* ``spectral centroid`` — energy-weighted mean frequency from an rFFT per frame,
  with a Hann window, accumulated into 1/3-octave-ish band energies;
* ``zero-crossing rate`` — per frame, averaged.

Everything is deterministic: same bytes in, same numbers out.  Nothing here
raises on silence — a silent window returns ``None`` for the level fields and a
zero centroid, so a caller can distinguish "silent" from "quiet" without
guessing from a sentinel value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.audio_engine.models import SpectralSignature

__all__ = [
    "AnalysisResult",
    "decode_s32le",
    "resample_mono",
    "analyse_samples",
    "BAND_EDGES_HZ",
]

#: Analysis frame: 4096 samples (~85 ms at 48 kHz) with a 50 % hop.
FRAME_SAMPLES = 4096
HOP_RATIO = 0.5

#: Band edges for the coarse spectral fingerprint (Hz).  Chosen to separate the
#: things that actually differ between two recordings of one speaker: low-end
#: rumble, body, presence and air.
BAND_EDGES_HZ: tuple[tuple[str, float, float], ...] = (
    ("sub_20_80", 20.0, 80.0),
    ("bass_80_250", 80.0, 250.0),
    ("body_250_1000", 250.0, 1_000.0),
    ("presence_1k_4k", 1_000.0, 4_000.0),
    ("brilliance_4k_8k", 4_000.0, 8_000.0),
    ("air_8k_16k", 8_000.0, 16_000.0),
)

_DB_FLOOR = 1e-12  # -240 dBFS: below this a sample is numerically silent


def _db(value: float) -> float | None:
    if value <= _DB_FLOOR:
        return None
    return float(20.0 * np.log10(value))


def decode_s32le(path: Path | str, *, channels: int, max_frames: int | None = None) -> np.ndarray:
    """Read a raw ``s32le`` decode as ``float64`` in ``[-1, 1]``.

    Chunked (bounded memory).  ``max_frames`` truncates explicitly; the caller
    knows from the returned shape how much it actually got.
    """
    if channels < 1:
        raise ValueError("channels must be >= 1")
    scale = float(2**31)
    chunks: list[np.ndarray] = []
    remaining = max_frames
    with Path(path).open("rb") as handle:
        while True:
            if remaining is not None and remaining <= 0:
                break
            byte_budget = 4 * channels * 65536
            if remaining is not None:
                byte_budget = min(byte_budget, 4 * channels * remaining)
            raw = handle.read(byte_budget)
            if not raw:
                break
            usable = len(raw) - (len(raw) % 4)
            if usable == 0:
                break
            data = np.frombuffer(raw[:usable], dtype="<i4").astype(np.float64) / scale
            usable_frames = data.size - (data.size % channels)
            if usable_frames == 0:
                break
            block = data[:usable_frames].reshape(-1, channels)
            chunks.append(block)
            if remaining is not None:
                remaining -= block.shape[0]
    if not chunks:
        return np.zeros((0, channels), dtype=np.float64)
    return np.concatenate(chunks, axis=0)


def resample_mono(samples: np.ndarray, *, source_rate: int, target_rate: int) -> np.ndarray:
    """Polyphase-free linear resample of a mono signal (analysis quality only).

    Used exclusively to make two *signatures* comparable, never to produce
    deliverable audio — deliverables are resampled by FFmpeg's ``aresample``.
    Linear interpolation is honest for spectral statistics and costs ``O(n)``.
    """
    if source_rate == target_rate or samples.size == 0:
        return samples
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    duration = samples.size / float(source_rate)
    target_count = max(1, int(round(duration * target_rate)))
    source_positions = np.arange(target_count, dtype=np.float64) * (source_rate / target_rate)
    return np.interp(source_positions, np.arange(samples.size, dtype=np.float64), samples)


@dataclass(frozen=True)
class AnalysisResult:
    """Aggregated analysis of one bounded window (see module docstring)."""

    seconds_covered: float
    rms_dbfs: float | None
    peak_dbfs: float | None
    crest_factor_db: float | None
    dynamic_range_db: float | None
    spectral_centroid_hz: float | None
    zero_crossing_rate: float | None
    band_energy_ratio: dict[str, float] = field(default_factory=dict)

    def to_signature(
        self,
        *,
        sample_rate: int,
        channels: int,
        truncated: bool,
        integrated_lufs: float | None = None,
    ) -> SpectralSignature:
        return SpectralSignature(
            sample_rate=sample_rate,
            channels=channels,
            samples_analyzed=int(round(self.seconds_covered * sample_rate)),
            analysis_seconds=round(self.seconds_covered, 4),
            truncated=truncated,
            rms_dbfs=self.rms_dbfs,
            peak_dbfs=self.peak_dbfs,
            crest_factor_db=self.crest_factor_db,
            dynamic_range_db=self.dynamic_range_db,
            spectral_centroid_hz=self.spectral_centroid_hz,
            zero_crossing_rate=self.zero_crossing_rate,
            band_energy_ratio=dict(self.band_energy_ratio),
            integrated_lufs=integrated_lufs,
        )


def analyse_samples(
    samples: np.ndarray,
    *,
    sample_rate: int,
    channels: int,
    frame_samples: int = FRAME_SAMPLES,
) -> AnalysisResult:
    """Aggregate statistics over a mono float signal (bounded memory)."""
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim != 1:
        raise ValueError("analyse_samples expects a mono (1-D) signal")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if data.size == 0:
        return AnalysisResult(0.0, None, None, None, None, None, None, {})

    seconds = data.size / float(sample_rate)
    peak = float(np.max(np.abs(data)))
    energy = float(np.mean(np.square(data)))
    rms = float(np.sqrt(energy))
    peak_db = _db(peak)
    rms_db = _db(rms)
    crest = (peak_db - rms_db) if peak_db is not None and rms_db is not None else None

    # Frame-wise aggregates.
    hop = max(1, int(frame_samples * HOP_RATIO))
    starts = np.arange(0, max(1, data.size - frame_samples + 1), hop)
    if data.size >= frame_samples and starts.size:
        window = np.lib.stride_tricks.sliding_window_view(data, frame_samples)[::hop]
    else:
        window = data.reshape(1, -1)
    frame_energy = np.mean(np.square(window), axis=1)
    frame_rms = np.sqrt(np.maximum(frame_energy, 0.0))
    loud = frame_rms[frame_rms > 10 ** (-60 / 20)]  # ignore frames below -60 dBFS
    dynamic_range = None
    if loud.size >= 2:
        loud_db = 20.0 * np.log10(loud)
        dynamic_range = float(np.percentile(loud_db, 95) - np.percentile(loud_db, 10))

    # Zero-crossing rate (per frame, mean).
    signs = np.signbit(data)
    crossings = np.count_nonzero(signs[1:] != signs[:-1])
    zcr = float(crossings / data.size) if data.size > 1 else None

    # Spectral centroid + band energies over the same bounded frames.
    taper = np.hanning(window.shape[1])
    centroids: list[float] = []
    band_sums = {name: 0.0 for name, _, _ in BAND_EDGES_HZ}
    total_energy = 0.0
    total_frames = window.shape[0]
    # 96 kHz at 4096 samples is 23 Hz bins — plenty for band separation.
    for index in range(total_frames):
        segment = window[index] * taper
        spectrum = np.abs(np.fft.rfft(segment))
        power = np.square(spectrum)
        frame_total = float(power.sum())
        if frame_total <= _DB_FLOOR:
            continue
        freqs = np.fft.rfftfreq(segment.size, d=1.0 / sample_rate)
        centroids.append(float(np.dot(freqs, power) / frame_total))
        for name, low, high in BAND_EDGES_HZ:
            if low >= sample_rate / 2:  # band above Nyquist: honestly zero
                continue
            mask = (freqs >= low) & (freqs < high)
            band_sums[name] += float(power[mask].sum())
        total_energy += frame_total
    centroid = float(np.mean(centroids)) if centroids else None
    bands: dict[str, float] = {}
    if total_energy > 0:
        for name, low, _ in BAND_EDGES_HZ:
            if low >= sample_rate / 2:
                bands[name] = 0.0
                continue
            bands[name] = round(band_sums[name] / total_energy, 6)

    return AnalysisResult(
        seconds_covered=seconds,
        rms_dbfs=rms_db,
        peak_dbfs=peak_db,
        crest_factor_db=crest,
        dynamic_range_db=dynamic_range,
        spectral_centroid_hz=centroid,
        zero_crossing_rate=zcr,
        band_energy_ratio=bands,
    )
