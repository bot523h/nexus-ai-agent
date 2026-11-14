"""Typed models for the ``nexus.vision.scene`` pack (TDD §1.4 contract).

Ten scene-understanding operations, purely declarative (stdlib + pydantic +
studio + the shared :mod:`nexus_ai_agent.creative.packs.vision_common`
primitives).  Semantic targets, never synthetic pixel coordinates as *input*:
objects and subjects are referenced through tracks, masks and semantic seeds.

Surface (TDD rows, in catalogue order):

* :class:`SegmentSubjectInput`        — ``ClipRef + semantic_query → MaskRef`` (A)
* :class:`RemoveObjectInput`          — ``ObjectTrack + fill_policy → EffectLayerRef`` (B)
* :class:`ReplaceSkyInput`            — ``ClipRef + sky_asset + horizon_policy`` (B)
* :class:`RemoveBackgroundInput`      — ``SubjectMask + alpha_policy → AlphaLayerRef`` (B)
* :class:`TrackObjectInput`           — ``ClipRef + semantic_seed → ObjectTrack`` (A)
* :class:`TrackFaceInput`             — ``ClipRef + SubjectRef → FaceTrackSet`` (A)
* :class:`DetectShotBoundariesInput`  — ``ClipRef + threshold → ShotBoundarySet`` (A)
* :class:`FindSubjectMomentInput`     — ``ClipRef + SubjectQuery + event → CandidateMomentSet`` (A)
* :class:`RemoveLogoInput`            — ``LogoMask + legal_policy → EffectLayerRef`` (C)
* :class:`AutoReframeSubjectInput`    — ``SubjectTrack + aspect + safe_area → TransformCurve`` (B)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.packs.vision_common import SamplePolicy

SCENE_PACKAGE_ID = "nexus.vision.scene"
DOMAIN = "scene"

OPERATION_SEGMENT_SUBJECT = "scene.segment_subject"
OPERATION_REMOVE_OBJECT = "scene.remove_object"
OPERATION_REPLACE_SKY = "scene.replace_sky"
OPERATION_REMOVE_BACKGROUND = "scene.remove_background"
OPERATION_TRACK_OBJECT = "scene.track_object"
OPERATION_TRACK_FACE = "scene.track_face"
OPERATION_DETECT_SHOT_BOUNDARIES = "scene.detect_shot_boundaries"
OPERATION_FIND_SUBJECT_MOMENT = "scene.find_subject_moment"
OPERATION_REMOVE_LOGO = "scene.remove_logo"
OPERATION_AUTO_REFRAME_SUBJECT = "scene.auto_reframe_subject"

#: ``scene.remove_object`` fill strategies → feather and temporal window (pure data).
FILL_POLICIES: dict[str, dict[str, float]] = {
    "temporal_inpaint": {"feather_px": 2.0, "temporal_window_frames": 5.0},
    "blur_fill": {"feather_px": 6.0, "temporal_window_frames": 0.0},
    "solid_fill": {"feather_px": 1.0, "temporal_window_frames": 0.0},
}

#: ``scene.remove_logo`` legal policies → fill radius (pure data).
LOGO_POLICIES: dict[str, dict[str, float]] = {
    "blur": {"fill_radius_px": 12.0},
    "black": {"fill_radius_px": 1.0},
    "inpaint": {"fill_radius_px": 6.0},
}

#: Output aspect ratios for ``scene.auto_reframe_subject`` (exact rational pairs).
ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:5": (4, 5),
}

FillPolicy = Literal["temporal_inpaint", "blur_fill", "solid_fill"]
HorizonPolicy = Literal["auto", "manual"]
AlphaPolicy = Literal["hard", "soft", "matte"]
SubjectKind = Literal["face", "object", "speaker_face"]
SubjectEvent = Literal["first_visible", "last_visible", "best_visible"]
LogoPolicy = Literal["blur", "black", "inpaint"]
AspectLiteral = Literal["16:9", "9:16", "1:1", "4:5"]


class SegmentSubjectInput(BaseModel):
    """Input payload for ``scene.segment_subject`` (Level A → MaskRef)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    semantic_query: str = Field(min_length=1, max_length=200)
    start_us: int = Field(default=0, ge=0)
    duration_us: int = Field(default=1_000_000, gt=0)


