"""Typed models for the ``nexus.color.delivery`` pack (Wave 7 substrate).

This module defines the typed contracts for color grading, LUT profile application,
lightweight proxy generation, 4K master rendering, and OpenTimelineIO (OTIO v1) export.
In accordance with Nagar architecture, this layer is purely declarative:
stdlib + pydantic only, no I/O, no heavy FFmpeg or PyTorch imports.

Surface:
* :class:`ApplyLutInput` — apply 3D LUT (Cube/HALD) color grade
* :class:`AdjustExposureInput` — exposure EV and color temperature tuning
* :class:`AutoBalanceInput` — automated white balance and histogram normalization
* :class:`MakeProxyInput` — fast-scrub 480p proxy rendition creation
* :class:`ExportOtioInput` — export timeline to standard OpenTimelineIO JSON
* :class:`RenderMaster4KInput` — Level C master render contract with color space & LUFS
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DELIVERY_PACKAGE_ID = "nexus.color.delivery"
DOMAIN_COLOR = "color"
DOMAIN_DELIVERY = "delivery"

OPERATION_APPLY_LUT = "color.apply_lut"
OPERATION_ADJUST_EXPOSURE = "color.adjust_exposure"
OPERATION_AUTO_BALANCE = "color.auto_balance"
OPERATION_MATCH_SHOT = "color.match_shot"
OPERATION_MAKE_PROXY = "delivery.make_proxy_480p"
OPERATION_EXPORT_OTIO = "delivery.export_otio"
OPERATION_RENDER_MASTER_4K = "delivery.render_master_4k"


class MatchShotInput(BaseModel):
    """Input payload for ``color.match_shot`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_clip_id: str = Field(min_length=1)
    reference_clip_id: str = Field(min_length=1)
    match_luminance: bool = True
    match_chrominance: bool = True
    output_asset_id: str | None = None


class ApplyLutInput(BaseModel):
    """Input payload for ``color.apply_lut`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    lut_name: str = Field(min_length=1, description="Identifier of the 3D LUT profile.")
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)
    color_space: Literal["bt709", "bt2020", "srgb", "acescc"] = "bt709"
    output_asset_id: str | None = None


class AdjustExposureInput(BaseModel):
    """Input payload for ``color.adjust_exposure`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    exposure_ev: float = Field(default=0.0, ge=-4.0, le=4.0, description="Exposure shift in EV.")
    contrast: float = Field(default=1.0, ge=0.2, le=3.0, description="Contrast curve slope.")
    temperature_k: int = Field(default=6500, ge=2000, le=12000, description="Color temperature in Kelvin.")
    tint: float = Field(default=0.0, ge=-50.0, le=50.0, description="Green-magenta tint shift.")
    output_asset_id: str | None = None


class AutoBalanceInput(BaseModel):
    """Input payload for ``color.auto_balance`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    preserve_skin_tones: bool = True
    output_asset_id: str | None = None


class MakeProxyInput(BaseModel):
    """Input payload for ``delivery.make_proxy_480p`` (Level A)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_asset_id: str = Field(min_length=1)
    resolution: Literal["854x480", "640x360"] = "854x480"
    crf: int = Field(default=28, ge=18, le=36)
    output_asset_id: str | None = None


class RationalTime(BaseModel):
    """OpenTimelineIO rational time representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    OTIO_SCHEMA: str = "RationalTime.1"
    value: int = Field(ge=0)
    rate: float = Field(gt=0.0)


class TimeRange(BaseModel):
    """OpenTimelineIO time range representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    OTIO_SCHEMA: str = "TimeRange.1"
    start_time: RationalTime
    duration: RationalTime


class OtioClip(BaseModel):
    """OpenTimelineIO clip element representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    OTIO_SCHEMA: str = "Clip.1"
    name: str
    source_range: TimeRange
    media_url: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class OtioTrack(BaseModel):
    """OpenTimelineIO track element representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    OTIO_SCHEMA: str = "Track.1"
    name: str
    kind: Literal["Video", "Audio"]
    children: list[OtioClip] = Field(default_factory=list)


class ExportOtioInput(BaseModel):
    """Input payload for ``delivery.export_otio`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeline_id: str = Field(default="main", min_length=1)
    frame_rate: float = Field(default=24.0, gt=0.0)
    include_markers: bool = True
    output_asset_id: str | None = None


class RenderMaster4KInput(BaseModel):
    """Input payload for ``delivery.render_master_4k`` (Level C)."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=3840, ge=640)
    height: int = Field(default=2160, ge=360)
    codec: Literal["h264", "hevc", "prores"] = "h264"
    container: Literal["mp4", "mov", "mkv"] = "mp4"
    color_space: Literal["bt709", "bt2020"] = "bt709"
    target_lufs: float = Field(default=-14.0, ge=-70.0, le=0.0)
    confirmed: bool = Field(default=False)
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_aspect_dimensions(self) -> RenderMaster4KInput:
        if self.width % 2 != 0 or self.height % 2 != 0:
            raise ValueError("render width and height must be even integers for video encoding")
        return self
