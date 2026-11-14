"""Strict semantic contracts shared by the Nagar portrait and scene packs."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PORTRAIT_PACKAGE_ID = "nexus.vision.portrait"
SCENE_PACKAGE_ID = "nexus.vision.scene"
Identifier = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
VisionConfidence = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
FiniteUnit = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TimeRangeUS(StrictModel):
    start_us: int = Field(strict=True, ge=0)
    end_us: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def ordered(self) -> TimeRangeUS:
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")
        return self


class ClipRef(StrictModel):
    asset_id: Identifier
    range: TimeRangeUS | None = None


class SubjectRef(StrictModel):
    subject_id: Identifier


class MaskRef(StrictModel):
    mask_id: Identifier
    source_asset_id: Identifier


class FaceTrackSet(StrictModel):
    track_set_id: Identifier
    source_asset_id: Identifier
    confidence: VisionConfidence


class FaceLandmarkSet(StrictModel):
    landmark_set_id: Identifier
    face_tracks: FaceTrackSet
    confidence: VisionConfidence


class SubjectTrack(StrictModel):
    track_id: Identifier
    subject: SubjectRef
    source_asset_id: Identifier
    confidence: VisionConfidence


class ObjectTrack(StrictModel):
    track_id: Identifier
    source_asset_id: Identifier
    confidence: VisionConfidence


class ShotBoundarySet(StrictModel):
    boundary_set_id: Identifier
    source_asset_id: Identifier
    confidence: VisionConfidence


class CandidateMomentSet(StrictModel):
    moment_set_id: Identifier
    source_asset_id: Identifier
    confidence: VisionConfidence


class TransformCurve(StrictModel):
    curve_id: Identifier
    source_asset_id: Identifier
    range: TimeRangeUS | None = None


class EffectLayerRef(StrictModel):
    layer_id: Identifier
    source_asset_id: Identifier


class DerivedAsset(StrictModel):
    asset_id: Identifier
    content_sha256: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class RetouchPolicy(StrictModel):
    strength: FiniteUnit = 0.5
    preserve_texture: bool = True


class RelightPolicy(StrictModel):
    exposure_ev: Annotated[float, Field(strict=True, ge=-3, le=3, allow_inf_nan=False)] = 0.0
    direction: Literal["front", "left", "right", "top"] = "front"


class GazeTarget(StrictModel):
    horizontal: Annotated[float, Field(strict=True, ge=-1, le=1, allow_inf_nan=False)] = 0.0
    vertical: Annotated[float, Field(strict=True, ge=-1, le=1, allow_inf_nan=False)] = 0.0


class HairMaskPolicy(StrictModel):
    edge_quality: Literal["fast", "balanced", "fine"] = "balanced"


class BackgroundBlurProfile(StrictModel):
    radius: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] = 20.0
    feather: FiniteUnit = 0.2


class SkyReplacementPolicy(StrictModel):
    horizon: Literal["auto", "preserve", "soft"] = "auto"
    blend: FiniteUnit = 0.5


class InpaintPolicy(StrictModel):
    temporal_radius: int = Field(strict=True, ge=1, le=120)
    fill: Literal["temporal", "patch"] = "temporal"


class ReframeProfile(StrictModel):
    aspect: Literal["16:9", "9:16", "1:1", "4:5"]
    safe_area: FiniteUnit = 0.9


class SourceInput(StrictModel):
    clip: ClipRef
    output_asset_id: Identifier | None = None
    minimum_confidence: VisionConfidence = 0.5


class DetectLandmarksInput(SourceInput):
    sample_interval: int = Field(default=1, strict=True, ge=1, le=120)


class SmoothSkinInput(SourceInput):
    face_tracks: FaceTrackSet
    policy: RetouchPolicy = RetouchPolicy()


class RetouchBlemishInput(SourceInput):
    mask: MaskRef
    policy: RetouchPolicy = RetouchPolicy()


class RelightFaceInput(SourceInput):
    face_tracks: FaceTrackSet
    policy: RelightPolicy = RelightPolicy()


class WhitenTeethInput(SourceInput):
    teeth_mask: MaskRef
    intensity: FiniteUnit = 0.3


class CorrectGazeInput(SourceInput):
    face_tracks: FaceTrackSet
    target: GazeTarget = GazeTarget()


class EnhanceEyesInput(SourceInput):
    face_tracks: FaceTrackSet
    clarity: FiniteUnit = 0.4
    red_eye: bool = False


class MaskHairInput(SourceInput):
    face_tracks: FaceTrackSet
    policy: HairMaskPolicy = HairMaskPolicy()


class BackgroundBlurInput(SourceInput):
    subject_mask: MaskRef
    profile: BackgroundBlurProfile = BackgroundBlurProfile()


class StabilizeFaceInput(SourceInput):
    face_tracks: FaceTrackSet
    smoothing: FiniteUnit = 0.7


class SegmentSubjectInput(SourceInput):
    query: Identifier


class RemoveObjectInput(SourceInput):
    object_track: ObjectTrack
    policy: InpaintPolicy


class ReplaceSkyInput(SourceInput):
    sky_asset_id: Identifier
    policy: SkyReplacementPolicy = SkyReplacementPolicy()


class RemoveBackgroundInput(SourceInput):
    subject_mask: MaskRef
    alpha_mode: Literal["straight", "premultiplied"] = "straight"


class TrackObjectInput(SourceInput):
    semantic_seed: Identifier


class TrackFaceInput(SourceInput):
    subject: SubjectRef


class DetectShotBoundariesInput(SourceInput):
    threshold: VisionConfidence = 0.5


class FindSubjectMomentInput(SourceInput):
    subject: SubjectRef
    event: Literal["first_visible", "last_visible", "speaking", "centered"]


class RemoveLogoInput(SourceInput):
    logo_mask: MaskRef
    legal_basis: Literal["owned", "licensed", "authorized"]


class AutoReframeSubjectInput(SourceInput):
    subject_track: SubjectTrack
    profile: ReframeProfile
