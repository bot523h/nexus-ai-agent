"""Typed models for the ``nexus.vision.portrait`` pack (TDD §1.3 contract).

Ten portrait operations, purely declarative (stdlib + pydantic + studio + the
shared :mod:`nexus_ai_agent.creative.packs.vision_common` primitives): no I/O,
no ONNX import, no heavy ML dependency — the capability ladder (WebGPU → WASM →
native) is declared in ``pack.manifest.json`` and executed by future adapters.

Surface (TDD rows, in catalogue order):

* :class:`DetectLandmarksInput`  — ``ClipRef + sample_policy → FaceTrackSet`` (A)
* :class:`SmoothSkinInput`      — ``FaceTrackSet/MaskRef + strength → EffectLayerRef`` (B)
* :class:`RetouchBlemishInput`  — ``FaceMask + blemish_policy → EffectLayerRef`` (B)
* :class:`RelightFaceInput`     — ``FaceTrackSet + LightModel → EffectLayerRef`` (B)
* :class:`WhitenTeethInput`     — ``TeethMask + intensity → EffectLayerRef`` (B)
* :class:`CorrectGazeInput`     — ``FaceTrackSet + gaze_target → EffectLayerRef`` (C)
* :class:`EnhanceEyesInput`     — ``FaceTrackSet + clarity/red_eye → EffectLayerRef`` (B)
* :class:`MaskHairInput`        — ``FaceTrackSet + edge_quality → MaskRef`` (A)
* :class:`BackgroundBlurInput`  — ``SubjectMask + blur_profile → EffectLayerRef`` (B)
* :class:`StabilizeFaceInput`   — ``FaceTrackSet + stabilization_policy → TransformCurve`` (B)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.packs.vision_common import SamplePolicy

PORTRAIT_PACKAGE_ID = "nexus.vision.portrait"
DOMAIN = "portrait"

OPERATION_DETECT_LANDMARKS = "portrait.detect_landmarks"
OPERATION_SMOOTH_SKIN = "portrait.smooth_skin"
OPERATION_RETOUCH_BLEMISH = "portrait.retouch_blemish"
OPERATION_RELIGHT_FACE = "portrait.relight_face"
OPERATION_WHITEN_TEETH = "portrait.whiten_teeth"
OPERATION_CORRECT_GAZE = "portrait.correct_gaze"
OPERATION_ENHANCE_EYES = "portrait.enhance_eyes"
OPERATION_MASK_HAIR = "portrait.mask_hair"
OPERATION_BACKGROUND_BLUR = "portrait.background_blur"
OPERATION_STABILIZE_FACE = "portrait.stabilize_face"

#: Blur profiles for ``portrait.background_blur`` (radius in pixels, pure data).
BLUR_PROFILES: dict[str, dict[str, float]] = {
    "subtle": {"radius_px": 6.0, "mix": 0.6},
    "standard": {"radius_px": 14.0, "mix": 0.85},
    "bokeh": {"radius_px": 28.0, "mix": 1.0},
}

#: Edge-quality → feather widths for hair/teeth masks (pure data).
EDGE_FEATHER_PX: dict[str, float] = {
    "soft": 8.0,
    "balanced": 4.0,
    "crisp": 1.5,
}

#: Blemish policies → inpaint radius and blend (pure data).
BLEMISH_POLICIES: dict[str, dict[str, float]] = {
    "conservative": {"inpaint_radius_px": 3.0, "blend": 0.5},
    "balanced": {"inpaint_radius_px": 5.0, "blend": 0.75},
    "aggressive": {"inpaint_radius_px": 8.0, "blend": 1.0},
}

LightDirection = Literal["camera_left", "camera_right", "top", "soft_front"]
GazeTarget = Literal["camera", "screen_center", "left", "right"]
StabilizationPolicy = Literal["lock_position", "lock_position_scale", "smooth_only"]
EdgeQuality = Literal["soft", "balanced", "crisp"]
BlurProfile = Literal["subtle", "standard", "bokeh"]
BlemishPolicy = Literal["conservative", "balanced", "aggressive"]
TemporalStability = Literal["low", "medium", "high"]


class LightModel(BaseModel):
    """TDD ``LightModel``: typed relight parameters (no shader strings)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: LightDirection = "soft_front"
    intensity: float = Field(default=1.0, ge=0.0, le=2.0)
    warmth: float = Field(default=0.0, ge=-1.0, le=1.0)


