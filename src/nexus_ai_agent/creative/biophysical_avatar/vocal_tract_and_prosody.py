"""Deterministic source-filter speech synthesis and phoneme-timed visemes.

This module implements a lightweight, procedural glottal source followed by a
five-band formant resonator.  It is intended for repeatable previs and dialogue
blocking, not as a replacement for a licensed production voice or a neural
TTS service.
"""

from __future__ import annotations

import math
import wave
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from nexus_ai_agent.creative.biophysical_avatar.entity_dna_seal import EntityDNASeal

__all__ = [
    "ENGLISH_PHONEMES",
    "MAX_SYNTHESIS_SECONDS",
    "PERSIAN_PHONEMES",
    "PHONEME_FORMANTS",
    "Emotion",
    "MouthPose",
    "PhonemeSegment",
    "ProsodyParameters",
    "SpeechSynthesis",
    "VisemeKeyframe",
    "VisemeTrajectory",
    "VoiceProfile",
    "VOICE_ARCHETYPES",
    "build_viseme_trajectory",
    "phonemize_text",
    "synthesize_wav",
    "voice_profile_from_seal",
]

FloatArray = NDArray[np.float64]
AudioArray = NDArray[np.float32]
REFERENCE_VTL_M = 0.17
MAX_SYNTHESIS_SECONDS = 12.0


@dataclass(frozen=True, slots=True)
class PhonemeTarget:
    """Five-formant target and normalized lip/jaw articulatory controls."""

    formants_hz: tuple[float, float, float, float, float]
    mouth_open: float
    jaw_open: float
    lip_rounding: float

    def __post_init__(self) -> None:
        if len(self.formants_hz) != 5 or any(
            not math.isfinite(value) or value <= 0.0 for value in self.formants_hz
        ):
            raise ValueError("phoneme target must have five positive formants")
        if any(
            not math.isfinite(value) or value < 0.0 or value > 1.0
            for value in (self.mouth_open, self.jaw_open, self.lip_rounding)
        ):
            raise ValueError("viseme controls must lie in [0, 1]")


PERSIAN_PHONEMES: tuple[str, ...] = (
    "آ",
    "اَ",
    "اِ",
    "اُ",
    "ای",
    "او",
    "ب",
    "پ",
    "ت",
    "د",
    "ک",
    "گ",
    "ق",
    "ء",
    "ف",
    "و",
    "س",
    "ز",
    "ش",
    "ژ",
    "خ",
    "ه",
    "چ",
    "ج",
    "م",
    "ن",
    "ل",
    "ر",
    "ی",
)

