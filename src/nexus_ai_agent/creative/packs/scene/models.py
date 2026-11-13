"""Typed input models for the AETHEL ``nexus.scene.imaging`` pack (Wave 3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCENE_PACKAGE_ID = "nexus.scene.imaging"
DOMAIN = "scene"

OPERATION_INSERT_OBJECT_WITH_SHADOW = "scene.insert_object_with_shadow"
OPERATION_REMOVE_OBJECT_INPAINT = "scene.remove_object_inpaint"
OPERATION_OUTPAINT_CANVAS_EXPAND = "scene.outpaint_canvas_expand"
OPERATION_SYNTHESIZE_DOF = "scene.synthesize_depth_of_field"
OPERATION_ADD_VOLUMETRIC_FOG = "scene.add_volumetric_fog_haze"
OPERATION_LENS_DISTORTION_CORRECT = "scene.lens_distortion_correct"
OPERATION_CHROMATIC_ABERRATION_CONTROL = "scene.chromatic_aberration_control"
OPERATION_PERSPECTIVE_HORIZON_LEVEL = "scene.perspective_horizon_level"
OPERATION_SKY_REPLACEMENT_RELIT = "scene.sky_replacement_relit"
OPERATION_REFLECTION_WATER_GLASS = "scene.reflection_water_glass_synth"

#: Wave 9 addition (not one of the ten catalogue operations).
OPERATION_HARMONIZE_RADIATIVE_TRANSFER = "scene.harmonize_radiative_transfer"

#: Wave 10 addition (not one of the ten catalogue operations).
OPERATION_RETIME_OPTICAL_FLOW = "scene.retime_optical_flow"

#: Wave 11 addition (not one of the ten catalogue operations).
OPERATION_TONEMAP_HDR = "scene.tonemap_hdr"

#: Wave 12 addition (not one of the ten catalogue operations).
OPERATION_FILM_EMULATION = "scene.film_emulation"

#: The ten catalogue scene operations this pack implements.
SCENE_OPERATIONS: tuple[str, ...] = (
    OPERATION_INSERT_OBJECT_WITH_SHADOW,
    OPERATION_REMOVE_OBJECT_INPAINT,
    OPERATION_OUTPAINT_CANVAS_EXPAND,
    OPERATION_SYNTHESIZE_DOF,
    OPERATION_ADD_VOLUMETRIC_FOG,
    OPERATION_LENS_DISTORTION_CORRECT,
    OPERATION_CHROMATIC_ABERRATION_CONTROL,
    OPERATION_PERSPECTIVE_HORIZON_LEVEL,
    OPERATION_SKY_REPLACEMENT_RELIT,
    OPERATION_REFLECTION_WATER_GLASS,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InsertObjectInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    object_id: str = Field(min_length=1, max_length=128)
    mask_id: str = Field(min_length=1, max_length=128)
    offset_x: int = Field(default=0, ge=-4096, le=4096)
    offset_y: int = Field(default=0, ge=-4096, le=4096)


class RemoveObjectInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    mask_id: str = Field(min_length=1, max_length=128)
    iters: int = Field(default=300, ge=1, le=5000)


class OutpaintInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    pad: int = Field(default=8, ge=1, le=512)


class DepthOfFieldInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    depth_id: str = Field(min_length=1, max_length=128)
    focus: float = Field(default=0.5, ge=0.0, le=1.0)
    f_number: float = Field(default=2.8, gt=0.0, le=64.0)
    f_mm: float = Field(default=50.0, gt=0.0, le=1000.0)


class FogHazeInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    depth_id: str = Field(min_length=1, max_length=128)
    beta: float = Field(default=0.05, ge=0.0, le=10.0)
    g: float = Field(default=0.6, ge=-1.0, le=1.0)


class LensDistortionInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    k1: float = Field(default=0.0, ge=-2.0, le=2.0)
    k2: float = Field(default=0.0, ge=-2.0, le=2.0)
    k3: float = Field(default=0.0, ge=-2.0, le=2.0)


class ChromaticAberrationInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    scale_r: float = Field(default=1.0, ge=0.5, le=1.5)
    scale_g: float = Field(default=1.0, ge=0.5, le=1.5)
    scale_b: float = Field(default=1.0, ge=0.5, le=1.5)


class HorizonLevelInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)


class SkyRelitInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    mask_id: str = Field(min_length=1, max_length=128)
    sky_id: str = Field(min_length=1, max_length=128)
    sky_temp_k: float = Field(default=6500.0, ge=1000.0, le=20000.0)


class ReflectionInput(_Strict):
    image_id: str = Field(min_length=1, max_length=128)
    axis: int = Field(default=0, ge=0, le=1)
    fresnel: float = Field(default=0.02, ge=0.0, le=1.0)
    waves: float = Field(default=0.0, ge=0.0, le=8.0)


class RadiativeTransferHarmonizeInput(_Strict):
    """Relight a composited foreground to a target SH environment (Wave 9)."""

    image_id: str = Field(min_length=1, max_length=128)
    normal_id: str = Field(min_length=1, max_length=128)
    mask_id: str = Field(min_length=1, max_length=128)
    target_ambient: float = Field(default=0.3, ge=0.0, le=8.0)
    roughness: float = Field(default=0.35, gt=0.0, le=1.0)
    specular_strength: float = Field(default=0.25, ge=0.0, le=1.0)


class OpticalFlowRetimeInput(_Strict):
    """Flow-based temporal interpolation of one frame between two keyframes (Wave 10)."""

    image_a_id: str = Field(min_length=1, max_length=128)
    image_b_id: str = Field(min_length=1, max_length=128)
    t: float = Field(default=0.5, ge=0.0, le=1.0)


class HdrTonemapInput(_Strict):
    """HDR tone mapping and optional white-balance adaptation (Wave 11)."""

    image_id: str = Field(min_length=1, max_length=128)
    curve: str = Field(default="aces", pattern="^(reinhard|aces|hejl|uncharted)$")
    exposure: float = Field(default=1.0, gt=0.0, le=64.0)
    white: float = Field(default=4.0, gt=0.0, le=64.0)
    auto_exposure: bool = Field(default=False)
    key: float = Field(default=0.18, gt=0.0, le=1.0)
    adapt_temperature_k: float | None = Field(default=None, ge=1667.0, le=25000.0)


class FilmEmulationInput(_Strict):
    """Photochemical film emulation: gate weave, halation and grain (Wave 12)."""

    image_id: str = Field(min_length=1, max_length=128)
    frame_index: int = Field(default=0, ge=0, le=1_000_000)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    grain_sigma: float = Field(default=0.03, ge=0.0, le=1.0)
    halation_strength: float = Field(default=0.4, ge=0.0, le=4.0)
    weave_amplitude: float = Field(default=1.5, ge=0.0, le=32.0)


__all__ = [
    "SCENE_PACKAGE_ID",
    "DOMAIN",
    "SCENE_OPERATIONS",
    "InsertObjectInput",
    "RemoveObjectInput",
    "OutpaintInput",
    "DepthOfFieldInput",
    "FogHazeInput",
    "LensDistortionInput",
    "ChromaticAberrationInput",
    "HorizonLevelInput",
    "SkyRelitInput",
    "ReflectionInput",
    "RadiativeTransferHarmonizeInput",
    "OpticalFlowRetimeInput",
    "HdrTonemapInput",
    "FilmEmulationInput",
]
