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
* :class:`ApplyMaskInput` — mask/feather compositing layer (Wave 5)
* :class:`MeshControlPoint` / :class:`WarpInput` — mesh-warp displacement curves (Wave 5)
* :class:`EmitterSpec` / :class:`AddParticlesInput` — deterministic particle emitter (Wave 5)
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
OPERATION_APPLY_MASK = "motion.apply_mask"
OPERATION_WARP = "motion.warp"
OPERATION_ADD_PARTICLES = "motion.add_particles"

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


class ApplyMaskInput(BaseModel):
    """Input payload for ``motion.apply_mask`` (Level B).

    A mask is a content-addressed **image** asset (the substrate deliberately has
    no separate mask registry): only its ``content_sha256`` enters the derived
    digest, so the plan is reproducible from the manifest alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(
        min_length=1, description="Image asset used as the mask source (white = keep)."
    )
    feather_px: float = Field(default=8.0, ge=0.0, le=200.0)
    invert: bool = False
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_mask_source(self) -> ApplyMaskInput:
        if self.mask_asset_id == self.clip_asset_id:
            raise ValueError("mask_asset_id must differ from clip_asset_id")
        return self


class MeshControlPoint(BaseModel):
    """One mesh vertex displacement, normalized to the clip box."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int = Field(ge=0)
    col: int = Field(ge=0)
    dx: float = Field(default=0.0, ge=-1.0, le=1.0)
    dy: float = Field(default=0.0, ge=-1.0, le=1.0)
    time_offset_us: int = Field(default=0, ge=0)


class WarpInput(BaseModel):
    """Input payload for ``motion.warp`` (Level B): a time-varying mesh curve."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    mesh_rows: int = Field(default=3, ge=2, le=16)
    mesh_cols: int = Field(default=3, ge=2, le=16)
    control_points: list[MeshControlPoint] = Field(min_length=1, max_length=256)
    easing: EasingKind = "smoothstep"
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_mesh_curve(self) -> WarpInput:
        seen: set[tuple[int, int, int]] = set()
        for point in self.control_points:
            if point.row >= self.mesh_rows or point.col >= self.mesh_cols:
                raise ValueError(
                    f"control point ({point.row}, {point.col}) is outside the "
                    f"{self.mesh_rows}x{self.mesh_cols} mesh"
                )
            key = (point.row, point.col, point.time_offset_us)
            if key in seen:
                raise ValueError(f"duplicate control point for (row, col, time): {key}")
            seen.add(key)
        ordered = [(p.time_offset_us, p.row, p.col) for p in self.control_points]
        if ordered != sorted(ordered):
            raise ValueError("control_points must be ordered by time_offset_us, then row, col")
        return self


class EmitterSpec(BaseModel):
    """Declarative particle emitter.

    There is no RNG in the substrate: ``seed`` is part of the contract and every
    derived sample comes from ``sha256(seed:index)``, so two runs of the same
    command produce byte-identical plans on every platform.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    particles: int = Field(default=120, ge=1, le=5000)
    lifetime_us: int = Field(default=1_500_000, ge=100_000, le=30_000_000)
    velocity_px_s: float = Field(default=180.0, ge=0.0, le=5000.0)
    gravity: float = Field(default=0.0, ge=-50.0, le=50.0)
    spread_deg: float = Field(default=45.0, ge=0.0, le=360.0)
    seed: int = Field(default=0, ge=0, le=4_294_967_295)


class AddParticlesInput(BaseModel):
    """Input payload for ``motion.add_particles`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    emitter: EmitterSpec = Field(default_factory=EmitterSpec)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> AddParticlesInput:
        if self.end_us <= self.start_us:
            raise ValueError(f"end_us ({self.end_us}) must be > start_us ({self.start_us})")
        return self
