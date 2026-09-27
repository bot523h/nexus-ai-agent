"""Deterministic, pure vision planning substrate; no pixel/ML execution is claimed.

Digest semantics (explicit, honest):
* ``plan_digest`` — SHA-256 of the canonical JSON plan (operation + source evidence + parameters
  + processor). Deterministic, sensitive to every meaningful parameter, and the **only**
  digest the planning substrate ever produces. It is *not* a hash of real media bytes.
* ``artifact_content_sha256`` — content hash of the derived artifact. In this
  planning-only phase it is *derived from* ``plan_digest`` (``artifact = plan``)
  because no pixel engine runs; the provenance marks ``execution_boundary =
  deterministic_plan_only`` and ``pixel_execution = False``. When a real executor
  ships, this hash will be the SHA-256 of actual bytes and will diverge from the plan.
* ``content_hash`` / ``content_sha256`` — backward-compatible aliases for
  ``plan_digest`` / ``artifact_content_sha256`` (both equal in this phase).
  New code should prefer the explicit names.

Canonical serialization is JSON with ``sort_keys=True``, ``separators=(',',':')``,
``allow_nan=False`` — the same recipe ``compute_state_hash`` uses — so the digest
is byte-stable and deterministic across Python versions (see :func:`canonical_plan_bytes`).
"""

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
from nexus_ai_agent.creative.studio.semantic_references import validate_semantic_references

PROCESSOR = "nagar.vision.plan.v1"
PROCESSOR_VERSION = "1.0.0"


