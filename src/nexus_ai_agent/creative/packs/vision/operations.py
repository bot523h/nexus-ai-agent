"""Deterministic, pure vision planning substrate; no pixel/ML execution is claimed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from nexus_ai_agent.creative.packs.vision import models as m
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
)

PROCESSOR = "nagar.vision.plan.v1"


@dataclass(frozen=True)
class Definition:
    model: type[BaseModel]
    permission: PermissionLevel
    package: str
    result_kind: str


PORTRAIT: dict[str, Definition] = {
    "portrait.detect_landmarks": Definition(
        m.DetectLandmarksInput,
        PermissionLevel.IMMEDIATE,
        m.PORTRAIT_PACKAGE_ID,
        "face_landmark_set",
    ),
    "portrait.smooth_skin": Definition(
        m.SmoothSkinInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.retouch_blemish": Definition(
        m.RetouchBlemishInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.relight_face": Definition(
        m.RelightFaceInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.whiten_teeth": Definition(
        m.WhitenTeethInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.correct_gaze": Definition(
        m.CorrectGazeInput, PermissionLevel.CONFIRMATION, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.enhance_eyes": Definition(
        m.EnhanceEyesInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.mask_hair": Definition(
        m.MaskHairInput, PermissionLevel.IMMEDIATE, m.PORTRAIT_PACKAGE_ID, "mask"
    ),
    "portrait.background_blur": Definition(
        m.BackgroundBlurInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "effect_layer"
    ),
    "portrait.stabilize_face": Definition(
        m.StabilizeFaceInput, PermissionLevel.REVERSIBLE, m.PORTRAIT_PACKAGE_ID, "transform_curve"
    ),
}
SCENE: dict[str, Definition] = {
    "scene.segment_subject": Definition(
        m.SegmentSubjectInput, PermissionLevel.IMMEDIATE, m.SCENE_PACKAGE_ID, "mask"
    ),
    "scene.remove_object": Definition(
        m.RemoveObjectInput, PermissionLevel.REVERSIBLE, m.SCENE_PACKAGE_ID, "effect_layer"
    ),
    "scene.replace_sky": Definition(
        m.ReplaceSkyInput, PermissionLevel.REVERSIBLE, m.SCENE_PACKAGE_ID, "effect_layer"
    ),
    "scene.remove_background": Definition(
        m.RemoveBackgroundInput, PermissionLevel.REVERSIBLE, m.SCENE_PACKAGE_ID, "alpha_layer"
    ),
    "scene.track_object": Definition(
        m.TrackObjectInput, PermissionLevel.IMMEDIATE, m.SCENE_PACKAGE_ID, "object_track"
    ),
    "scene.track_face": Definition(
        m.TrackFaceInput, PermissionLevel.IMMEDIATE, m.SCENE_PACKAGE_ID, "face_track_set"
    ),
    "scene.detect_shot_boundaries": Definition(
        m.DetectShotBoundariesInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "shot_boundary_set",
    ),
    "scene.find_subject_moment": Definition(
        m.FindSubjectMomentInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "candidate_moment_set",
    ),
    "scene.remove_logo": Definition(
        m.RemoveLogoInput, PermissionLevel.CONFIRMATION, m.SCENE_PACKAGE_ID, "effect_layer"
    ),
    "scene.auto_reframe_subject": Definition(
        m.AutoReframeSubjectInput, PermissionLevel.REVERSIBLE, m.SCENE_PACKAGE_ID, "transform_curve"
    ),
}


def _handler(
    operation: str, definition: Definition
) -> Callable[[Project, OperationContext], OperationOutcome]:
    def execute(project: Project, context: OperationContext) -> OperationOutcome:
        payload = definition.model.model_validate(context.input_data)
        data = payload.model_dump(mode="json")
        source_id = data["clip"]["asset_id"]
        assets = {a.asset_id: a for a in project.assets}
        source = assets.get(source_id)
        if source is None:
            raise CommandValidationError(f"{operation}: unknown source asset {source_id!r}")
        if source.media_kind not in ("video", "image"):
            raise CommandValidationError(f"{operation}: source must be video or image")
        clip_range = data["clip"].get("range")
        if (
            clip_range
            and source.duration_us is not None
            and clip_range["end_us"] > source.duration_us
        ):
            raise CommandValidationError(f"{operation}: range exceeds source duration")
        for key, value in data.items():
            if (
                isinstance(value, dict)
                and "source_asset_id" in value
                and value["source_asset_id"] != source_id
            ):
                raise CommandValidationError(f"{operation}: {key} belongs to another source asset")
        if "sky_asset_id" in data and data["sky_asset_id"] not in assets:
            raise CommandValidationError(f"{operation}: unknown sky asset")
        canonical = json.dumps(
            {
                "operation": operation,
                "source_sha256": source.content_sha256,
                "parameters": data,
                "processor": PROCESSOR,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        output_id = data.get("output_asset_id") or f"vision_{digest[:20]}"
        if output_id in assets:
            raise CommandValidationError(f"{operation}: output asset already exists: {output_id!r}")
        provenance = {
            "operation": operation,
            "processor": PROCESSOR,
            "processor_version": "1.0.0",
            "source_asset_id": source_id,
            "parent_asset_ids": [source_id],
            "input_evidence_sha256": source.content_sha256,
            "parameters": data,
            "execution_boundary": "deterministic_plan_only",
            "pixel_execution": False,
            "result_kind": definition.result_kind,
            "content_hash": f"sha256:{digest}",
        }
        derived = AssetRecord(
            asset_id=output_id,
            media_kind=source.media_kind,
            content_sha256=f"sha256:{digest}",
            duration_us=source.duration_us,
            parent_asset_ids=(source_id,),
            provenance=provenance,
        )
        result = {
            "asset_id": output_id,
            "result_kind": definition.result_kind,
            "source_asset_id": source_id,
            "content_sha256": derived.content_sha256,
            "confidence": data["minimum_confidence"],
            "processor": PROCESSOR,
            "pixel_execution": False,
            "provenance": provenance,
        }
        return OperationOutcome(
            project.model_copy(update={"assets": [*project.assets, derived]}),
            context.history,
            result,
        )

    execute.__name__ = "execute_" + operation.replace(".", "_")
    return execute


def _register(registry: CapabilityRegistry, definitions: dict[str, Definition]) -> None:
    for operation, definition in definitions.items():
        registry.register_operation(
            operation.split(".")[0],
            operation.split(".")[1],
            OperationSpec(
                operation_id=operation,
                description=(
                    f"Deterministic semantic plan for {operation}; pixel engine is not bundled."
                ),
                permission_level=definition.permission,
                input_model=definition.model,
                handler=_handler(operation, definition),
                required_packs=(definition.package,),
                deterministic=True,
            ),
        )


def register_portrait_operations(registry: CapabilityRegistry) -> None:
    _register(registry, PORTRAIT)


def register_scene_operations(registry: CapabilityRegistry) -> None:
    _register(registry, SCENE)
