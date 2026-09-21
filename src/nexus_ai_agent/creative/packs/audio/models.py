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

from pydantic import BaseModel, ConfigDict, Field, model_validator

AUDIO_PACKAGE_ID = "nexus.audio.studio"
DOMAIN = "audio"

OPERATION_DETECT_BEATS = "audio.detect_beats"
OPERATION_NORMALIZE_LOUDNESS = "audio.normalize_loudness"
OPERATION_DUCK_MUSIC = "audio.duck_music"
OPERATION_BEAT_SYNC_CUT = "audio.beat_sync_cut"


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
