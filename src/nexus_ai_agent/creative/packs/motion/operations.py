"""Pure operations for the ``nexus.motion.graphics`` pack (Wave 6 substrate).

This module implements the Nagar Command Bus operations for video transitions,
keyframe animation, bloom/glow, motion blur, and kinetic motion titles:
* ``motion.add_transition`` (Level B, REVERSIBLE): Crossfade and wipe transitions.
* ``motion.keyframe_transform`` (Level B, REVERSIBLE): 2D affine transform animation.
* ``motion.add_glow`` (Level B, REVERSIBLE): Thresholded bloom and glow effects.
* ``motion.add_motion_blur`` (Level B, REVERSIBLE): Temporal shutter angle motion blur.
* ``motion.add_title`` (Level B, REVERSIBLE): Kinetic typography and lower thirds.
"""

from __future__ import annotations

import hashlib
import uuid

from nexus_ai_agent.creative.packs.motion.models import (
    DOMAIN,
    MOTION_PACKAGE_ID,
    OPERATION_ADD_GLOW,
    OPERATION_ADD_MOTION_BLUR,
    OPERATION_ADD_TITLE,
    OPERATION_ADD_TRANSITION,
    OPERATION_KEYFRAME_TRANSFORM,
    AddGlowInput,
    AddMotionBlurInput,
    AddTitleInput,
    AddTransitionInput,
    KeyframeTransformInput,
)
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


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {a.asset_id: a for a in project.assets}


def _add_transition(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_transition."""
    payload = AddTransitionInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.left_clip_id not in known:
        raise CommandValidationError(
            f"motion.add_transition references unknown left clip: {payload.left_clip_id!r}"
        )
    if payload.right_clip_id not in known:
        raise CommandValidationError(
            f"motion.add_transition references unknown right clip: {payload.right_clip_id!r}"
        )

    left_rec = known[payload.left_clip_id]
    right_rec = known[payload.right_clip_id]

    output_id = payload.output_asset_id or f"trans_{payload.left_clip_id}_{payload.right_clip_id}"
    digest_seed = f"{left_rec.content_sha256}:{right_rec.content_sha256}:{payload.kind}:{payload.duration_us}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    transition_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=payload.duration_us,
        parent_asset_ids=(left_rec.asset_id, right_rec.asset_id),
        provenance={
            "effect": "transition",
            "kind": payload.kind,
            "duration_us": payload.duration_us,
            "easing": payload.easing,
            "left_clip_id": left_rec.asset_id,
            "right_clip_id": right_rec.asset_id,
            "processor": "nagar.motion.transition.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, transition_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "kind": payload.kind,
            "duration_us": payload.duration_us,
            "easing": payload.easing,
            "content_sha256": transition_record.content_sha256,
        },
    )


def _keyframe_transform(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.keyframe_transform."""
    payload = KeyframeTransformInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"motion.keyframe_transform references unknown clip: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_anim"
    digest_seed = f"{clip_rec.content_sha256}:kf:{len(payload.keyframes)}:{payload.easing}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    anim_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "effect": "keyframe_transform",
            "source_clip_id": clip_rec.asset_id,
            "keyframe_count": len(payload.keyframes),
            "easing": payload.easing,
            "processor": "nagar.motion.transform.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, anim_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip_rec.asset_id,
            "keyframe_count": len(payload.keyframes),
            "content_sha256": anim_record.content_sha256,
        },
    )


def _add_glow(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_glow."""
    payload = AddGlowInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"motion.add_glow references unknown clip: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_glow"
    digest_seed = f"{clip_rec.content_sha256}:glow:{payload.radius_px}:{payload.intensity}:{payload.threshold}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    glow_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "effect": "glow",
            "source_clip_id": clip_rec.asset_id,
            "radius_px": payload.radius_px,
            "intensity": payload.intensity,
            "threshold": payload.threshold,
            "processor": "nagar.motion.glow.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, glow_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip_rec.asset_id,
            "radius_px": payload.radius_px,
            "intensity": payload.intensity,
            "content_sha256": glow_record.content_sha256,
        },
    )


def _add_motion_blur(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_motion_blur."""
    payload = AddMotionBlurInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"motion.add_motion_blur references unknown clip: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_mblur"
    digest_seed = f"{clip_rec.content_sha256}:mblur:{payload.shutter_angle_deg}:{payload.samples}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    blur_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "effect": "motion_blur",
            "source_clip_id": clip_rec.asset_id,
            "shutter_angle_deg": payload.shutter_angle_deg,
            "samples": payload.samples,
            "processor": "nagar.motion.blur.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, blur_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip_rec.asset_id,
            "shutter_angle_deg": payload.shutter_angle_deg,
            "samples": payload.samples,
            "content_sha256": blur_record.content_sha256,
        },
    )


def _add_title(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_title."""
    payload = AddTitleInput.model_validate(context.input_data)
    output_id = payload.output_asset_id or f"title_{uuid.uuid4().hex[:12]}"
    digest_seed = f"title:{payload.text}:{payload.animation_style}:{payload.font_name}:{payload.duration_us}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    title_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=payload.duration_us,
        parent_asset_ids=(),
        provenance={
            "effect": "kinetic_title",
            "text": payload.text,
            "animation_style": payload.animation_style,
            "font_name": payload.font_name,
            "font_size": payload.font_size,
            "color": payload.color,
            "position": payload.position,
            "processor": "nagar.motion.title.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, title_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "text": payload.text,
            "animation_style": payload.animation_style,
            "duration_us": payload.duration_us,
            "position": payload.position,
            "content_sha256": title_record.content_sha256,
        },
    )


def register_motion_operations(registry: CapabilityRegistry) -> None:
    """Register all motion graphics operations with the capability registry."""
    registry.register_operation(
        DOMAIN,
        "add_transition",
        OperationSpec(
            operation_id=OPERATION_ADD_TRANSITION,
            description="Add transition effect between two video clips (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddTransitionInput,
            handler=_add_transition,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "keyframe_transform",
        OperationSpec(
            operation_id=OPERATION_KEYFRAME_TRANSFORM,
            description="Apply keyframed 2D transform animation to a video clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=KeyframeTransformInput,
            handler=_keyframe_transform,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "add_glow",
        OperationSpec(
            operation_id=OPERATION_ADD_GLOW,
            description="Add bloom and glow effect layer to a video clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddGlowInput,
            handler=_add_glow,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "add_motion_blur",
        OperationSpec(
            operation_id=OPERATION_ADD_MOTION_BLUR,
            description="Apply shutter-angle temporal motion blur to a video clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddMotionBlurInput,
            handler=_add_motion_blur,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "add_title",
        OperationSpec(
            operation_id=OPERATION_ADD_TITLE,
            description="Create animated kinetic title or lower third (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddTitleInput,
            handler=_add_title,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )


def build_motion_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with motion graphics and Wave 1 operations."""
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    registry = build_wave1_registry()
    register_motion_operations(registry)
    return registry
