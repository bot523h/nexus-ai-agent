"""Pure operations for the ``nexus.color.delivery`` pack (Wave 7 substrate).

This module implements the Nagar Command Bus operations for color grading, proxy generation,
master rendering, and standard OpenTimelineIO (OTIO v1) timeline export:
* ``color.apply_lut`` (Level B, REVERSIBLE): 3D LUT look application with intensity blending.
* ``color.adjust_exposure`` (Level B, REVERSIBLE): EV exposure and Kelvin white-balance adjustments.
* ``color.auto_balance`` (Level B, REVERSIBLE): Automated histogram balancing and skin-tone preservation.
* ``delivery.make_proxy_480p`` (Level A, IMMEDIATE): Low-resolution proxy asset derivation.
* ``delivery.export_otio`` (Level B, REVERSIBLE): OpenTimelineIO v1 interchange JSON export.
* ``delivery.render_master_4k`` (Level C, CONFIRMATION): Master 4K encode with color management.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from nexus_ai_agent.creative.packs.delivery.models import (
    DELIVERY_PACKAGE_ID,
    DOMAIN_COLOR,
    DOMAIN_DELIVERY,
    OPERATION_ADJUST_EXPOSURE,
    OPERATION_APPLY_LUT,
    OPERATION_AUTO_BALANCE,
    OPERATION_EXPORT_OTIO,
    OPERATION_MAKE_PROXY,
    OPERATION_MATCH_SHOT,
    OPERATION_RENDER_MASTER_4K,
    AdjustExposureInput,
    ApplyLutInput,
    AutoBalanceInput,
    ExportOtioInput,
    MakeProxyInput,
    MatchShotInput,
    OtioClip,
    OtioTrack,
    RationalTime,
    RenderMaster4KInput,
    TimeRange,
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


def _apply_lut(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.apply_lut."""
    payload = ApplyLutInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.apply_lut references unknown clip asset: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    if clip_rec.media_kind != "video":
        raise CommandValidationError(
            f"color.apply_lut requires video asset, got: {clip_rec.media_kind!r}"
        )

    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_lut"
    digest_seed = f"{clip_rec.content_sha256}:lut:{payload.lut_name}:{payload.intensity}:{payload.color_space}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    graded_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "lut_name": payload.lut_name,
            "intensity": payload.intensity,
            "color_space": payload.color_space,
            "grade_node": "3d_lut",
            "processor": "nagar.color.lut.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, graded_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "lut_name": payload.lut_name,
            "intensity": payload.intensity,
            "color_space": payload.color_space,
            "content_sha256": graded_record.content_sha256,
        },
    )


def _adjust_exposure(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.adjust_exposure."""
    payload = AdjustExposureInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.adjust_exposure references unknown clip asset: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_exp"
    digest_seed = f"{clip_rec.content_sha256}:exp:{payload.exposure_ev}:temp:{payload.temperature_k}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    graded_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "exposure_ev": payload.exposure_ev,
            "contrast": payload.contrast,
            "temperature_k": payload.temperature_k,
            "tint": payload.tint,
            "processor": "nagar.color.exposure.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, graded_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "exposure_ev": payload.exposure_ev,
            "temperature_k": payload.temperature_k,
            "content_sha256": graded_record.content_sha256,
        },
    )


def _auto_balance(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.auto_balance."""
    payload = AutoBalanceInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.auto_balance references unknown clip asset: {payload.clip_asset_id!r}"
        )

    clip_rec = known[payload.clip_asset_id]
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_autobal"
    digest_seed = f"{clip_rec.content_sha256}:autobalance:skin:{payload.preserve_skin_tones}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    graded_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "auto_balance": True,
            "preserve_skin_tones": payload.preserve_skin_tones,
            "processor": "nagar.color.autobalance.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, graded_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "content_sha256": graded_record.content_sha256,
        },
    )


