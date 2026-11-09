"""Voice-identity and motion-identity measurements (synthetic sources, real DSP).

What is real here
-----------------
* :func:`synthesize_voice` renders a deterministic speech-like signal from a
  :class:`VoiceSignature` (pitch, formants, jitter, vibrato, breathiness) using
  additive harmonic synthesis. That is signal generation, not a voice model: it
  cannot clone anybody, which is why it is safe to ship in a prototype that must
  not touch a real person's voice.
* :func:`voice_features` is ordinary DSP — autocorrelation pitch tracking, mel
  band energies, spectral centroid/rolloff, harmonicity and zero-crossing rate.
  Speaker-discriminating *in the synthetic domain*, exactly like the visual
  descriptors: the machinery is measurable, the transfer to real speech needs a
  licensed speaker-embedding model (deferred, and stated as such).
* :func:`synthesize_gait` / :func:`gait_features` do the same for motion on a
  7-joint skeleton: cadence, stride, arm swing, hip sway, head bob, and the
  arm/foot phase relationship.

No file here reads, writes or embeds a real voice. Any real-voice capsule must
carry a consent record covering ``VOICE_SYNTHESIS`` before
:meth:`VoiceModule.voice_origin` may be ``CONSENTED_REAL`` (see ``security``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .model import GaitProfile, VoiceModule

#: Formant tables per timbre label — three F1/F2/F3 triples, in Hz.
FORMANTS: dict[str, tuple[float, float, float]] = {
    "warm": (520.0, 1480.0, 2480.0),
    "bright": (610.0, 1820.0, 2900.0),
    "deep": (430.0, 1180.0, 2320.0),
    "nasal": (480.0, 2100.0, 2650.0),
    "breathy": (505.0, 1560.0, 2500.0),
}


def _rng(material: str) -> np.random.Generator:
    digest = hashlib.sha256(material.encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


@dataclass(frozen=True)
class VoiceSignature:
    """The measurable voice identity derived from a capsule's voice module."""

    f0_hz: float
    timbre: str
    speaking_rate: float
    jitter: float = 0.008
    vibrato_hz: float = 5.2
    vibrato_depth: float = 0.006
    breathiness: float = 0.05

    @classmethod
    def from_module(cls, module: VoiceModule) -> VoiceSignature:
        breathiness = 0.22 if module.timbre == "breathy" else 0.05
        return cls(
            f0_hz=float(module.f0_hz),
            timbre=module.timbre,
            speaking_rate=float(module.speaking_rate),
            breathiness=breathiness,
        )

    def key(self) -> str:
        return (
            f"{self.f0_hz:.2f}|{self.timbre}|{self.speaking_rate:.3f}|"
            f"{self.jitter:.4f}|{self.vibrato_hz:.2f}|{self.vibrato_depth:.4f}|"
            f"{self.breathiness:.3f}"
        )


def synthesize_voice(
    signature: VoiceSignature, *, seconds: float = 1.6, sample_rate: int = 16000
) -> np.ndarray:
    """Deterministic additive synthesis of a vowel-like utterance."""
    if seconds <= 0 or sample_rate < 8000:
        raise ValueError("invalid synthesis parameters")
    count = int(seconds * sample_rate)
    t = np.arange(count, dtype=np.float64) / sample_rate
    rng = _rng("voice|" + signature.key())
    # pitch contour: gentle declination + vibrato + jitter
    declination = 1.0 + 0.06 * np.cos(2 * np.pi * 0.4 * t)
    vibrato = 1.0 + signature.vibrato_depth * np.sin(2 * np.pi * signature.vibrato_hz * t)
    jitter = 1.0 + signature.jitter * rng.normal(0.0, 1.0, size=count).cumsum() / np.sqrt(count)
    f0_track = signature.f0_hz * declination * vibrato * jitter
    phase = 2 * np.pi * np.cumsum(f0_track) / sample_rate
    formants = FORMANTS.get(signature.timbre, FORMANTS["warm"])
    signal = np.zeros(count, dtype=np.float64)
    for harmonic in range(1, 25):
        freq = harmonic * signature.f0_hz
        if freq > sample_rate / 2.2:
            break
        weight = 0.0
        for index, formant in enumerate(formants):
            bandwidth = 90.0 + 40.0 * index
            weight += np.exp(-0.5 * ((freq - formant) / bandwidth) ** 2)
        weight += 0.12 / harmonic  # glottal tilt
        signal += weight * np.sin(harmonic * phase) / harmonic**0.35
    # syllables: amplitude envelope shaped by the speaking rate
    syllable_hz = 3.4 * signature.speaking_rate
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * syllable_hz * t) ** 2
    envelope *= np.clip(np.minimum(t * 12.0, (seconds - t) * 12.0), 0.0, 1.0)
    if signature.breathiness > 0:
        signal = signal + signature.breathiness * rng.normal(0.0, 1.0, size=count)
    signal = signal * envelope
    peak = float(np.max(np.abs(signal))) or 1.0
    return 0.85 * signal / peak