class RemoveObjectInput(BaseModel):
    """Input payload for ``scene.remove_object`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    object_track_id: str = Field(min_length=1)
    fill_policy: FillPolicy = "temporal_inpaint"
    preserve_camera_motion: bool = True
    minimum_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    start_us: int = Field(default=0, ge=0)
    duration_us: int | None = Field(default=None, gt=0)
    output_asset_id: str | None = None


class ReplaceSkyInput(BaseModel):
    """Input payload for ``scene.replace_sky`` (Level B)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    sky_asset_id: str = Field(min_length=1)
    horizon_policy: HorizonPolicy = "auto"
    horizon_y_norm: float = Field(default=0.5, ge=0.0, le=1.0)
    feather_px: float = Field(default=8.0, ge=0.0, le=64.0)
    output_asset_id: str | None = None


class RemoveBackgroundInput(BaseModel):
    """Input payload for ``scene.remove_background`` (Level B → AlphaLayerRef)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(min_length=1, description="TDD SubjectMask (image asset).")
    alpha_policy: AlphaPolicy = "soft"
    output_asset_id: str | None = None


class TrackObjectInput(BaseModel):
    """Input payload for ``scene.track_object`` (Level A → ObjectTrack)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    semantic_seed: str = Field(min_length=1, max_length=200)
    sample_policy: SamplePolicy = "standard"


class TrackFaceInput(BaseModel):
    """Input payload for ``scene.track_face`` (Level A → FaceTrackSet)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    subject_id: str = Field(default="subject_01", min_length=1)
    sample_policy: SamplePolicy = "standard"
    landmark_count: int = Field(default=68, ge=1, le=68)


class DetectShotBoundariesInput(BaseModel):
    """Input payload for ``scene.detect_shot_boundaries`` (Level A)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    threshold: float = Field(default=0.6, ge=0.0, le=1.0)


class FindSubjectMomentInput(BaseModel):
    """Input payload for ``scene.find_subject_moment`` (Level A)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    subject_kind: SubjectKind = "face"
    subject_id: str = Field(default="subject_01", min_length=1)
    event: SubjectEvent = "first_visible"
    minimum_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    sample_policy: SamplePolicy = "standard"


class RemoveLogoInput(BaseModel):
    """Input payload for ``scene.remove_logo`` (Level C — legal/identity surface)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    mask_asset_id: str = Field(min_length=1, description="TDD LogoMask (image asset).")
    legal_policy: LogoPolicy = "blur"
    confirmed: bool = Field(
        default=False, description="Explicit user confirmation required for Level C execution."
    )
    output_asset_id: str | None = None


class AutoReframeSubjectInput(BaseModel):
    """Input payload for ``scene.auto_reframe_subject`` (Level B → TransformCurve)."""

    model_config = ConfigDict(extra="forbid")

    clip_asset_id: str = Field(min_length=1)
    object_track_id: str = Field(min_length=1)
    aspect: AspectLiteral = "9:16"
    safe_area: float = Field(default=0.05, ge=0.0, le=0.3)
    output_asset_id: str | None = None


__all__ = [
    "ASPECT_RATIOS",
    "AutoReframeSubjectInput",
    "DetectShotBoundariesInput",
    "DOMAIN",
    "FILL_POLICIES",
    "FindSubjectMomentInput",
    "LOGO_POLICIES",
    "OPERATION_AUTO_REFRAME_SUBJECT",
    "OPERATION_DETECT_SHOT_BOUNDARIES",
    "OPERATION_FIND_SUBJECT_MOMENT",
    "OPERATION_REMOVE_BACKGROUND",
    "OPERATION_REMOVE_LOGO",
    "OPERATION_REMOVE_OBJECT",
    "OPERATION_REPLACE_SKY",
    "OPERATION_SEGMENT_SUBJECT",
    "OPERATION_TRACK_FACE",
    "OPERATION_TRACK_OBJECT",
    "RemoveBackgroundInput",
    "RemoveLogoInput",
    "RemoveObjectInput",
    "ReplaceSkyInput",
    "SCENE_PACKAGE_ID",
    "SegmentSubjectInput",
    "TrackFaceInput",
    "TrackObjectInput",
]
