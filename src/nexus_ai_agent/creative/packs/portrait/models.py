"""Typed input models for the AETHEL ``nexus.portrait.refine`` pack (Wave 4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

PORTRAIT_PACKAGE_ID = "nexus.portrait.refine"
DOMAIN = "portrait"

OPERATION_GAZE_REDIRECTION = "portrait.gaze_redirection"
OPERATION_EYEBLINK_SYNTHESIS = "portrait.eyeblink_synthesis"
OPERATION_FACIAL_RELIGHTING = "portrait.facial_relighting_gradient"
OPERATION_SKINTONE_HARMONIZATION = "portrait.skintone_harmonization"
OPERATION_MICRO_EXPRESSION_MORPH = "portrait.micro_expression_morph"
OPERATION_TEETH_WHITENING = "portrait.teeth_whitening"
OPERATION_FACIAL_HAIR_CONTINUITY = "portrait.facial_hair_continuity"
OPERATION_HEAD_REPOSE_3D = "portrait.head_repose_3d"
OPERATION_DOF_BOKEH = "portrait.depth_of_field_bokeh"
OPERATION_AGING_DEAGING = "portrait.aging_deaging_geometry"

#: The ten catalogue portrait operations this pack implements.
PORTRAIT_OPERATIONS: tuple[str, ...] = (
    OPERATION_GAZE_REDIRECTION,
    OPERATION_EYEBLINK_SYNTHESIS,
    OPERATION_FACIAL_RELIGHTING,
    OPERATION_SKINTONE_HARMONIZATION,
    OPERATION_MICRO_EXPRESSION_MORPH,
    OPERATION_TEETH_WHITENING,
    OPERATION_FACIAL_HAIR_CONTINUITY,
    OPERATION_HEAD_REPOSE_3D,
    OPERATION_DOF_BOKEH,
    OPERATION_AGING_DEAGING,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GazeRedirectionInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    eye_mask_id: str = Field(min_length=1, max_length=128)
    target_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    target_y: float = Field(default=0.0, ge=-1.0, le=1.0)
    strength: float = Field(default=1.0, ge=0.0, le=1.0)


class EyeblinkInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    eye_mask_id: str = Field(min_length=1, max_length=128)
    phase: float = Field(default=0.5, ge=0.0, le=1.0)


class FacialRelightingInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    light_azimuth_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    light_elevation_deg: float = Field(default=30.0, ge=-90.0, le=90.0)
    ambient: float = Field(default=0.3, ge=0.0, le=1.0)


class SkintoneInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    skin_mask_id: str = Field(min_length=1, max_length=128)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)


class MicroExpressionInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    control_points: tuple[tuple[float, float, float, float], ...] = Field(
        min_length=1, max_length=64
    )


class TeethWhiteningInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    teeth_mask_id: str = Field(min_length=1, max_length=128)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)


class FacialHairInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    beard_mask_id: str = Field(min_length=1, max_length=128)
    density: float = Field(default=0.5, ge=0.0, le=1.0)


class HeadReposeInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    yaw_deg: float = Field(default=0.0, ge=-15.0, le=15.0)
    pitch_deg: float = Field(default=0.0, ge=-15.0, le=15.0)


class DepthOfFieldBokehInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    depth_id: str = Field(min_length=1, max_length=128)
    focus: float = Field(default=0.5, ge=0.0, le=1.0)
    blades: int = Field(default=6, ge=3, le=16)


class AgingInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    years: float = Field(default=0.0, ge=-60.0, le=60.0)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)


__all__ = [
    "PORTRAIT_PACKAGE_ID",
    "DOMAIN",
    "PORTRAIT_OPERATIONS",
    "GazeRedirectionInput",
    "EyeblinkInput",
    "FacialRelightingInput",
    "SkintoneInput",
    "MicroExpressionInput",
    "TeethWhiteningInput",
    "FacialHairInput",
    "HeadReposeInput",
    "DepthOfFieldBokehInput",
    "AgingInput",
]