def _match_shot(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.match_shot."""
    payload = MatchShotInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.source_clip_id not in known:
        raise CommandValidationError(
            f"color.match_shot references unknown source clip: {payload.source_clip_id!r}"
        )
    if payload.reference_clip_id not in known:
        raise CommandValidationError(
            f"color.match_shot references unknown reference clip: {payload.reference_clip_id!r}"
        )

    src_rec = known[payload.source_clip_id]
    ref_rec = known[payload.reference_clip_id]
    output_id = payload.output_asset_id or f"{payload.source_clip_id}_matched"
    digest_seed = f"{src_rec.content_sha256}:{ref_rec.content_sha256}:matchshot"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    matched_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=src_rec.duration_us,
        parent_asset_ids=(src_rec.asset_id, ref_rec.asset_id),
        provenance={
            "source_clip_id": src_rec.asset_id,
            "reference_clip_id": ref_rec.asset_id,
            "match_luminance": payload.match_luminance,
            "match_chrominance": payload.match_chrominance,
            "processor": "nagar.color.matchshot.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, matched_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": src_rec.asset_id,
            "reference_clip_id": ref_rec.asset_id,
            "content_sha256": matched_record.content_sha256,
        },
    )


def _make_proxy(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for delivery.make_proxy_480p."""
    payload = MakeProxyInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.video_asset_id not in known:
        raise CommandValidationError(
            f"delivery.make_proxy_480p references unknown video asset: {payload.video_asset_id!r}"
        )

    video_rec = known[payload.video_asset_id]
    output_id = payload.output_asset_id or f"{payload.video_asset_id}_proxy480p"
    digest_seed = f"{video_rec.content_sha256}:proxy:{payload.resolution}:crf:{payload.crf}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    proxy_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=video_rec.duration_us,
        parent_asset_ids=(video_rec.asset_id,),
        provenance={
            "is_proxy": True,
            "source_asset_id": video_rec.asset_id,
            "resolution": payload.resolution,
            "crf": payload.crf,
            "codec": "h264",
            "processor": "nagar.delivery.proxy.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, proxy_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": video_rec.asset_id,
            "resolution": payload.resolution,
            "is_proxy": True,
            "content_sha256": proxy_record.content_sha256,
        },
    )


def _export_otio(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for delivery.export_otio.

    Serializes project timeline, layers, and color metadata into the canonical
    OpenTimelineIO (OTIO v1) interchange format.
    """
    payload = ExportOtioInput.model_validate(context.input_data)
    rate = payload.frame_rate

    video_clips: list[OtioClip] = []
    audio_clips: list[OtioClip] = []

    for asset in project.assets:
        duration_us = asset.duration_us or 1_000_000
        duration_frames = max(1, int((duration_us / 1_000_000.0) * rate))
        time_range = TimeRange(
            start_time=RationalTime(value=0, rate=rate),
            duration=RationalTime(value=duration_frames, rate=rate),
        )

        if asset.media_kind == "video":
            video_clips.append(
                OtioClip(
                    name=asset.asset_id,
                    source_range=time_range,
                    media_url=f"asset:{asset.asset_id}",
                    metadata={"provenance": asset.provenance},
                )
            )
        elif asset.media_kind == "audio":
            audio_clips.append(
                OtioClip(
                    name=asset.asset_id,
                    source_range=time_range,
                    media_url=f"asset:{asset.asset_id}",
                    metadata={"provenance": asset.provenance},
                )
            )

    video_track = OtioTrack(name="Video 1", kind="Video", children=video_clips)
    audio_track = OtioTrack(name="Audio 1", kind="Audio", children=audio_clips)

    otio_doc: dict[str, Any] = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": project.name or project.project_id,
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [
                video_track.model_dump(mode="json"),
                audio_track.model_dump(mode="json"),
            ],
        },
        "metadata": {
            "nagar_project_id": project.project_id,
            "nagar_state_revision": project.state_revision,
            "export_standard": "OpenTimelineIO-v1.0",
        },
    }

    otio_json = json.dumps(otio_doc, indent=2, sort_keys=True)
    derived_sha256 = hashlib.sha256(otio_json.encode("utf-8")).hexdigest()
    output_id = payload.output_asset_id or f"{project.project_id}_timeline.otio"

    otio_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=project.timeline.duration_us,
        parent_asset_ids=tuple(a.asset_id for a in project.assets),
        provenance={
            "format": "otio",
            "frame_rate": rate,
            "video_clip_count": len(video_clips),
            "audio_clip_count": len(audio_clips),
            "generator": "nagar.delivery.otio.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, otio_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "format": "otio",
            "frame_rate": rate,
            "total_video_clips": len(video_clips),
            "total_audio_clips": len(audio_clips),
            "otio_json": otio_json,
            "content_sha256": otio_record.content_sha256,
        },
    )


def _render_master_4k(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMATION) handler for delivery.render_master_4k."""
    payload = RenderMaster4KInput.model_validate(context.input_data)
    if not payload.confirmed:
        raise CommandValidationError(
            "delivery.render_master_4k requires explicit user confirmation (confirmed=true) in Level C"
        )

    output_id = payload.output_asset_id or f"master_{uuid.uuid4().hex[:12]}"
    composite_hashes = ":".join(sorted(a.content_sha256 for a in project.assets))
    render_spec = f"{composite_hashes}:{payload.width}x{payload.height}:{payload.codec}:{payload.color_space}:{payload.target_lufs}"
    derived_sha256 = hashlib.sha256(render_spec.encode("utf-8")).hexdigest()

    master_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=project.timeline.duration_us,
        parent_asset_ids=tuple(a.asset_id for a in project.assets if a.media_kind in ("video", "audio")),
        provenance={
            "is_master": True,
            "width": payload.width,
            "height": payload.height,
            "codec": payload.codec,
            "container": payload.container,
            "color_space": payload.color_space,
            "target_lufs": payload.target_lufs,
            "render_engine": "nagar.delivery.ffmpeg.native.v1",
        },
    )

    new_project = project.model_copy(
        update={"assets": [*project.assets, master_record]}
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "width": payload.width,
            "height": payload.height,
            "codec": payload.codec,
            "container": payload.container,
            "color_space": payload.color_space,
            "target_lufs": payload.target_lufs,
            "content_sha256": master_record.content_sha256,
        },
    )


