"""Typed models for the ``nexus.motion.graphics`` pack (Wave 6 substrate).

This module defines the typed contracts for video transitions, keyframe animation,
glow filters, motion blur, and kinetic titles. In accordance with Nagar architecture,
this layer is purely declarative: stdlib + pydantic only, no I/O, no heavy WebGPU or C-extensions.

Surface:
* :class:`AddTransitionInput` — video transitions between adjacent clips
* :class:`TransformKeyframe` — 2D transform point (scale, position, rotation, opacity)
* :class:`KeyframeTransformInput` — keyframe animation track
* :class:`AddGlowInput` — thresholded blur bloom and glow layer
* :class:`AddMotionBlurInput` — shutter-angle temporal motion blur
* :class:`AddTitleInput` — kinetic motion typography and lower thirds
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MOTION_PACKAGE_ID = "nexus.motion.graphics"
DOMAIN = "motion"

OPERATION_ADD_TRANSITION = "motion.add_transition"
OPERATION_KEYFRAME_TRANSFORM = "motion.keyframe_transform"
OPERATION_ADD_GLOW = "motion.add_glow"
OPERATION_ADD_MOTION_BLUR = "motion.add_motion_blur"
OPERATION_ADD_TITLE = "motion.add_title"
OPERATION_STABILIZE = "motion.stabilize"
OPERATION_ADD_PARALLAX = "motion.add_parallax"

TransitionKind = Literal[
    "crossfade",
    "dip_to_black",
    "wipe_left",
    "wipe_right",
    "dissolve",
    "zoom_in",
    "slide_up",
]

EasingKind = Literal[
    "linear",
    "ease_in",
    "ease_out",
    "ease_in_out",
    "smoothstep",
]


class AddTransitionInput(BaseModel):
    """Input payload for ``motion.add_transition`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left_clip_id: str = Field(min_length=1)
    right_clip_id: str = Field(min_length=1)
    kind: TransitionKind = "crossfade"
    duration_us: int = Field(default=500_000, ge=10_000, le=5_000_000)
    easing: EasingKind = "smoothstep"
    output_asset_id: str | None = None


class TransformKeyframe(BaseModel):
    """Individual transform point on a keyframe curve."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_offset_us: int = Field(ge=0, description="Time offset from clip start in microseconds.")
    scale: float = Field(default=1.0, ge=0.0, le=10.0)
    position_x: float = Field(
        default=0.0, ge=-1.0, le=1.0, description="Normalized X offset [-1.0, 1.0]."
    )
    position_y: float = Field(
        default=0.0, ge=-1.0, le=1.0, description="Normalized Y offset [-1.0, 1.0]."
    )
    rotation_deg: float = Field(default=0.0, ge=-360.0, le=360.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


class KeyframeTransformInput(BaseModel):
    """Input payload for ``motion.keyframe_transform`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    keyframes: list[TransformKeyframe] = Field(min_length=1)
    easing: EasingKind = "ease_in_out"
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_keyframe_order(self) -> KeyframeTransformInput:
        offsets = [k.time_offset_us for k in self.keyframes]
        if offsets != sorted(offsets):
            raise ValueError("keyframes must be ordered in monotonically increasing time_offset_us")
        return self


class AddGlowInput(BaseModel):
    """Input payload for ``motion.add_glow`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    radius_px: float = Field(default=15.0, ge=1.0, le=100.0)
    intensity: float = Field(default=0.8, ge=0.0, le=3.0)
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    output_asset_id: str | None = None


class AddMotionBlurInput(BaseModel):
    """Input payload for ``motion.add_motion_blur`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    shutter_angle_deg: float = Field(default=180.0, ge=0.0, le=360.0)
    samples: int = Field(default=8, ge=2, le=32)
    output_asset_id: str | None = None


class AddTitleInput(BaseModel):
    """Input payload for ``motion.add_title`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=500)
    animation_style: Literal["fade_up", "typewriter", "kinetic_pop", "glitch", "slide_in"] = (
        "fade_up"
    )
    font_name: str = "Vazirmatn"
    font_size: int = Field(default=64, ge=12, le=256)
    color: str = "#FFFFFF"
    duration_us: int = Field(default=3_000_000, ge=100_000)
    position: Literal["center", "lower_third", "top_header"] = "lower_third"
    output_asset_id: str | None = None


class StabilizeInput(BaseModel):
    """Input payload for ``motion.stabilize`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    strength: float = Field(default=0.7, ge=0.0, le=1.0)
    crop_mode: Literal["none", "static", "dynamic"] = "dynamic"
    output_asset_id: str | None = None


class AddParallaxInput(BaseModel):
    """Input payload for ``motion.add_parallax`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    depth_layers: int = Field(default=3, ge=2, le=8)
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    direction: Literal["horizontal", "vertical"] = "horizontal"
    output_asset_id: str | None = None