PHONEME_FORMANTS: dict[str, PhonemeTarget] = {
    "آ": PhonemeTarget((850.0, 1350.0, 2500.0, 3500.0, 4500.0), 0.82, 0.76, 0.04),
    "اَ": PhonemeTarget((700.0, 1350.0, 2450.0, 3500.0, 4500.0), 0.76, 0.72, 0.03),
    "اِ": PhonemeTarget((390.0, 2050.0, 2800.0, 3650.0, 4650.0), 0.44, 0.38, 0.06),
    "اُ": PhonemeTarget((470.0, 1050.0, 2400.0, 3450.0, 4450.0), 0.48, 0.42, 0.58),
    "ای": PhonemeTarget((310.0, 2250.0, 3050.0, 3900.0, 4800.0), 0.34, 0.28, 0.08),
    "او": PhonemeTarget((320.0, 900.0, 2200.0, 3350.0, 4350.0), 0.38, 0.32, 0.88),
    "ب": PhonemeTarget((300.0, 1050.0, 2200.0, 3300.0, 4300.0), 0.16, 0.12, 0.08),
    "پ": PhonemeTarget((280.0, 1000.0, 2150.0, 3250.0, 4250.0), 0.14, 0.10, 0.08),
    "ت": PhonemeTarget((420.0, 1800.0, 2800.0, 3700.0, 4700.0), 0.18, 0.12, 0.04),
    "د": PhonemeTarget((450.0, 1650.0, 2700.0, 3600.0, 4600.0), 0.20, 0.14, 0.04),
    "ک": PhonemeTarget((380.0, 1550.0, 2600.0, 3600.0, 4600.0), 0.18, 0.12, 0.06),
    "گ": PhonemeTarget((400.0, 1450.0, 2550.0, 3550.0, 4550.0), 0.20, 0.14, 0.05),
    "ق": PhonemeTarget((420.0, 1250.0, 2400.0, 3500.0, 4500.0), 0.20, 0.14, 0.08),
    "ء": PhonemeTarget((500.0, 1400.0, 2500.0, 3500.0, 4500.0), 0.10, 0.08, 0.0),
    "ف": PhonemeTarget((500.0, 1900.0, 3000.0, 4000.0, 5000.0), 0.24, 0.18, 0.06),
    "و": PhonemeTarget((350.0, 1000.0, 2300.0, 3400.0, 4400.0), 0.22, 0.18, 0.46),
    "س": PhonemeTarget((450.0, 2100.0, 3300.0, 4500.0, 5500.0), 0.22, 0.16, 0.04),
    "ز": PhonemeTarget((460.0, 2000.0, 3200.0, 4400.0, 5400.0), 0.24, 0.18, 0.04),
    "ش": PhonemeTarget((470.0, 1800.0, 3000.0, 4200.0, 5200.0), 0.26, 0.20, 0.08),
    "ژ": PhonemeTarget((480.0, 1750.0, 2900.0, 4100.0, 5100.0), 0.26, 0.20, 0.08),
    "خ": PhonemeTarget((430.0, 1500.0, 2850.0, 3950.0, 4950.0), 0.28, 0.22, 0.08),
    "ه": PhonemeTarget((500.0, 1450.0, 2550.0, 3550.0, 4550.0), 0.32, 0.26, 0.04),
    "چ": PhonemeTarget((420.0, 1700.0, 2900.0, 4050.0, 5050.0), 0.24, 0.18, 0.08),
    "ج": PhonemeTarget((430.0, 1600.0, 2800.0, 3950.0, 4950.0), 0.26, 0.20, 0.06),
    "م": PhonemeTarget((260.0, 1050.0, 2250.0, 3350.0, 4350.0), 0.16, 0.12, 0.12),
    "ن": PhonemeTarget((300.0, 1450.0, 2500.0, 3550.0, 4550.0), 0.22, 0.16, 0.04),
    "ل": PhonemeTarget((360.0, 1550.0, 2650.0, 3650.0, 4650.0), 0.24, 0.18, 0.04),
    "ر": PhonemeTarget((390.0, 1450.0, 2600.0, 3600.0, 4600.0), 0.22, 0.16, 0.04),
    "ی": PhonemeTarget((330.0, 1900.0, 2850.0, 3750.0, 4750.0), 0.30, 0.24, 0.08),
}

# Native letters map to broad phoneme classes; common digraphs are handled
# before single characters.  Every ASCII English letter has an explicit entry.
ENGLISH_PHONEMES: dict[str, str] = {
    "a": "اَ",
    "b": "ب",
    "c": "ک",
    "d": "د",
    "e": "اِ",
    "f": "ف",
    "g": "گ",
    "h": "ه",
    "i": "ای",
    "j": "ج",
    "k": "ک",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "o": "آ",
    "p": "پ",
    "q": "ق",
    "r": "ر",
    "s": "س",
    "t": "ت",
    "u": "او",
    "v": "و",
    "w": "و",
    "x": "ک",
    "y": "ی",
    "z": "ز",
}
_ENGLISH_DIGRAPHS: dict[str, str] = {
    "ch": "چ",
    "ee": "ای",
    "ng": "ن",
    "oo": "او",
    "ph": "ف",
    "sh": "ش",
    "th": "ت",
}
_PERSIAN_ALIASES = {
    "أ": "آ",
    "إ": "اِ",
    "ٱ": "آ",
    "ي": "ی",
    "ك": "ک",
    "ث": "س",
    "ص": "س",
    "ذ": "ز",
    "ض": "ز",
    "ظ": "ز",
    "ط": "ت",
    "ح": "ه",
    "ع": "ء",
    "غ": "ق",
}
_UNVOICED_PHONEMES = frozenset(("پ", "ت", "ک", "ف", "س", "ش", "چ", "خ"))


