"""Typed models for the ``nexus.edit.timeline`` pack (Wave 3 substrate).

This module defines the typed contracts for non-destructive timeline editing,
trimming, ripple deletion, speed ramping, time reversal, freeze frames, and B-roll.
In accordance with Nagar architecture, this layer is purely declarative:
stdlib + pydantic only, no I/O, no heavy video or FFmpeg imports.

Surface:
* :class:`TrimInput` — non-destructive in/out trimming
* :class:`RippleDeleteInput` — span deletion with ripple shift
* :class:`InsertGapInput` — gap insertion on a track
* :class:`SpeedRampInput` — speed factor multiplier and pitch lock
* :class:`ReverseSegmentInput` — time-reversed clip derivation
* :class:`FreezeFrameInput` — hold-frame derivation
* :class:`AttachBRollInput` — secondary track visual overlay
* :class:`RetimeToMusicInput` — beat-snapped timeline cut alignment
* :class:`MulticamAnchor` / :class:`SyncMulticamInput` — multicam offset map (Wave 5)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EDIT_PACKAGE_ID = "nexus.edit.timeline"
DOMAIN = "timeline"

OPERATION_TRIM = "timeline.trim"
OPERATION_RIPPLE_DELETE = "timeline.ripple_delete"
OPERATION_INSERT_GAP = "timeline.insert_gap"
OPERATION_SPEED_RAMP = "timeline.speed_ramp"
OPERATION_REVERSE_SEGMENT = "timeline.reverse_segment"
OPERATION_FREEZE_FRAME = "timeline.freeze_frame"
OPERATION_ATTACH_B_ROLL = "timeline.attach_b_roll"
OPERATION_RETIME_TO_MUSIC = "timeline.retime_to_music"
OPERATION_SYNC_MULTICAM = "timeline.sync_multicam"


class TrimInput(BaseModel):
    """Input payload for ``timeline.trim`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    in_point_us: int = Field(ge=0)
    out_point_us: int = Field(ge=0)
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_points(self) -> TrimInput:
        if self.out_point_us <= self.in_point_us:
            raise ValueError(
                f"out_point_us ({self.out_point_us}) must be > in_point_us ({self.in_point_us})"
            )
        return self


class RippleDeleteInput(BaseModel):
    """Input payload for ``timeline.ripple_delete`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(default="video_main", min_length=1)
    start_us: int = Field(ge=0)
    duration_us: int = Field(gt=0)


class InsertGapInput(BaseModel):
    """Input payload for ``timeline.insert_gap`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(default="video_main", min_length=1)
    at_us: int = Field(ge=0)
    duration_us: int = Field(gt=0)


class SpeedRampInput(BaseModel):
    """Input payload for ``timeline.speed_ramp`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    speed_factor: float = Field(default=1.0, ge=0.1, le=16.0)
    maintain_pitch: bool = True
    output_asset_id: str | None = None


class ReverseSegmentInput(BaseModel):
    """Input payload for ``timeline.reverse_segment`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    output_asset_id: str | None = None


class FreezeFrameInput(BaseModel):
    """Input payload for ``timeline.freeze_frame`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    freeze_at_us: int = Field(ge=0)
    duration_us: int = Field(default=3_000_000, ge=10_000)
    output_asset_id: str | None = None


class AttachBRollInput(BaseModel):
    """Input payload for ``timeline.attach_b_roll`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    main_clip_id: str = Field(min_length=1)
    b_roll_asset_id: str = Field(min_length=1)
    start_offset_us: int = Field(default=0, ge=0)
    duration_us: int | None = None
    track_id: str = Field(default="video_b_roll", min_length=1)
    output_asset_id: str | None = None


class RetimeToMusicInput(BaseModel):
    """Input payload for ``timeline.retime_to_music`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_ids: list[str] = Field(min_length=1)
    audio_asset_id: str = Field(min_length=1)
    tempo_bpm: float = Field(default=120.0, gt=0.0)
    beats_per_cut: int = Field(default=4, ge=1)


MulticamAnchorPolicy = Literal["first_clip", "longest_clip", "explicit"]


class MulticamAnchor(BaseModel):
    """One measured anchor marker for a multicam clip.

    The measurement itself belongs to the analysis lane (audio onset / timecode
    detection); the substrate only consumes the number, which keeps this
    operation pure and deterministic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    anchor_us: int = Field(ge=0)


class SyncMulticamInput(BaseModel):
    """Input payload for ``timeline.sync_multicam`` (Level A — read-only analysis)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_ids: list[str] = Field(min_length=2, max_length=16)
    anchors: list[MulticamAnchor] = Field(min_length=2, max_length=16)
    anchor_policy: MulticamAnchorPolicy = "first_clip"
    reference_clip_id: str | None = None
    max_offset_us: int = Field(default=5_000_000, ge=1, le=60_000_000)

    @model_validator(mode="after")
    def _validate_multicam_request(self) -> SyncMulticamInput:
        if len(set(self.clip_asset_ids)) != len(self.clip_asset_ids):
            raise ValueError("clip_asset_ids must be unique")
        anchored = [anchor.clip_asset_id for anchor in self.anchors]
        if len(set(anchored)) != len(anchored):
            raise ValueError("anchors must carry one marker per clip (no duplicates)")
        missing = [clip for clip in self.clip_asset_ids if clip not in set(anchored)]
        if missing:
            raise ValueError(f"missing anchors for clips: {sorted(missing)}")
        extra = [clip for clip in anchored if clip not in set(self.clip_asset_ids)]
        if extra:
            raise ValueError(f"anchors reference clips that are not in the set: {sorted(extra)}")
        if self.anchor_policy == "explicit":
            if self.reference_clip_id is None:
                raise ValueError("anchor_policy 'explicit' requires reference_clip_id")
            if self.reference_clip_id not in set(self.clip_asset_ids):
                raise ValueError(
                    f"reference_clip_id {self.reference_clip_id!r} is not part of clip_asset_ids"
                )
        elif self.reference_clip_id is not None:
            raise ValueError("reference_clip_id is only meaningful with anchor_policy 'explicit'")
        return self
