"""Pure operations for the ``nexus.edit.timeline`` pack (Wave 3 substrate).

This module implements the Nagar Command Bus operations for non-destructive timeline editing:
* ``timeline.trim`` (Level B, REVERSIBLE): Non-destructive in/out trimming.
* ``timeline.ripple_delete`` (Level B, REVERSIBLE): Cut out a time segment and collapse the gap.
* ``timeline.insert_gap`` (Level B, REVERSIBLE): Insert a blank gap and shift downstream elements.
* ``timeline.speed_ramp`` (Level B, REVERSIBLE): Change clip playback rate with pitch retention.
* ``timeline.reverse_segment`` (Level B, REVERSIBLE): Create time-reversed clip asset.
* ``timeline.freeze_frame`` (Level B, REVERSIBLE): Extract and hold a single frame for duration.
* ``timeline.attach_b_roll`` (Level B, REVERSIBLE): Attach an overlay B-roll track segment.
* ``timeline.retime_to_music`` (Level B, REVERSIBLE): Align clip boundaries to musical tempo beats.
"""

from __future__ import annotations

import hashlib

from nexus_ai_agent.creative.packs.edit.models import (
    DOMAIN,
    EDIT_PACKAGE_ID,
    OPERATION_ATTACH_B_ROLL,
    OPERATION_FREEZE_FRAME,
    OPERATION_INSERT_GAP,
    OPERATION_RETIME_TO_MUSIC,
    OPERATION_REVERSE_SEGMENT,
    OPERATION_RIPPLE_DELETE,
    OPERATION_SPEED_RAMP,
    OPERATION_TRIM,
    AttachBRollInput,
    FreezeFrameInput,
    InsertGapInput,
    RetimeToMusicInput,
    ReverseSegmentInput,
    RippleDeleteInput,
    SpeedRampInput,
    TrimInput,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
)


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {a.asset_id: a for a in project.assets}


def _trim(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.trim."""
    payload = TrimInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"timeline.trim references unknown clip asset: {payload.clip_asset_id!r}"
        )

    source_rec = known[payload.clip_asset_id]
    trimmed_duration_us = payload.out_point_us - payload.in_point_us
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_trim"

    digest_seed = f"{source_rec.content_sha256}:trim:{payload.in_point_us}:{payload.out_point_us}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    trimmed_rec = AssetRecord(
        asset_id=output_id,
        media_kind=source_rec.media_kind,
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=trimmed_duration_us,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "action": "trim",
            "source_asset_id": source_rec.asset_id,
            "in_point_us": payload.in_point_us,
            "out_point_us": payload.out_point_us,
            "processor": "nagar.timeline.trim.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, trimmed_rec]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source_rec.asset_id,
            "in_point_us": payload.in_point_us,
            "out_point_us": payload.out_point_us,
            "duration_us": trimmed_duration_us,
            "content_sha256": trimmed_rec.content_sha256,
        },
    )


def _ripple_delete(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.ripple_delete."""
    payload = RippleDeleteInput.model_validate(context.input_data)
    new_duration_us = max(0, project.timeline.duration_us - payload.duration_us)

    updated_timeline = project.timeline.model_copy(update={"duration_us": new_duration_us})
    new_project = project.model_copy(update={"timeline": updated_timeline})

    return OperationOutcome(
        new_project,
        context.history,
        {
            "track_id": payload.track_id,
            "deleted_start_us": payload.start_us,
            "deleted_duration_us": payload.duration_us,
            "new_timeline_duration_us": new_duration_us,
        },
    )


def _insert_gap(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.insert_gap."""
    payload = InsertGapInput.model_validate(context.input_data)
    new_duration_us = project.timeline.duration_us + payload.duration_us

    updated_timeline = project.timeline.model_copy(update={"duration_us": new_duration_us})
    new_project = project.model_copy(update={"timeline": updated_timeline})

    return OperationOutcome(
        new_project,
        context.history,
        {
            "track_id": payload.track_id,
            "gap_at_us": payload.at_us,
            "gap_duration_us": payload.duration_us,
            "new_timeline_duration_us": new_duration_us,
        },
    )


def _speed_ramp(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.speed_ramp."""
    payload = SpeedRampInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"timeline.speed_ramp references unknown clip: {payload.clip_asset_id!r}"
        )

    source_rec = known[payload.clip_asset_id]
    original_dur = source_rec.duration_us or 1_000_000
    new_dur = max(1, int(original_dur / payload.speed_factor))
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_speed_{payload.speed_factor}"

    digest_seed = (
        f"{source_rec.content_sha256}:speed:{payload.speed_factor}:{payload.maintain_pitch}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    ramped_rec = AssetRecord(
        asset_id=output_id,
        media_kind=source_rec.media_kind,
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=new_dur,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "action": "speed_ramp",
            "source_asset_id": source_rec.asset_id,
            "speed_factor": payload.speed_factor,
            "maintain_pitch": payload.maintain_pitch,
            "original_duration_us": original_dur,
            "processor": "nagar.timeline.speed.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, ramped_rec]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "speed_factor": payload.speed_factor,
            "new_duration_us": new_dur,
            "maintain_pitch": payload.maintain_pitch,
            "content_sha256": ramped_rec.content_sha256,
        },
    )