def register_delivery_operations(registry: CapabilityRegistry) -> None:
    """Register all color and delivery operations with the capability registry."""
    # Color operations (Domain: color)
    registry.register_operation(
        DOMAIN_COLOR,
        "apply_lut",
        OperationSpec(
            operation_id=OPERATION_APPLY_LUT,
            description="Apply 3D LUT look transform to video clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ApplyLutInput,
            handler=_apply_lut,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN_COLOR,
        "adjust_exposure",
        OperationSpec(
            operation_id=OPERATION_ADJUST_EXPOSURE,
            description="Adjust exposure EV and temperature balance (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AdjustExposureInput,
            handler=_adjust_exposure,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN_COLOR,
        "auto_balance",
        OperationSpec(
            operation_id=OPERATION_AUTO_BALANCE,
            description="Automated white balance and histogram normalization (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AutoBalanceInput,
            handler=_auto_balance,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )

    registry.register_operation(
        DOMAIN_COLOR,
        "match_shot",
        OperationSpec(
            operation_id=OPERATION_MATCH_SHOT,
            description="Match color grade from reference clip to source clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MatchShotInput,
            handler=_match_shot,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )

    # Delivery operations (Domain: delivery)
    registry.register_operation(
        DOMAIN_DELIVERY,
        "make_proxy_480p",
        OperationSpec(
            operation_id=OPERATION_MAKE_PROXY,
            description="Generate lightweight 480p preview proxy (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=MakeProxyInput,
            handler=_make_proxy,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN_DELIVERY,
        "export_otio",
        OperationSpec(
            operation_id=OPERATION_EXPORT_OTIO,
            description="Export timeline to OpenTimelineIO (OTIO v1) schema (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ExportOtioInput,
            handler=_export_otio,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN_DELIVERY,
        "render_master_4k",
        OperationSpec(
            operation_id=OPERATION_RENDER_MASTER_4K,
            description="Render master 4K broadcast deliverable with confirmation (Level C).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=RenderMaster4KInput,
            handler=_render_master_4k,
            required_packs=(DELIVERY_PACKAGE_ID,),
            deterministic=True,
        ),
    )


def build_delivery_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with delivery and color operations."""
    registry = CapabilityRegistry()
    register_delivery_operations(registry)
    return registry
