"""Typed models for the ``nexus.audio.studio`` pack (Wave 5 substrate).

This module defines the typed contracts for studio audio processing, beat detection,
loudness normalization, ducking, and beat-synchronized timeline cuts in Nagar.
In accordance with Nagar architecture, this layer is purely declarative:
stdlib + pydantic only, no I/O, no heavy DSP or PyTorch imports.

Surface:
* :class:`BeatMarker` — time position, confidence, and downbeat status of an audio beat
* :class:`BeatGridRef` — container of detected tempo and beat sequence for an audio asset
* :class:`DetectBeatsInput` — input for beat detection
* :class:`NormalizeLoudnessInput` — input for EBU R128 / streaming loudness normalization
* :class:`DuckMusicInput` — input for sidechain / voice-over music ducking
* :class:`BeatSyncCutInput` — input for snapping timeline visual clips to audio beats
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AUDIO_PACKAGE_ID = "nexus.audio.studio"
DOMAIN = "audio"

OPERATION_DETECT_BEATS = "audio.detect_beats"
OPERATION_NORMALIZE_LOUDNESS = "audio.normalize_loudness"
OPERATION_DUCK_MUSIC = "audio.duck_music"
OPERATION_BEAT_SYNC_CUT = "audio.beat_sync_cut"
OPERATION_REMOVE_NOISE = "audio.remove_noise"
OPERATION_DEESS = "audio.deess"
OPERATION_EQ_VOICE = "audio.eq_voice"
OPERATION_TIME_STRETCH = "audio.time_stretch"
OPERATION_REMOVE_VOCAL = "audio.remove_vocal"
OPERATION_ALIGN_MUSIC = "audio.align_music"

#: Stem layouts for ``audio.remove_vocal``: the source-separation policy decides
#: how many derived stem assets the operation derives.
StemPolicy = Literal["two_stem", "four_stem"]

STEM_LAYOUTS: dict[str, tuple[str, ...]] = {
    "two_stem": ("vocals", "accompaniment"),
    "four_stem": ("vocals", "drums", "bass", "other"),
}

#: Beat-grid anchor policies for ``audio.align_music``.
MusicAnchor = Literal["first_beat", "nearest_beat"]

#: Deterministic voice-EQ preset tables: band name → gain in dB (master
#: ``gain_db`` is added on top by the handler). Pure data, no DSP here.
VOICE_EQ_PRESETS: dict[str, dict[str, float]] = {
    "warm": {"low_shelf_120hz": 3.0, "presence_3khz": -1.5, "air_12khz": -2.0},
    "bright": {"low_shelf_120hz": -2.0, "presence_3khz": 2.5, "air_12khz": 3.0},
    "broadcast": {"body_250hz": 1.5, "presence_3khz": 2.0, "air_12khz": 1.0},
    "telephone": {"presence_2khz": 3.0},
}

#: High/low-pass cuts applied per preset (names only — the lane compiles them).
VOICE_EQ_CUTS: dict[str, tuple[str, ...]] = {
    "warm": (),
    "bright": (),
    "broadcast": ("high_pass_80hz",),
    "telephone": ("high_pass_300hz", "low_pass_3400hz"),
}


class BeatMarker(BaseModel):
    """Timestamp and metric metadata for an individual detected beat."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_us: int = Field(ge=0, description="Beat onset in integer microseconds.")
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Detection confidence score [0.0, 1.0]."
    )
    beat_number: int = Field(ge=1, description="1-based index of the beat in sequence.")
    is_downbeat: bool = Field(
        default=False, description="True if this beat represents a measure downbeat (e.g. beat 1)."
    )


class BeatGridRef(BaseModel):
    """Beat grid metadata for an audio track, containing detected BPM and beat markers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_asset_id: str = Field(min_length=1)
    tempo_bpm: float = Field(gt=0.0, description="Estimated tempo in beats per minute.")
    beats: list[BeatMarker] = Field(default_factory=list)
    total_beats: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_beat_count(self) -> BeatGridRef:
        if self.total_beats != len(self.beats):
            object.__setattr__(self, "total_beats", len(self.beats))
        return self


class DetectBeatsInput(BaseModel):
    """Input payload for ``audio.detect_beats`` (Level A)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    min_bpm: float = Field(default=60.0, gt=0.0)
    max_bpm: float = Field(default=200.0, gt=0.0)
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_bpm_range(self) -> DetectBeatsInput:
        if self.max_bpm <= self.min_bpm:
            raise ValueError(f"max_bpm ({self.max_bpm}) must be > min_bpm ({self.min_bpm})")
        return self