def _reverse_segment(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.reverse_segment."""
    payload = ReverseSegmentInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"timeline.reverse_segment references unknown clip: {payload.clip_asset_id!r}"
        )

    source_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_reversed"

    digest_seed = f"{source_rec.content_sha256}:reverse"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    reversed_rec = AssetRecord(
        asset_id=output_id,
        media_kind=source_rec.media_kind,
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source_rec.duration_us,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "action": "reverse",
            "source_asset_id": source_rec.asset_id,
            "playback_rate": -1.0,
            "processor": "nagar.timeline.reverse.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, reversed_rec]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source_rec.asset_id,
            "playback_rate": -1.0,
            "content_sha256": reversed_rec.content_sha256,
        },
    )


def _freeze_frame(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.freeze_frame."""
    payload = FreezeFrameInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"timeline.freeze_frame references unknown clip: {payload.clip_asset_id!r}"
        )

    source_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_freeze_{payload.freeze_at_us}"

    digest_seed = f"{source_rec.content_sha256}:freeze:{payload.freeze_at_us}:{payload.duration_us}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    freeze_rec = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=payload.duration_us,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "action": "freeze_frame",
            "source_asset_id": source_rec.asset_id,
            "freeze_at_us": payload.freeze_at_us,
            "hold_duration_us": payload.duration_us,
            "processor": "nagar.timeline.freeze.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, freeze_rec]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source_rec.asset_id,
            "freeze_at_us": payload.freeze_at_us,
            "hold_duration_us": payload.duration_us,
            "content_sha256": freeze_rec.content_sha256,
        },
    )