def _frames(samples: np.ndarray, frame: int, hop: int) -> np.ndarray:
    if samples.size < frame:
        return np.zeros((0, frame))
    count = 1 + (samples.size - frame) // hop
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    return samples[index]


def voice_features(samples: np.ndarray, *, sample_rate: int = 16000) -> dict[str, np.ndarray]:
    """Measure pitch, spectral shape and harmonicity from a waveform."""
    signal = np.asarray(samples, dtype=np.float64).ravel()
    if signal.size < sample_rate // 10:
        raise ValueError("signal too short to measure (need >= 100 ms)")
    frame = int(0.040 * sample_rate)
    hop = int(0.020 * sample_rate)
    frames = _frames(signal, frame, hop)
    window = np.hanning(frame)
    windowed = frames * window
    spectra = np.abs(np.fft.rfft(windowed, axis=1))
    freqs = np.fft.rfftfreq(frame, d=1.0 / sample_rate)
    energy = spectra.sum(axis=1)
    voiced_frames = frames[energy > np.percentile(energy, 35)] if frames.size else frames
    if voiced_frames.size == 0:
        voiced_frames = frames
    # autocorrelation pitch per voiced frame
    pitches: list[float] = []
    harmonicity: list[float] = []
    min_lag = int(sample_rate / 400)
    max_lag = int(sample_rate / 70)
    for raw in voiced_frames:
        centred = raw - raw.mean()
        norm = float(np.dot(centred, centred))
        if norm <= 1e-12:
            continue
        correlation = np.correlate(centred, centred, mode="full")[len(centred) - 1 :]
        correlation = correlation / norm
        segment = correlation[min_lag:max_lag]
        if segment.size == 0:
            continue
        peak = int(np.argmax(segment))
        harmonicity.append(float(segment[peak]))
        lag = min_lag + peak
        if lag > 0:
            pitches.append(float(sample_rate) / lag)
    f0 = float(np.median(pitches)) if pitches else 0.0
    f0_spread = float(np.std(pitches)) if pitches else 0.0
    centroid = float(
        np.average(freqs, weights=spectra.sum(axis=0) + 1e-12) if spectra.size else 0.0
    )
    cumulative = np.cumsum(spectra.sum(axis=0))
    total = float(cumulative[-1]) if cumulative.size else 0.0
    rolloff = float(freqs[int(np.searchsorted(cumulative, 0.85 * total))]) if total > 0 else 0.0
    # mel-ish band energies: 16 log-spaced bands between 80 Hz and 7 kHz
    edges = np.geomspace(80.0, min(7000.0, sample_rate / 2.0 - 1.0), 17)
    bands = np.zeros(16, dtype=np.float64)
    power = spectra.sum(axis=0)
    for index in range(16):
        selector = (freqs >= edges[index]) & (freqs < edges[index + 1])
        bands[index] = power[selector].mean() if selector.any() else 0.0
    bands = bands / (bands.sum() + 1e-12)
    zcr = float(np.mean(np.abs(np.diff(np.signbit(signal).astype(np.int8)))))
    return {
        "pitch": np.array([f0, f0_spread, float(np.mean(harmonicity)) if harmonicity else 0.0]),
        "spectral": np.array([centroid, rolloff, zcr]),
        "bands": bands,
    }


def voice_vector(features: dict[str, np.ndarray]) -> np.ndarray:
    return np.concatenate([np.ravel(features[key]) for key in sorted(features)]).astype(np.float64)


def voice_distance(
    reference: dict[str, np.ndarray],
    candidate: dict[str, np.ndarray],
    *,
    stats: dict[str, list[float]] | None = None,
) -> float:
    a = voice_vector(reference)
    b = voice_vector(candidate)
    if stats is not None:
        mean = np.asarray(stats["mean"], dtype=np.float64)
        std = np.asarray(stats["std"], dtype=np.float64)
        if mean.shape != a.shape:
            raise ValueError("voice calibration stats shape mismatch")
        a = (a - mean) / np.where(std < 1e-6, 1.0, std)
        b = (b - mean) / np.where(std < 1e-6, 1.0, std)
    diff = a - b
    return float(np.sqrt(float(np.mean(diff * diff))))


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

JOINTS = ("hip", "shoulder", "head", "left_foot", "right_foot", "left_hand", "right_hand")