class DetectLandmarksInput(BaseModel):
    """Input payload for ``portrait.detect_landmarks`` (Level A)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    sample_policy: SamplePolicy = "standard"
    landmark_count: int = Field(default=68, ge=1, le=68)
    max_faces: int = Field(default=1, ge=1, le=4)
    subject_id: str = Field(default="subject_01", min_length=1)


class SmoothSkinInput(BaseModel):
    """Input payload for ``portrait.smooth_skin`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    temporal_stability: TemporalStability = "high"
    face_track_id: str | None = Field(default=None, min_length=1)
    mask_asset_id: str | None = Field(default=None, min_length=1)
    output_asset_id: str | None = None

    @model_validator(mode="after")
    def _require_a_subject_reference(self) -> SmoothSkinInput:
        if self.face_track_id is None and self.mask_asset_id is None:
            raise ValueError("smooth_skin needs a face_track_id or a mask_asset_id (TDD input)")
        return self


class RetouchBlemishInput(BaseModel):
    """Input payload for ``portrait.retouch_blemish`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(min_length=1, description="TDD FaceMask (image asset).")
    blemish_policy: BlemishPolicy = "balanced"
    output_asset_id: str | None = None


class RelightFaceInput(BaseModel):
    """Input payload for ``portrait.relight_face`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    face_track_id: str = Field(min_length=1)
    light: LightModel = Field(default_factory=LightModel)
    output_asset_id: str | None = None


class WhitenTeethInput(BaseModel):
    """Input payload for ``portrait.whiten_teeth`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(min_length=1, description="TDD TeethMask (image asset).")
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    edge_quality: EdgeQuality = "balanced"
    output_asset_id: str | None = None


class CorrectGazeInput(BaseModel):
    """Input payload for ``portrait.correct_gaze`` (Level C — identity-sensitive)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    face_track_id: str = Field(min_length=1)
    gaze_target: GazeTarget = "camera"
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    confirmed: bool = Field(
        default=False, description="Explicit user confirmation required for Level C execution."
    )
    output_asset_id: str | None = None


class EnhanceEyesInput(BaseModel):
    """Input payload for ``portrait.enhance_eyes`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    face_track_id: str = Field(min_length=1)
    clarity: float = Field(default=0.4, ge=0.0, le=1.0)
    red_eye: bool = False
    output_asset_id: str | None = None


class MaskHairInput(BaseModel):
    """Input payload for ``portrait.mask_hair`` (Level A → MaskRef)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    face_track_id: str = Field(min_length=1)
    edge_quality: EdgeQuality = "balanced"


class BackgroundBlurInput(BaseModel):
    """Input payload for ``portrait.background_blur`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(min_length=1, description="TDD SubjectMask (image asset).")
    blur_profile: BlurProfile = "standard"
    output_asset_id: str | None = None


class StabilizeFaceInput(BaseModel):
    """Input payload for ``portrait.stabilize_face`` (Level B → TransformCurve)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    face_track_id: str = Field(min_length=1)
    stabilization_policy: StabilizationPolicy = "smooth_only"
    strength: float = Field(default=0.6, ge=0.0, le=1.0)
    output_asset_id: str | None = None


__all__ = [
    "BLEMISH_POLICIES",
    "BLUR_PROFILES",
    "BackgroundBlurInput",
    "CorrectGazeInput",
    "DOMAIN",
    "DetectLandmarksInput",
    "EDGE_FEATHER_PX",
    "EnhanceEyesInput",
    "LightModel",
    "MaskHairInput",
    "OPERATION_BACKGROUND_BLUR",
    "OPERATION_CORRECT_GAZE",
    "OPERATION_DETECT_LANDMARKS",
    "OPERATION_ENHANCE_EYES",
    "OPERATION_MASK_HAIR",
    "OPERATION_RELIGHT_FACE",
    "OPERATION_RETOUCH_BLEMISH",
    "OPERATION_SMOOTH_SKIN",
    "OPERATION_STABILIZE_FACE",
    "OPERATION_WHITEN_TEETH",
    "PORTRAIT_PACKAGE_ID",
    "RelightFaceInput",
    "RetouchBlemishInput",
    "SmoothSkinInput",
    "StabilizeFaceInput",
    "WhitenTeethInput",
]