def _attach_b_roll(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.attach_b_roll."""
    payload = AttachBRollInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.main_clip_id not in known:
        raise CommandValidationError(
            f"timeline.attach_b_roll references unknown main clip: {payload.main_clip_id!r}"
        )
    if payload.b_roll_asset_id not in known:
        raise CommandValidationError(
            f"timeline.attach_b_roll references unknown B-roll asset: {payload.b_roll_asset_id!r}"
        )

    main_rec = known[payload.main_clip_id]
    broll_rec = known[payload.b_roll_asset_id]
    dur = payload.duration_us or broll_rec.duration_us or 3_000_000
    output_id = payload.output_asset_id or f"broll_{payload.main_clip_id}_{payload.b_roll_asset_id}"

    digest_seed = (
        f"{main_rec.content_sha256}:{broll_rec.content_sha256}:broll:"
        f"{payload.start_offset_us}:{dur}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    broll_layer_rec = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=dur,
        parent_asset_ids=(main_rec.asset_id, broll_rec.asset_id),
        provenance={
            "action": "b_roll_overlay",
            "main_clip_id": main_rec.asset_id,
            "b_roll_asset_id": broll_rec.asset_id,
            "track_id": payload.track_id,
            "start_offset_us": payload.start_offset_us,
            "processor": "nagar.timeline.broll.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, broll_layer_rec]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "main_clip_id": main_rec.asset_id,
            "b_roll_asset_id": broll_rec.asset_id,
            "track_id": payload.track_id,
            "start_offset_us": payload.start_offset_us,
            "duration_us": dur,
            "content_sha256": broll_layer_rec.content_sha256,
        },
    )


def _retime_to_music(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for timeline.retime_to_music."""
    payload = RetimeToMusicInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"timeline.retime_to_music references unknown audio asset: {payload.audio_asset_id!r}"
        )

    beat_duration_us = int((60.0 / payload.tempo_bpm) * 1_000_000)
    cut_span_us = beat_duration_us * payload.beats_per_cut

    cuts: list[dict[str, object]] = []
    current_time_us = 0
    for idx, clip_id in enumerate(payload.clip_asset_ids):
        if clip_id not in known:
            raise CommandValidationError(
                f"timeline.retime_to_music references unknown clip: {clip_id!r}"
            )
        cuts.append(
            {
                "index": idx,
                "clip_id": clip_id,
                "start_us": current_time_us,
                "end_us": current_time_us + cut_span_us,
                "duration_us": cut_span_us,
            }
        )
        current_time_us += cut_span_us

    return OperationOutcome(
        project,
        context.history,
        {
            "audio_asset_id": payload.audio_asset_id,
            "tempo_bpm": payload.tempo_bpm,
            "beats_per_cut": payload.beats_per_cut,
            "total_cuts": len(cuts),
            "total_duration_us": current_time_us,
            "cut_plan": cuts,
        },
    )


def register_edit_operations(registry: CapabilityRegistry) -> None:
    """Register all timeline editing operations with the capability registry."""
    registry.register_operation(
        DOMAIN,
        "trim",
        OperationSpec(
            operation_id=OPERATION_TRIM,
            description="Non-destructive in/out trimming of video or audio clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=TrimInput,
            handler=_trim,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "ripple_delete",
        OperationSpec(
            operation_id=OPERATION_RIPPLE_DELETE,
            description="Delete a span of time and collapse gap on track (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RippleDeleteInput,
            handler=_ripple_delete,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "insert_gap",
        OperationSpec(
            operation_id=OPERATION_INSERT_GAP,
            description="Insert a time gap and shift subsequent timeline clips (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=InsertGapInput,
            handler=_insert_gap,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "speed_ramp",
        OperationSpec(
            operation_id=OPERATION_SPEED_RAMP,
            description="Change clip playback speed with optional audio pitch lock (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=SpeedRampInput,
            handler=_speed_ramp,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "reverse_segment",
        OperationSpec(
            operation_id=OPERATION_REVERSE_SEGMENT,
            description="Derive time-reversed clip playback asset (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ReverseSegmentInput,
            handler=_reverse_segment,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "freeze_frame",
        OperationSpec(
            operation_id=OPERATION_FREEZE_FRAME,
            description="Hold a single frame at playhead for specified duration (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=FreezeFrameInput,
            handler=_freeze_frame,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "attach_b_roll",
        OperationSpec(
            operation_id=OPERATION_ATTACH_B_ROLL,
            description="Attach secondary visual B-roll overlay on timeline (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AttachBRollInput,
            handler=_attach_b_roll,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "retime_to_music",
        OperationSpec(
            operation_id=OPERATION_RETIME_TO_MUSIC,
            description="Calculate timeline cuts snapped to music tempo beats (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RetimeToMusicInput,
            handler=_retime_to_music,
            required_packs=(EDIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )


def build_edit_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with timeline edit and Wave 1 operations."""
    registry = build_wave1_registry()
    register_edit_operations(registry)
    return registry