def synthesize_gait(
    profile: GaitProfile, *, frames: int = 64, fps: float = 24.0, seed: str = "gait"
) -> np.ndarray:
    """Deterministic 2-D skeletal walk cycle: ``(frames, len(JOINTS), 2)``.

    Coordinates are normalised to the character's height (y down, x across).
    The per-character parameters change the *observable* cadence, stride, arm
    swing, sway and bob, which is what the motion metrics measure.
    """
    if frames < 8:
        raise ValueError("a gait sequence needs at least 8 frames")
    t = np.arange(frames, dtype=np.float64) / fps
    steps_per_second = profile.cadence_steps_per_min / 60.0
    phase = 2 * np.pi * steps_per_second * t
    rng = _rng("motion|" + seed + "|" + str(profile))
    sway = profile.hip_sway_ratio * np.sin(phase)
    bob = profile.head_bob_ratio * np.abs(np.sin(phase * 2))
    stride = profile.stride_ratio * 0.5
    arm = profile.arm_swing_ratio * 0.5
    sequence = np.zeros((frames, len(JOINTS), 2), dtype=np.float64)
    sequence[:, 0, :] = np.stack([0.5 + sway, 0.52 - bob * 0.2], axis=1)  # hip
    sequence[:, 1, :] = np.stack([0.5 + sway * 0.7, 0.36 - bob * 0.2], axis=1)  # shoulder
    sequence[:, 2, :] = np.stack([0.5 + sway * 0.4, 0.24 - bob], axis=1)  # head
    sequence[:, 3, :] = np.stack(
        [0.5 + stride * np.sin(phase), 0.94 - 0.04 * np.abs(np.sin(phase))], axis=1
    )
    sequence[:, 4, :] = np.stack(
        [0.5 - stride * np.sin(phase), 0.94 - 0.04 * np.abs(np.sin(phase))], axis=1
    )
    hand_height = np.full(t.shape, 0.55)
    sequence[:, 5, :] = np.stack([0.5 - arm * np.sin(phase), hand_height], axis=1)
    sequence[:, 6, :] = np.stack([0.5 + arm * np.sin(phase), hand_height], axis=1)
    sequence += rng.normal(0.0, 0.0015, size=sequence.shape)
    return sequence


def gait_features(sequence: np.ndarray, *, fps: float = 24.0) -> dict[str, np.ndarray]:
    """Cadence, stride, swing, sway, bob and arm/foot phase from a skeleton."""
    array = np.asarray(sequence, dtype=np.float64)
    if array.ndim != 3 or array.shape[1] != len(JOINTS):
        raise ValueError("gait_features expects (frames, 7, 2)")
    if array.shape[0] < 8:
        raise ValueError("sequence too short")
    left_foot = array[:, 3, 0]
    right_foot = array[:, 4, 0]
    separation = left_foot - right_foot
    crossings = int(np.sum(np.diff(np.signbit(separation)).astype(np.int8) != 0))
    duration = max(array.shape[0] / fps, 1e-6)
    cadence = crossings / duration * 60.0
    stride = float(np.mean(np.abs(separation)))
    arm_swing = float(np.mean(np.abs(array[:, 5, 0] - array[:, 6, 0])))
    sway = float(np.std(array[:, 0, 0]))
    bob = float(np.ptp(array[:, 2, 1]))
    foot_velocity = np.diff(left_foot)
    arm_velocity = np.diff(array[:, 5, 0])
    if foot_velocity.size > 2 and np.std(foot_velocity) > 1e-9 and np.std(arm_velocity) > 1e-9:
        phase_correlation = float(np.corrcoef(foot_velocity, arm_velocity)[0, 1])
    else:
        phase_correlation = 0.0
    return {
        "gait": np.array(
            [cadence, stride, arm_swing, sway, bob, phase_correlation, float(np.ptp(left_foot))],
            dtype=np.float64,
        )
    }


def motion_vector(features: dict[str, np.ndarray]) -> np.ndarray:
    return np.concatenate([np.ravel(features[key]) for key in sorted(features)]).astype(np.float64)


def motion_distance(
    reference: dict[str, np.ndarray],
    candidate: dict[str, np.ndarray],
    *,
    stats: dict[str, list[float]] | None = None,
) -> float:
    a = motion_vector(reference)
    b = motion_vector(candidate)
    if stats is not None:
        mean = np.asarray(stats["mean"], dtype=np.float64)
        std = np.asarray(stats["std"], dtype=np.float64)
        if mean.shape != a.shape:
            raise ValueError("motion calibration stats shape mismatch")
        a = (a - mean) / np.where(std < 1e-6, 1.0, std)
        b = (b - mean) / np.where(std < 1e-6, 1.0, std)
    diff = a - b
    return float(np.sqrt(float(np.mean(diff * diff))))


def feature_payload(features: dict[str, np.ndarray]) -> dict[str, Any]:
    """JSON-safe, rounded copy of a feature dict (for evidence bundles)."""
    return {
        key: [round(float(value), 6) for value in np.ravel(values)]
        for key, values in features.items()
    }