def canonical_plan_bytes(operation: str, source_sha256: str, parameters: dict, processor: str = PROCESSOR) -> bytes:
    """Deterministic bytes for plan digest (must remain stable for determinism tests)."""
    canonical = json.dumps(
        {
            "operation": operation,
            "source_sha256": source_sha256,
            "parameters": parameters,
            "processor": processor,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return canonical.encode("utf-8")


def plan_digest_for(operation: str, source_sha256: str, parameters: dict, processor: str = PROCESSOR) -> str:
    """SHA-256 hex of the canonical plan; the ``plan_digest`` (prefixed with ``sha256:`` by callers)."""
    return hashlib.sha256(canonical_plan_bytes(operation, source_sha256, parameters, processor)).hexdigest()


@dataclass(frozen=True)
class Definition:
    model: type[BaseModel]
    permission: PermissionLevel
    package: str
    result_kind: str
    allowed_media_kinds: tuple[str, ...] = ("video", "image")
    # Mapping from top-level field name to expected result_kind for semantic references.
    # Used by the generic ownership validator; unknown fields are ownership-only.
    expected_kinds: dict[str, str] | None = None


PORTRAIT: dict[str, Definition] = {
    "portrait.detect_landmarks": Definition(
        m.DetectLandmarksInput,
        PermissionLevel.IMMEDIATE,
        m.PORTRAIT_PACKAGE_ID,
        "face_landmark_set",
    ),
    "portrait.smooth_skin": Definition(
        m.SmoothSkinInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"face_tracks": "face_track_set"},
    ),
    "portrait.retouch_blemish": Definition(
        m.RetouchBlemishInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"mask": "mask"},
    ),
    "portrait.relight_face": Definition(
        m.RelightFaceInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"face_tracks": "face_track_set"},
    ),
    "portrait.whiten_teeth": Definition(
        m.WhitenTeethInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"teeth_mask": "mask"},
    ),
    "portrait.correct_gaze": Definition(
        m.CorrectGazeInput,
        PermissionLevel.CONFIRMATION,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"face_tracks": "face_track_set"},
    ),
    "portrait.enhance_eyes": Definition(
        m.EnhanceEyesInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"face_tracks": "face_track_set"},
    ),
    "portrait.mask_hair": Definition(
        m.MaskHairInput,
        PermissionLevel.IMMEDIATE,
        m.PORTRAIT_PACKAGE_ID,
        "mask",
        expected_kinds={"face_tracks": "face_track_set"},
    ),
    "portrait.background_blur": Definition(
        m.BackgroundBlurInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"subject_mask": "mask"},
    ),
    "portrait.stabilize_face": Definition(
        m.StabilizeFaceInput,
        PermissionLevel.REVERSIBLE,
        m.PORTRAIT_PACKAGE_ID,
        "transform_curve",
        allowed_media_kinds=("video",),
        expected_kinds={"face_tracks": "face_track_set"},
    ),
}
SCENE: dict[str, Definition] = {
    "scene.segment_subject": Definition(
        m.SegmentSubjectInput, PermissionLevel.IMMEDIATE, m.SCENE_PACKAGE_ID, "mask"
    ),
    "scene.remove_object": Definition(
        m.RemoveObjectInput,
        PermissionLevel.REVERSIBLE,
        m.SCENE_PACKAGE_ID,
        "effect_layer",
        allowed_media_kinds=("video",),
        expected_kinds={"object_track": "object_track"},
    ),
    "scene.replace_sky": Definition(
        m.ReplaceSkyInput,
        PermissionLevel.REVERSIBLE,
        m.SCENE_PACKAGE_ID,
        "effect_layer",
        expected_kinds={},  # sky_asset_id is validated as image separately
    ),
    "scene.remove_background": Definition(
        m.RemoveBackgroundInput,
        PermissionLevel.REVERSIBLE,
        m.SCENE_PACKAGE_ID,
        "alpha_layer",
        expected_kinds={"subject_mask": "mask"},
    ),
    "scene.track_object": Definition(
        m.TrackObjectInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "object_track",
        allowed_media_kinds=("video",),
    ),
    "scene.track_face": Definition(
        m.TrackFaceInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "face_track_set",
        allowed_media_kinds=("video",),
    ),
    "scene.detect_shot_boundaries": Definition(
        m.DetectShotBoundariesInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "shot_boundary_set",
        allowed_media_kinds=("video",),
    ),
    "scene.find_subject_moment": Definition(
        m.FindSubjectMomentInput,
        PermissionLevel.IMMEDIATE,
        m.SCENE_PACKAGE_ID,
        "candidate_moment_set",
        allowed_media_kinds=("video",),
    ),
    "scene.remove_logo": Definition(
        m.RemoveLogoInput,
        PermissionLevel.CONFIRMATION,
        m.SCENE_PACKAGE_ID,
        "effect_layer",
        expected_kinds={"logo_mask": "mask"},
    ),
    "scene.auto_reframe_subject": Definition(
        m.AutoReframeSubjectInput,
        PermissionLevel.REVERSIBLE,
        m.SCENE_PACKAGE_ID,
        "transform_curve",
        allowed_media_kinds=("video",),
        expected_kinds={"subject_track": "subject_track"},
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
        if source.media_kind not in definition.allowed_media_kinds:
            raise CommandValidationError(
                f"{operation}: source must be one of {definition.allowed_media_kinds!r}, got {source.media_kind!r}"
            )
        clip_range = data["clip"].get("range")
        if (
            clip_range
            and source.duration_us is not None
            and clip_range["end_us"] > source.duration_us
        ):
            raise CommandValidationError(f"{operation}: range exceeds source duration")
        # Generic semantic ownership + kind validation (core helper, vision-supplied mapping)
        if definition.expected_kinds is not None:
            validate_semantic_references(
                data,
                source_id,
                assets,
                operation=operation,
                expected_kinds=definition.expected_kinds,
            )
        else:
            # Still enforce cross-source ownership even when no kind expectation
            validate_semantic_references(data, source_id, assets, operation=operation)
        if "sky_asset_id" in data:
            sky_id = data["sky_asset_id"]
            sky = assets.get(sky_id)
            if sky is None:
                raise CommandValidationError(f"{operation}: unknown sky asset {sky_id!r}")
            if sky.media_kind != "image":
                raise CommandValidationError(f"{operation}: sky asset must be image, got {sky.media_kind!r}")
            # Sky must not be the source itself
            if sky_id == source_id:
                raise CommandValidationError(f"{operation}: sky asset collides with source")
        digest = plan_digest_for(operation, source.content_sha256, data, PROCESSOR)
        plan_digest = f"sha256:{digest}"
        # Planning-only artifact hash: derived from plan, not from real media bytes.
        # Documented as ``artifact_content_sha256``; ``content_sha256`` remains alias.
        artifact_content_sha256 = plan_digest
        output_id = data.get("output_asset_id") or f"vision_{digest[:20]}"
        if output_id in assets:
            raise CommandValidationError(f"{operation}: output asset already exists: {output_id!r}")
        provenance = {
            "operation": operation,
            "processor": PROCESSOR,
            "processor_version": PROCESSOR_VERSION,
            "source_asset_id": source_id,
            "parent_asset_ids": [source_id],
            "input_evidence_sha256": source.content_sha256,
            "parameters": data,
            "execution_boundary": "deterministic_plan_only",
            "pixel_execution": False,
            "result_kind": definition.result_kind,
            # Explicit plan identity (new, preferred)
            "plan_digest": plan_digest,
            "artifact_content_sha256": artifact_content_sha256,
            # Backward-compatible aliases (equal in plan-only phase)
            "content_hash": plan_digest,
            "digest_kind": "plan_only",
        }
        derived = AssetRecord(
            asset_id=output_id,
            media_kind=source.media_kind,
            content_sha256=artifact_content_sha256,
            duration_us=source.duration_us,
            parent_asset_ids=(source_id,),
            provenance=provenance,
        )
        result = {
            "asset_id": output_id,
            "result_kind": definition.result_kind,
            "source_asset_id": source_id,
            # New explicit names
            "plan_digest": plan_digest,
            "artifact_content_sha256": artifact_content_sha256,
            # Backward-compatible aliases
            "content_sha256": derived.content_sha256,
            "content_hash": plan_digest,
            "confidence": data["minimum_confidence"],
            "processor": PROCESSOR,
            "processor_version": PROCESSOR_VERSION,
            "pixel_execution": False,
            "execution_boundary": "deterministic_plan_only",
            "digest_kind": "plan_only",
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
