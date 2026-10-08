"""Pure operations for the ``nexus.motion.graphics`` pack (Wave 6 substrate).

This module implements the Nagar Command Bus operations for video transitions,
keyframe animation, bloom/glow, motion blur, and kinetic motion titles:
* ``motion.add_transition`` (Level B, REVERSIBLE): Crossfade and wipe transitions.
* ``motion.keyframe_transform`` (Level B, REVERSIBLE): 2D affine transform animation.
* ``motion.add_glow`` (Level B, REVERSIBLE): Thresholded bloom and glow effects.
* ``motion.add_motion_blur`` (Level B, REVERSIBLE): Temporal shutter angle motion blur.
* ``motion.add_title`` (Level B, REVERSIBLE): Kinetic typography and lower thirds.
* ``motion.apply_mask`` (Level B, REVERSIBLE): Mask and feather compositing layer.
* ``motion.warp`` (Level B, REVERSIBLE): Mesh-warp displacement curve.
* ``motion.add_particles`` (Level B, REVERSIBLE): Deterministic particle emitter layer.
"""

from __future__ import annotations

import hashlib
import uuid

from nexus_ai_agent.creative.packs.motion.models import (
    DOMAIN,
    MOTION_PACKAGE_ID,
    OPERATION_ADD_GLOW,
    OPERATION_ADD_MOTION_BLUR,
    OPERATION_ADD_PARALLAX,
    OPERATION_ADD_PARTICLES,
    OPERATION_ADD_TITLE,
    OPERATION_ADD_TRANSITION,
    OPERATION_APPLY_MASK,
    OPERATION_KEYFRAME_TRANSFORM,
    OPERATION_STABILIZE,
    OPERATION_WARP,
    AddGlowInput,
    AddMotionBlurInput,
    AddParallaxInput,
    AddParticlesInput,
    AddTitleInput,
    AddTransitionInput,
    ApplyMaskInput,
    KeyframeTransformInput,
    StabilizeInput,
    WarpInput,
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
    digest_seed = (
        f"{left_rec.content_sha256}:{right_rec.content_sha256}:{payload.kind}:{payload.duration_us}"
    )
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

    new_project = project.model_copy(update={"assets": [*project.assets, transition_record]})
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

    new_project = project.model_copy(update={"assets": [*project.assets, anim_record]})
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
    digest_seed = (
        f"{clip_rec.content_sha256}:glow:{payload.radius_px}:"
        f"{payload.intensity}:{payload.threshold}"
    )
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

    new_project = project.model_copy(update={"assets": [*project.assets, glow_record]})
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

    new_project = project.model_copy(update={"assets": [*project.assets, blur_record]})
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
    digest_seed = (
        f"title:{payload.text}:{payload.animation_style}:{payload.font_name}:{payload.duration_us}"
    )
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

    new_project = project.model_copy(update={"assets": [*project.assets, title_record]})
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


def _require_video_asset(
    known: dict[str, AssetRecord], asset_id: str, operation_id: str
) -> AssetRecord:
    """Resolve ``asset_id`` or raise; enforces the ``video`` media kind."""
    if asset_id not in known:
        raise CommandValidationError(f"{operation_id} references unknown video asset: {asset_id!r}")
    record = known[asset_id]
    if record.media_kind != "video":
        raise CommandValidationError(
            f"{operation_id} requires a video asset, got {record.media_kind!r} for {asset_id!r}"
        )
    return record


def _stabilize(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.stabilize.

    Derives a stabilized clip plan (camera-shake compensation with crop mode).
    """
    payload = StabilizeInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_video_asset(known, payload.clip_asset_id, OPERATION_STABILIZE)

    output_id = payload.output_asset_id or f"stabilized_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:stabilize:{payload.strength}:{payload.crop_mode}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    stabilized_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "strength": payload.strength,
            "crop_mode": payload.crop_mode,
            "processor": "nagar.motion.graphics.stabilize.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, stabilized_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "strength": payload.strength,
            "crop_mode": payload.crop_mode,
            "content_sha256": stabilized_record.content_sha256,
        },
    )


def _add_parallax(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_parallax.

    Derives a parallax clip plan with a deterministic per-layer offset table:
    layer 0 is the foreground (full offset) and the last layer is the
    background (zero offset).
    """
    payload = AddParallaxInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_video_asset(known, payload.clip_asset_id, OPERATION_ADD_PARALLAX)

    layer_count = payload.depth_layers
    layer_plan = [
        {
            "layer": index,
            "depth": round(index / (layer_count - 1), 4),
            "offset_factor": round((1.0 - index / (layer_count - 1)) * payload.intensity, 4),
            "axis": payload.direction,
        }
        for index in range(layer_count)
    ]

    output_id = payload.output_asset_id or f"parallax_{uuid.uuid4().hex[:12]}"
    digest_seed = (
        f"{source.content_sha256}:parallax:{layer_count}:{payload.intensity}:{payload.direction}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    parallax_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "depth_layers": layer_count,
            "intensity": payload.intensity,
            "direction": payload.direction,
            "processor": "nagar.motion.graphics.parallax.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, parallax_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "layer_plan": layer_plan,
            "content_sha256": parallax_record.content_sha256,
        },
    )


def _apply_mask(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.apply_mask.

    Derives a masked clip plan.  The mask asset must be an image and contributes
    only its content hash to the derived digest (white = keep, ``invert`` flips
    the polarity), which keeps the operation pure and reproducible.
    """
    payload = ApplyMaskInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_video_asset(known, payload.clip_asset_id, OPERATION_APPLY_MASK)
    if payload.mask_asset_id not in known:
        raise CommandValidationError(
            f"{OPERATION_APPLY_MASK} references unknown mask asset: {payload.mask_asset_id!r}"
        )
    mask = known[payload.mask_asset_id]
    if mask.media_kind != "image":
        raise CommandValidationError(
            f"{OPERATION_APPLY_MASK} requires an image mask asset, got "
            f"{mask.media_kind!r} for {payload.mask_asset_id!r}"
        )

    output_id = payload.output_asset_id or f"masked_{uuid.uuid4().hex[:12]}"
    digest_seed = (
        f"{source.content_sha256}:mask:{mask.content_sha256}:{payload.feather_px}:{payload.invert}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    masked_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id, mask.asset_id),
        provenance={
            "effect": "mask",
            "source_asset_id": source.asset_id,
            "mask_asset_id": mask.asset_id,
            "mask_sha256": mask.content_sha256,
            "feather_px": payload.feather_px,
            "invert": payload.invert,
            "processor": "nagar.motion.graphics.mask.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, masked_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "mask_asset_id": mask.asset_id,
            "feather_px": payload.feather_px,
            "invert": payload.invert,
            "content_sha256": masked_record.content_sha256,
        },
    )


def _warp(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.warp.

    Derives a mesh-warp clip plan and normalizes the caller's control points into
    an explicit ``mesh_plan`` (grid position, displacement, time offset), so the
    impure apply lane never has to re-derive anything from the raw payload.
    """
    payload = WarpInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_video_asset(known, payload.clip_asset_id, OPERATION_WARP)

    mesh_plan = [
        {
            "row": point.row,
            "col": point.col,
            "time_offset_us": point.time_offset_us,
            "dx": point.dx,
            "dy": point.dy,
            "displacement": round((point.dx**2 + point.dy**2) ** 0.5, 6),
        }
        for point in payload.control_points
    ]
    max_displacement = round(max(entry["displacement"] for entry in mesh_plan), 6)

    output_id = payload.output_asset_id or f"warp_{uuid.uuid4().hex[:12]}"
    digest_seed = (
        f"{source.content_sha256}:warp:{payload.mesh_rows}x{payload.mesh_cols}:"
        f"{len(payload.control_points)}:{payload.easing}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    warp_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "effect": "mesh_warp",
            "source_asset_id": source.asset_id,
            "mesh": f"{payload.mesh_rows}x{payload.mesh_cols}",
            "control_point_count": len(payload.control_points),
            "easing": payload.easing,
            "processor": "nagar.motion.graphics.warp.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, warp_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "mesh": f"{payload.mesh_rows}x{payload.mesh_cols}",
            "control_point_count": len(payload.control_points),
            "max_displacement": max_displacement,
            "mesh_plan": mesh_plan,
            "content_sha256": warp_record.content_sha256,
        },
    )


def _particle_samples(seed: int, count: int) -> list[dict[str, float]]:
    """Deterministic particle table: ``sha256(seed:index)`` -> normalized floats.

    No RNG is involved, so the same command yields the same samples on every
    platform and in every process — the property the command bus relies on to
    call a handler ``deterministic=True``.
    """
    samples: list[dict[str, float]] = []
    for index in range(count):
        raw = hashlib.sha256(f"{seed}:{index}".encode()).digest()
        unit = [byte / 255.0 for byte in raw]
        samples.append(
            {
                "index": index,
                "x": round(unit[0], 6),
                "y": round(unit[1], 6),
                "angle_deg": round(unit[2] * 360.0, 6),
                "speed_factor": round(unit[3], 6),
                "lifetime_factor": round(unit[4], 6),
            }
        )
    return samples


def _add_particles(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for motion.add_particles.

    Derives a particle-layer clip plan over ``[start_us, end_us)``: the emitter
    digest, the emission rate and a bounded sample table (first 8 particles) are
    emitted as evidence, so the layer is inspectable without running a compositor.
    """
    payload = AddParticlesInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_video_asset(known, payload.clip_asset_id, OPERATION_ADD_PARTICLES)

    window_us = payload.end_us - payload.start_us
    window_start = payload.start_us
    if window_start >= source.duration_us:
        raise CommandValidationError(
            f"{OPERATION_ADD_PARTICLES} window starts at {window_start}us but "
            f"{source.asset_id!r} is only {source.duration_us}us long"
        )
    window_end = min(payload.end_us, source.duration_us)

    emitter_digest = hashlib.sha256(
        ":".join(
            [
                str(payload.emitter.particles),
                str(payload.emitter.lifetime_us),
                str(payload.emitter.velocity_px_s),
                str(payload.emitter.gravity),
                str(payload.emitter.spread_deg),
                str(payload.emitter.seed),
            ]
        ).encode("utf-8")
    ).hexdigest()
    emission_rate_hz = round(payload.emitter.particles / (window_us / 1_000_000), 4)
    samples = _particle_samples(payload.emitter.seed, min(payload.emitter.particles, 8))

    output_id = payload.output_asset_id or f"particles_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:particles:{emitter_digest}:{window_start}:{window_end}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    particles_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=window_end - window_start,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "effect": "particles",
            "source_asset_id": source.asset_id,
            "emitter": payload.emitter.model_dump(),
            "emitter_digest": f"sha256:{emitter_digest}",
            "window_start_us": window_start,
            "window_end_us": window_end,
            "processor": "nagar.motion.graphics.particles.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, particles_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "emitter_digest": f"sha256:{emitter_digest}",
            "emission_rate_hz": emission_rate_hz,
            "window_us": window_end - window_start,
            "window_clamped": window_end != payload.end_us,
            "particle_samples": samples,
            "content_sha256": particles_record.content_sha256,
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
    registry.register_operation(
        DOMAIN,
        "stabilize",
        OperationSpec(
            operation_id=OPERATION_STABILIZE,
            description="Derive a stabilized clip plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=StabilizeInput,
            handler=_stabilize,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "add_parallax",
        OperationSpec(
            operation_id=OPERATION_ADD_PARALLAX,
            description="Derive a depth-layer parallax clip plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddParallaxInput,
            handler=_add_parallax,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "apply_mask",
        OperationSpec(
            operation_id=OPERATION_APPLY_MASK,
            description="Derive a masked clip plan with feathering from an image mask (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ApplyMaskInput,
            handler=_apply_mask,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "warp",
        OperationSpec(
            operation_id=OPERATION_WARP,
            description="Derive a mesh-warp clip plan from a time-varying mesh curve (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=WarpInput,
            handler=_warp,
            required_packs=(MOTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "add_particles",
        OperationSpec(
            operation_id=OPERATION_ADD_PARTICLES,
            description="Derive a deterministic particle-layer clip plan over a window (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AddParticlesInput,
            handler=_add_particles,
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