class Emotion(str, Enum):
    """Prosodic presets available to deterministic speech synthesis."""

    EPIC = "epic"
    CALM = "calm"
    ANGRY = "angry"
    SAD = "sad"
    EXCITED = "excited"
    WHISPER = "whisper"


@dataclass(frozen=True, slots=True)
class ProsodyParameters:
    """Pitch, loudness, speaking-rate, and breath adjustments for one emotion."""

    pitch_multiplier: float
    intensity: float
    rate: float
    breath_multiplier: float


PROSODY: dict[Emotion, ProsodyParameters] = {
    Emotion.EPIC: ProsodyParameters(0.92, 0.92, 0.88, 0.75),
    Emotion.CALM: ProsodyParameters(0.96, 0.64, 0.90, 0.90),
    Emotion.ANGRY: ProsodyParameters(1.16, 1.0, 1.10, 0.65),
    Emotion.SAD: ProsodyParameters(0.84, 0.58, 0.78, 1.15),
    Emotion.EXCITED: ProsodyParameters(1.20, 0.94, 1.16, 0.85),
    Emotion.WHISPER: ProsodyParameters(0.86, 0.42, 0.94, 2.8),
}


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """Compact reproducible voice identity with a bounded VTL scale factor."""

    name: str
    f0_hz: float
    vocal_tract_scale: float = 1.0
    open_quotient: float = 0.62
    jitter: float = 0.004
    shimmer: float = 0.018
    breathiness: float = 0.025

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("voice name must not be empty")
        if not math.isfinite(self.f0_hz) or not 45.0 <= self.f0_hz <= 600.0:
            raise ValueError("f0_hz must lie in [45, 600]")
        if not math.isfinite(self.vocal_tract_scale) or not 0.75 <= self.vocal_tract_scale <= 1.35:
            raise ValueError("vocal_tract_scale must lie in [0.75, 1.35]")
        if not math.isfinite(self.open_quotient) or not 0.35 <= self.open_quotient <= 0.85:
            raise ValueError("open_quotient must lie in [0.35, 0.85]")
        for name, value, upper in (
            ("jitter", self.jitter, 0.08),
            ("shimmer", self.shimmer, 0.25),
            ("breathiness", self.breathiness, 0.5),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= upper:
                raise ValueError(f"{name} is outside its physical range")

    @property
    def vocal_tract_length_m(self) -> float:
        return REFERENCE_VTL_M * self.vocal_tract_scale


VOICE_ARCHETYPES: dict[str, VoiceProfile] = {
    "epic_male": VoiceProfile("epic_male", 102.0, 1.12, 0.68, 0.003, 0.014, 0.018),
    "young_male": VoiceProfile("young_male", 132.0, 1.04, 0.62, 0.004, 0.018, 0.024),
    "expressive_female": VoiceProfile("expressive_female", 218.0, 0.88, 0.61, 0.006, 0.022, 0.030),
    "gentle_female": VoiceProfile("gentle_female", 188.0, 0.91, 0.65, 0.005, 0.026, 0.055),
    "elderly": VoiceProfile("elderly", 158.0, 0.98, 0.70, 0.018, 0.045, 0.075),
    "child": VoiceProfile("child", 292.0, 0.76, 0.58, 0.009, 0.030, 0.035),
    "robotic_sci_fi": VoiceProfile("robotic_sci_fi", 124.0, 1.08, 0.52, 0.0, 0.0, 0.006),
}


def voice_profile_from_seal(
    seal: EntityDNASeal,
    *,
    archetype: str = "young_male",
) -> VoiceProfile:
    """Derive a stable physical voice profile from the seal's 16-D vocal vector."""
    try:
        base = VOICE_ARCHETYPES[archetype]
    except KeyError as exc:
        raise ValueError(f"unknown voice archetype: {archetype}") from exc
    vector = seal.vocal
    vtl_scale = 0.75 + 0.60 * (0.5 + 0.5 * math.tanh(vector[0]))
    f0 = base.f0_hz * 2.0 ** (0.24 * math.tanh(vector[1]))
    jitter = min(0.08, max(0.0, base.jitter + 0.012 * (0.5 + 0.5 * math.tanh(vector[2]))))
    shimmer = min(0.25, max(0.0, base.shimmer + 0.025 * (0.5 + 0.5 * math.tanh(vector[3]))))
    breathiness = min(
        0.5,
        max(0.0, base.breathiness + 0.04 * (0.5 + 0.5 * math.tanh(vector[4]))),
    )
    return VoiceProfile(
        name=f"{archetype}:{seal.dna_sha256[:12]}",
        f0_hz=f0,
        vocal_tract_scale=vtl_scale,
        open_quotient=0.58 + 0.16 * (0.5 + 0.5 * math.tanh(vector[5])),
        jitter=jitter,
        shimmer=shimmer,
        breathiness=breathiness,
    )


def phonemize_text(text: str, language: str = "fa") -> tuple[str, ...]:
    """Map Persian/English text to a deterministic broad phoneme inventory."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not isinstance(language, str):
        raise ValueError("language must be a string")
    normalized_language = language.lower().replace("_", "-")
    if normalized_language not in {"fa", "fa-ir", "en", "en-us", "en-gb"}:
        raise ValueError("language must be Persian (fa) or English (en)")
    if not text.strip():
        raise ValueError("text must not be empty")
    normalized = text.casefold()
    if normalized_language.startswith("fa"):
        normalized = "".join(_PERSIAN_ALIASES.get(character, character) for character in normalized)
    result: list[str] = []
    index = 0
    while index < len(normalized):
        if normalized[index].isspace():
            if not result or result[-1] != "sil":
                result.append("sil")
            index += 1
            continue
        if normalized_language.startswith("en"):
            digraph = normalized[index : index + 2]
            if digraph in _ENGLISH_DIGRAPHS:
                result.append(_ENGLISH_DIGRAPHS[digraph])
                index += 2
                continue
            character = normalized[index]
            if character in ENGLISH_PHONEMES:
                result.append(ENGLISH_PHONEMES[character])
            elif character.isalpha():
                result.append("ه")
            elif not character.isalnum():
                if not result or result[-1] != "sil":
                    result.append("sil")
            index += 1
            continue
        matched = False
        for token in ("اَ", "اِ", "اُ", "ای", "او"):
            if normalized.startswith(token, index):
                result.append(token)
                index += len(token)
                matched = True
                break
        if matched:
            continue
        character = normalized[index]
        if character in PHONEME_FORMANTS:
            result.append(character)
        elif character == "ا":
            result.append("آ")
        elif character.isalpha():
            result.append("ه")
        elif not character.isalnum() and (not result or result[-1] != "sil"):
            result.append("sil")
        index += 1
    while result and result[-1] == "sil":
        result.pop()
    if not result:
        raise ValueError("text contains no supported phonemes")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PhonemeSegment:
    """Timed phoneme, with half-open audio sample interval in seconds."""

    phoneme: str
    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if not self.phoneme or not all(
            math.isfinite(value) for value in (self.start_seconds, self.end_seconds)
        ):
            raise ValueError("phoneme segment values must be finite and non-empty")
        if self.start_seconds < 0.0 or self.end_seconds <= self.start_seconds:
            raise ValueError("phoneme segment must have positive duration")


@dataclass(frozen=True, slots=True)
class MouthPose:
    """Normalized oral aperture sampled from the viseme timeline."""

    mouth_open: float
    jaw_open: float
    lip_rounding: float


@dataclass(frozen=True, slots=True)
class VisemeKeyframe:
    """One articulation keyframe synchronized to an absolute audio timestamp."""

    time_seconds: float
    pose: MouthPose


@dataclass(frozen=True, slots=True)
class VisemeTrajectory:
    """Piecewise-linear mouth motion with frame-independent sub-10ms timing."""

    keyframes: tuple[VisemeKeyframe, ...]

    def __post_init__(self) -> None:
        if not self.keyframes:
            raise ValueError("viseme trajectory requires at least one keyframe")
        times = tuple(frame.time_seconds for frame in self.keyframes)
        if any(not math.isfinite(time) or time < 0.0 for time in times):
            raise ValueError("viseme keyframe times must be finite and non-negative")
        if any(right <= left for left, right in zip(times, times[1:], strict=False)):
            raise ValueError("viseme keyframe times must be strictly increasing")

    @property
    def duration_seconds(self) -> float:
        return self.keyframes[-1].time_seconds

    def sample(self, time_seconds: float) -> MouthPose:
        """Interpolate at any time; timestamps are not rounded to video frames."""
        if not math.isfinite(time_seconds):
            raise ValueError("sample time must be finite")
        if time_seconds <= self.keyframes[0].time_seconds:
            return self.keyframes[0].pose
        if time_seconds >= self.keyframes[-1].time_seconds:
            return self.keyframes[-1].pose
        times = [frame.time_seconds for frame in self.keyframes]
        upper_index = bisect_right(times, time_seconds)
        lower = self.keyframes[upper_index - 1]
        upper = self.keyframes[upper_index]
        fraction = (time_seconds - lower.time_seconds) / (upper.time_seconds - lower.time_seconds)
        return MouthPose(
            mouth_open=lower.pose.mouth_open
            + fraction * (upper.pose.mouth_open - lower.pose.mouth_open),
            jaw_open=lower.pose.jaw_open + fraction * (upper.pose.jaw_open - lower.pose.jaw_open),
            lip_rounding=lower.pose.lip_rounding
            + fraction * (upper.pose.lip_rounding - lower.pose.lip_rounding),
        )

    def sample_at_audio_sample(self, sample_index: int, sample_rate: int) -> MouthPose:
        """Sample directly at a WAV sample index for exact audio/video alignment."""
        if sample_index < 0 or sample_rate <= 0:
            raise ValueError("sample_index must be non-negative and sample_rate positive")
        return self.sample(sample_index / sample_rate)


def _duration_for(phoneme: str) -> float:
    if phoneme == "sil":
        return 0.105
    target = PHONEME_FORMANTS[phoneme]
    is_vowel = phoneme in {"آ", "اَ", "اِ", "اُ", "ای", "او"}
    return 0.135 if is_vowel else 0.082 + 0.025 * target.mouth_open


def build_viseme_trajectory(
    phonemes: Sequence[str],
    *,
    rate: float = 1.0,
) -> tuple[tuple[PhonemeSegment, ...], VisemeTrajectory]:
    """Build phoneme durations and a smoothly sampled oral articulation path."""
    if not phonemes:
        raise ValueError("phonemes must not be empty")
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("rate must be finite and positive")
    segments: list[PhonemeSegment] = []
    time = 0.0
    keyframes: list[VisemeKeyframe] = []
    for phoneme in phonemes:
        if phoneme != "sil" and phoneme not in PHONEME_FORMANTS:
            raise ValueError(f"unknown phoneme: {phoneme}")
        duration = _duration_for(phoneme) / rate
        end = time + duration
        segment = PhonemeSegment(phoneme, time, end)
        segments.append(segment)
        if phoneme == "sil":
            pose = MouthPose(0.0, 0.0, 0.0)
        else:
            target = PHONEME_FORMANTS[phoneme]
            pose = MouthPose(target.mouth_open, target.jaw_open, target.lip_rounding)
        center = (time + end) * 0.5
        keyframes.append(VisemeKeyframe(center, pose))
        time = end
    if len(keyframes) == 1:
        first = keyframes[0]
        keyframes.insert(0, VisemeKeyframe(0.0, MouthPose(0.0, 0.0, 0.0)))
        keyframes.append(VisemeKeyframe(time, first.pose))
    else:
        keyframes.insert(0, VisemeKeyframe(0.0, keyframes[0].pose))
        if keyframes[-1].time_seconds < time:
            keyframes.append(VisemeKeyframe(time, keyframes[-1].pose))
    return tuple(segments), VisemeTrajectory(tuple(keyframes))


@dataclass(frozen=True, slots=True)
class SpeechSynthesis:
    """Mono floating-point speech plus a timestamped viseme trajectory."""

    samples: AudioArray
    sample_rate: int
    text: str
    language: str
    voice: VoiceProfile
    emotion: Emotion
    segments: tuple[PhonemeSegment, ...]
    visemes: VisemeTrajectory

    def __post_init__(self) -> None:
        samples = np.array(self.samples, dtype=np.float32, copy=True)
        if samples.ndim != 1 or samples.size == 0 or not np.isfinite(samples).all():
            raise ValueError("speech samples must be a non-empty finite mono signal")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        samples.flags.writeable = False
        object.__setattr__(self, "samples", samples)

    @property
    def duration_seconds(self) -> float:
        return self.samples.size / self.sample_rate

    def write_wav(self, path: str | Path) -> Path:
        """Write actual mono PCM16 WAV data at the synthesis sample rate."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        pcm = np.rint(np.clip(self.samples, -1.0, 1.0) * 32767.0).astype("<i2")
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            output.writeframes(pcm.tobytes())
        return destination


def _smooth_random_track(
    rng: np.random.Generator,
    sample_count: int,
    sample_rate: int,
    base_f0: float,
    deviation: float,
) -> FloatArray:
    if deviation == 0.0:
        return np.zeros(sample_count, dtype=np.float64)
    cycles = max(2, int(math.ceil(sample_count * base_f0 / sample_rate)) + 2)
    control = rng.normal(0.0, deviation, cycles)
    control_positions = np.linspace(0.0, sample_count - 1.0, cycles, dtype=np.float64)
    sample_positions = np.arange(sample_count, dtype=np.float64)
    return np.interp(sample_positions, control_positions, control)


def _glottal_source(
    sample_count: int,
    sample_rate: int,
    f0_track: FloatArray,
    profile: VoiceProfile,
    style: ProsodyParameters,
    rng: np.random.Generator,
) -> FloatArray:
    jitter_track = _smooth_random_track(
        rng, sample_count, sample_rate, profile.f0_hz, profile.jitter
    )
    shimmer_track = _smooth_random_track(
        rng, sample_count, sample_rate, profile.f0_hz, profile.shimmer
    )
    instantaneous_f0 = f0_track * np.clip(1.0 + jitter_track, 0.85, 1.15)
    phase = np.cumsum((2.0 * math.pi / sample_rate) * instantaneous_f0)
    cycle = np.mod(phase / (2.0 * math.pi), 1.0)
    quotient = profile.open_quotient
    opening = cycle < quotient
    source = np.empty(sample_count, dtype=np.float64)
    open_phase = cycle[opening] / quotient
    close_phase = (cycle[~opening] - quotient) / (1.0 - quotient)
    source[opening] = -np.sin(math.pi * open_phase)
    source[~opening] = 0.48 * np.sin(math.pi * close_phase) * np.exp(-1.6 * close_phase)
    source *= np.clip(1.0 + shimmer_track, 0.4, 1.6)
    breath = rng.normal(0.0, profile.breathiness * style.breath_multiplier * 0.025, sample_count)
    return source + breath


def _resonant_spectrum(
    source: FloatArray,
    sample_rate: int,
    formants_hz: tuple[float, float, float, float, float],
) -> FloatArray:
    if source.size == 0:
        return source.copy()
    spectrum = np.fft.rfft(source)
    frequencies = np.fft.rfftfreq(source.size, d=1.0 / sample_rate)
    response = np.full(frequencies.shape, 0.018, dtype=np.float64)
    bandwidths = (90.0, 125.0, 170.0, 230.0, 300.0)
    for formant, bandwidth in zip(formants_hz, bandwidths, strict=True):
        normalized = (frequencies - formant) / bandwidth
        response += 1.0 / (1.0 + normalized**4)
    peak_response = float(np.max(response))
    if peak_response > 0.0:
        response /= peak_response
    return np.fft.irfft(spectrum * response, n=source.size)


def _apply_formants(
    source: FloatArray,
    sample_rate: int,
    segments: Sequence[PhonemeSegment],
    profile: VoiceProfile,
) -> FloatArray:
    output = np.zeros_like(source)
    weights = np.zeros_like(source)
    scale = REFERENCE_VTL_M / profile.vocal_tract_length_m
    fade_length = max(1, int(round(sample_rate * 0.006)))
    for segment in segments:
        start = min(source.size, int(round(segment.start_seconds * sample_rate)))
        end = min(source.size, int(round(segment.end_seconds * sample_rate)))
        if end <= start:
            continue
        sample = source[start:end]
        if segment.phoneme == "sil":
            filtered = sample * 0.12
        else:
            target = PHONEME_FORMANTS[segment.phoneme]
            formants = cast(
                tuple[float, float, float, float, float],
                tuple(value * scale for value in target.formants_hz),
            )
            filtered = _resonant_spectrum(sample, sample_rate, formants)
        window = np.ones(end - start, dtype=np.float64)
        edge = min(fade_length, window.size // 2)
        if edge > 0:
            ramp = np.linspace(0.0, 1.0, edge, endpoint=False, dtype=np.float64)
            window[:edge] = ramp
            window[-edge:] = ramp[::-1]
        output[start:end] += filtered * window
        weights[start:end] += window
    valid = weights > 1e-12
    output[valid] /= weights[valid]
    return output


def synthesize_wav(
    text: str,
    *,
    voice: VoiceProfile | None = None,
    emotion: Emotion | str = Emotion.CALM,
    language: str = "fa",
    sample_rate: int = 48_000,
    seed: int = 0,
) -> SpeechSynthesis:
    """Synthesize reproducible mono PCM-ready speech and sub-10ms visemes."""
    profile = VOICE_ARCHETYPES["young_male"] if voice is None else voice
    try:
        selected_emotion = emotion if isinstance(emotion, Emotion) else Emotion(emotion)
    except ValueError as exc:
        raise ValueError(f"unsupported emotion: {emotion}") from exc
    if sample_rate < 8_000 or sample_rate > 96_000:
        raise ValueError("sample_rate must lie between 8000 and 96000 Hz")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    style = PROSODY[selected_emotion]
    phonemes = phonemize_text(text, language)
    segments, visemes = build_viseme_trajectory(phonemes, rate=style.rate)
    duration = segments[-1].end_seconds
    if duration > MAX_SYNTHESIS_SECONDS:
        raise ValueError(f"utterance exceeds the {MAX_SYNTHESIS_SECONDS:g}s memory budget")
    sample_count = max(1, int(round(duration * sample_rate)))
    times = np.arange(sample_count, dtype=np.float64) / sample_rate
    f0_track = np.full(sample_count, profile.f0_hz * style.pitch_multiplier, dtype=np.float64)
    amplitude = np.zeros(sample_count, dtype=np.float64)
    for segment in segments:
        start = max(0, int(round(segment.start_seconds * sample_rate)))
        end = min(sample_count, int(round(segment.end_seconds * sample_rate)))
        if segment.phoneme != "sil" and end > start:
            amplitude[start:end] = 1.0
    if duration > 0.0:
        intonation = 1.0 + 0.035 * np.sin(2.0 * math.pi * times / duration)
        f0_track *= intonation
    rng = np.random.default_rng(seed)
    source = _glottal_source(sample_count, sample_rate, f0_track, profile, style, rng)
    source *= amplitude
    filtered = _apply_formants(source, sample_rate, segments, profile)
    peak = float(np.max(np.abs(filtered)))
    target_peak = 0.72 * style.intensity
    if peak > 1e-12:
        filtered *= target_peak / peak
    filtered = np.clip(filtered, -0.98, 0.98)
    samples = filtered.astype(np.float32)
    return SpeechSynthesis(
        samples=samples,
        sample_rate=sample_rate,
        text=text,
        language=language,
        voice=profile,
        emotion=selected_emotion,
        segments=segments,
        visemes=visemes,
    )