class NormalizeLoudnessInput(BaseModel):
    """Input payload for ``audio.normalize_loudness`` (Level C)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    target_lufs: float = Field(
        default=-14.0, ge=-70.0, le=0.0, description="Target integrated loudness in LUFS."
    )
    true_peak_db: float = Field(
        default=-1.0, ge=-20.0, le=0.0, description="Maximum allowed true peak in dBFS."
    )
    output_asset_id: str | None = None
    confirmed: bool = Field(
        default=False, description="Explicit user confirmation required for Level C execution."
    )


class DuckMusicInput(BaseModel):
    """Input payload for ``audio.duck_music`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    music_asset_id: str = Field(min_length=1)
    voice_asset_id: str = Field(min_length=1)
    duck_attenuation_db: float = Field(
        default=-12.0, le=0.0, ge=-60.0, description="Attenuation applied during speech in dB."
    )
    attack_ms: int = Field(default=100, ge=10, description="Fade-down duration in milliseconds.")
    release_ms: int = Field(default=400, ge=10, description="Fade-up duration in milliseconds.")
    output_asset_id: str | None = None


class BeatSyncCutInput(BaseModel):
    """Input payload for ``audio.beat_sync_cut`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_ids: list[str] = Field(min_length=1)
    audio_asset_id: str = Field(min_length=1)
    beats_per_cut: int = Field(
        default=4, ge=1, description="Number of beats per clip cut boundary."
    )
    timeline_track_id: str = Field(default="video_main", min_length=1)


class RemoveNoiseInput(BaseModel):
    """Input payload for ``audio.remove_noise`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    strength: float = Field(default=0.6, ge=0.0, le=1.0)
    preserve_speech: bool = True
    output_asset_id: str | None = None


class DeessInput(BaseModel):
    """Input payload for ``audio.deess`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    frequency_hz: float = Field(default=6500.0, ge=2000.0, le=12000.0)
    threshold_db: float = Field(default=-24.0, ge=-60.0, le=0.0)
    output_asset_id: str | None = None


class EqVoiceInput(BaseModel):
    """Input payload for ``audio.eq_voice`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    preset: str = Field(default="broadcast")
    gain_db: float = Field(default=0.0, ge=-12.0, le=12.0)
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_preset(self) -> EqVoiceInput:
        if self.preset not in VOICE_EQ_PRESETS:
            raise ValueError(f"unknown voice EQ preset: {self.preset!r}")
        return self


class TimeStretchInput(BaseModel):
    """Input payload for ``audio.time_stretch`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    factor: float = Field(default=1.0, ge=0.25, le=4.0)
    preserve_pitch: bool = True
    output_asset_id: str | None = None


class RemoveVocalInput(BaseModel):
    """Input payload for ``audio.remove_vocal`` (Level B).

    ``stem_policy`` selects the separation layout; every stem becomes its own
    content-addressed derived asset, so the caller can reference (and later
    re-render) the isolated stems without re-running the separator.
    """

    model_config = ConfigDict(extra="forbid")

    audio_asset_id: str = Field(min_length=1)
    stem_policy: StemPolicy = "two_stem"
    strength: float = Field(default=0.85, ge=0.0, le=1.0)
    output_asset_id: str | None = Field(
        default=None, description="Optional prefix for the derived stem asset ids."
    )


class AlignMusicInput(BaseModel):
    """Input payload for ``audio.align_music`` (Level B).

    The beat grid travels as the two numbers ``audio.detect_beats`` derives
    (tempo + first beat offset), so the operation stays pure: no DSP happens on
    the substrate, only beat-grid arithmetic and an offset map.
    """

    model_config = ConfigDict(extra="forbid")

    music_asset_id: str = Field(min_length=1)
    tempo_bpm: float = Field(gt=0.0, le=400.0)
    first_beat_us: int = Field(default=0, ge=0)
    target_start_us: int = Field(default=0, ge=0)
    anchor: MusicAnchor = "first_beat"
    max_shift_us: int = Field(default=500_000, ge=0, le=10_000_000)
