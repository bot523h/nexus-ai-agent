"""Pure operations for the ``nexus.vision.scene`` pack (TDD §1.4).

Ten Nagar Command Bus operations for scene understanding and semantic editing.
Handlers are pure state reducers over :class:`Project`; segmentation, tracking
and inpainting *plans* are derived deterministically here and executed later by
the apply lane / native adapters (same contract style as ``motion.warp`` and
``portrait.mask_hair``).

* ``scene.segment_subject``       (A) semantic mask → MaskRef image asset
* ``scene.remove_object``         (B) temporal-inpaint plan + confidence
* ``scene.replace_sky``           (B) sky segmentation composite layer
* ``scene.remove_background``     (B) SubjectMask alpha layer
* ``scene.track_object``          (A) ObjectTrack evidence
* ``scene.track_face``            (A) FaceTrackSet evidence
* ``scene.detect_shot_boundaries``(A) ShotBoundarySet evidence
* ``scene.find_subject_moment``   (A) pinned CandidateMomentSet
* ``scene.remove_logo``           (C) legal-policy logo removal (confirmation)
* ``scene.auto_reframe_subject``  (B) reframe TransformCurve plan

Shared primitives (mask/track/curve models, digest seeds, deterministic
sampling) live in :mod:`nexus_ai_agent.creative.packs.vision_common` — the
duplication-vs-abstraction decision is recorded in that module and in the
mission report.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.scene.models import (
    ASPECT_RATIOS,
    DOMAIN,
    FILL_POLICIES,
    LOGO_POLICIES,
    OPERATION_AUTO_REFRAME_SUBJECT,
    OPERATION_DETECT_SHOT_BOUNDARIES,
    OPERATION_FIND_SUBJECT_MOMENT,
    OPERATION_REMOVE_BACKGROUND,
    OPERATION_REMOVE_LOGO,
    OPERATION_REMOVE_OBJECT,
    OPERATION_REPLACE_SKY,
    OPERATION_SEGMENT_SUBJECT,
    OPERATION_TRACK_FACE,
    OPERATION_TRACK_OBJECT,
    SCENE_PACKAGE_ID,
    AutoReframeSubjectInput,
    DetectShotBoundariesInput,
    FindSubjectMomentInput,
    RemoveBackgroundInput,
    RemoveLogoInput,
    RemoveObjectInput,
    ReplaceSkyInput,
    SegmentSubjectInput,
    TrackFaceInput,
    TrackObjectInput,
)
from nexus_ai_agent.creative.packs.vision_common import (
    MASK_RESOLUTION,
    CandidateMoment,
    CandidateMomentSet,
    MaskEvidence,
    ShotBoundary,
    ShotBoundarySet,
    derive_asset,
    digest_seed_hash,
    face_track_from_asset,
    frame_at,
    object_track_from_asset,
    sample_offsets_us,
    transform_curve_from_offsets,
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


def _require_video(known: dict[str, AssetRecord], asset_id: str, operation: str) -> AssetRecord:
    record = known.get(asset_id)
    if record is None:
        raise CommandValidationError(f"{operation} references unknown clip asset: {asset_id!r}")
    if record.media_kind != "video":
        raise CommandValidationError(
            f"{operation} requires video asset, got: {record.media_kind!r}"
        )
    return record


def _require_mask(known: dict[str, AssetRecord], asset_id: str, operation: str) -> AssetRecord:
    record = known.get(asset_id)
    if record is None:
        raise CommandValidationError(f"{operation} references unknown mask asset: {asset_id!r}")
    if record.media_kind != "image":
        raise CommandValidationError(
            f"{operation} requires an image mask asset, got: {record.media_kind!r}"
        )
    return record


def _segment_subject(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for scene.segment_subject → MaskRef asset."""
    payload = SegmentSubjectInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_SEGMENT_SUBJECT)
    if payload.start_us >= clip.duration_us > 0:
        raise CommandValidationError(
            f"{OPERATION_SEGMENT_SUBJECT} start_us ({payload.start_us}) is outside "
            f"the clip duration ({clip.duration_us}µs)"
        )

    width, height = MASK_RESOLUTION
    coverage_seed = digest_seed_hash(
        clip.content_sha256, "segment_subject", payload.semantic_query, payload.start_us
    )
    coverage = round(0.1 + (int(coverage_seed[7:13], 16) % 500) / 1000.0, 6)  # 0.1..0.6
    mask_id = f"{clip.asset_id}_mask"
    mask_asset = derive_asset(
        asset_id=mask_id,
        media_kind="image",
        seed_parts=(
            clip.content_sha256,
            "segment_subject",
            payload.semantic_query,
            payload.start_us,
            payload.duration_us,
        ),
        duration_us=0,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "subject_segmentation",
            "source_clip_id": clip.asset_id,
            "semantic_query": payload.semantic_query,
            "start_us": payload.start_us,
            "duration_us": payload.duration_us,
            "coordinate_space": "normalized",
            "processor": "nagar.scene.segment_subject.v1",
        },
    )
    evidence = MaskEvidence(
        mask_id=mask_id,
        source_asset_id=clip.asset_id,
        width=width,
        height=height,
        coverage_ratio=coverage,
        confidence=0.88,
    )
    new_project = project.model_copy(update={"assets": [*project.assets, mask_asset]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "mask": evidence.model_dump(mode="json"),
            "mask_id": mask_id,
            "semantic_query": payload.semantic_query,
            "content_sha256": mask_asset.content_sha256,
        },
    )


def _remove_object(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for scene.remove_object (temporal inpaint plan)."""
    payload = RemoveObjectInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_REMOVE_OBJECT)

    fill = FILL_POLICIES[payload.fill_policy]
    start_us = payload.start_us
    span_us = payload.duration_us if payload.duration_us is not None else clip.duration_us
    if clip.duration_us > 0 and start_us + span_us > clip.duration_us:
        raise CommandValidationError(
            f"{OPERATION_REMOVE_OBJECT} range {start_us}..{start_us + span_us}µs exceeds "
            f"the clip duration ({clip.duration_us}µs)"
        )

    # Deterministic plan confidence: seeded detector agreement, gated by the
    # caller's minimum — below-threshold plans must surface as needs_review.
    agreement = round(
        0.85 + (int(digest_seed_hash(clip.content_sha256, payload.object_track_id)[7:13], 16) % 150)
        / 1000.0,
        6,
    )
    confidence = round(min(agreement, 1.0), 6)
    needs_review = confidence < payload.minimum_confidence

    output_id = payload.output_asset_id or f"{clip.asset_id}_objremoved"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "remove_object",
            payload.object_track_id,
            payload.fill_policy,
            payload.preserve_camera_motion,
            start_us,
            span_us,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "object_remove",
            "source_clip_id": clip.asset_id,
            "object_track_id": payload.object_track_id,
            "fill_policy": payload.fill_policy,
            "preserve_camera_motion": payload.preserve_camera_motion,
            "start_us": start_us,
            "duration_us": span_us,
            "feather_px": fill["feather_px"],
            "temporal_window_frames": fill["temporal_window_frames"],
            "needs_review": needs_review,
            "processor": "nagar.scene.remove_object.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "object_track_id": payload.object_track_id,
            "fill_policy": payload.fill_policy,
            "start_us": start_us,
            "duration_us": span_us,
            "confidence": confidence,
            "needs_review": needs_review,
            "inpaint_plan": {
                "feather_px": fill["feather_px"],
                "temporal_window_frames": fill["temporal_window_frames"],
                "preserve_camera_motion": payload.preserve_camera_motion,
            },
            "content_sha256": derived.content_sha256,
        },
    )


def _replace_sky(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for scene.replace_sky."""
    payload = ReplaceSkyInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_REPLACE_SKY)
    sky = known.get(payload.sky_asset_id)
    if sky is None:
        raise CommandValidationError(
            f"{OPERATION_REPLACE_SKY} references unknown sky asset: {payload.sky_asset_id!r}"
        )
    if sky.media_kind not in ("image", "video"):
        raise CommandValidationError(
            f"{OPERATION_REPLACE_SKY} requires an image or video sky asset, got: {sky.media_kind!r}"
        )

    output_id = payload.output_asset_id or f"{clip.asset_id}_sky"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "replace_sky",
            sky.content_sha256,
            payload.horizon_policy,
            payload.horizon_y_norm,
            payload.feather_px,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, sky.asset_id),
        provenance={
            "effect": "sky_replace",
            "source_clip_id": clip.asset_id,
            "sky_asset_id": sky.asset_id,
            "horizon_policy": payload.horizon_policy,
            "horizon_y_norm": payload.horizon_y_norm,
            "feather_px": payload.feather_px,
            "processor": "nagar.scene.replace_sky.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "sky_asset_id": sky.asset_id,
            "horizon_policy": payload.horizon_policy,
            "horizon_y_norm": payload.horizon_y_norm,
            "content_sha256": derived.content_sha256,
        },
    )


def _remove_background(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for scene.remove_background (AlphaLayerRef)."""
    payload = RemoveBackgroundInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_REMOVE_BACKGROUND)
    mask = _require_mask(known, payload.mask_asset_id, OPERATION_REMOVE_BACKGROUND)

    output_id = payload.output_asset_id or f"{clip.asset_id}_alpha"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "remove_background",
            mask.content_sha256,
            payload.alpha_policy,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, mask.asset_id),
        provenance={
            "effect": "background_remove",
            "source_clip_id": clip.asset_id,
            "subject_mask_asset_id": mask.asset_id,
            "alpha_policy": payload.alpha_policy,
            "alpha_layer": True,
            "processor": "nagar.scene.remove_background.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "subject_mask_asset_id": mask.asset_id,
            "alpha_policy": payload.alpha_policy,
            "content_sha256": derived.content_sha256,
        },
    )


def _track_object(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for scene.track_object → ObjectTrack."""
    payload = TrackObjectInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_TRACK_OBJECT)

    track = object_track_from_asset(
        source_asset_id=clip.asset_id,
        content_sha256=clip.content_sha256,
        duration_us=clip.duration_us,
        policy=payload.sample_policy,
        semantic_seed=payload.semantic_seed,
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "object_track": track.model_dump(mode="json"),
            "track_id": track.track_id,
            "semantic_seed": payload.semantic_seed,
            "sample_count": len(track.samples),
            "sample_policy": payload.sample_policy,
            "confidence": track.confidence,
            "detector": track.detector,
            "model_digest": track.model_digest,
        },
    )


def _track_face(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for scene.track_face → FaceTrackSet."""
    payload = TrackFaceInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_TRACK_FACE)

    track = face_track_from_asset(
        source_asset_id=clip.asset_id,
        content_sha256=clip.content_sha256,
        duration_us=clip.duration_us,
        policy=payload.sample_policy,
        landmark_count=payload.landmark_count,
        subject_id=payload.subject_id,
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "face_track": track.model_dump(mode="json"),
            "track_id": track.track_id,
            "subject_id": track.subject_id,
            "sample_count": len(track.samples),
            "sample_policy": payload.sample_policy,
            "confidence": track.confidence,
            "detector": track.detector,
            "model_digest": track.model_digest,
        },
    )


def _detect_shot_boundaries(
    project: Project, context: OperationContext
) -> OperationOutcome:
    """Level A (READ) handler for scene.detect_shot_boundaries."""
    payload = DetectShotBoundariesInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_DETECT_SHOT_BOUNDARIES)

    boundaries: list[ShotBoundary] = []
    if clip.duration_us > 0:
        # Deterministic contract detector: candidate cuts every 2s, kept when
        # the seeded cut score clears the caller's threshold (same inputs →
        # same boundary set, per the deterministic=True contract).
        for index, offset in enumerate(range(2_000_000, clip.duration_us, 2_000_000)):
            score = round(
                0.4
                + (int(digest_seed_hash(clip.content_sha256, "cut", index)[7:13], 16) % 600)
                / 1000.0,
                6,
            )
            if score >= payload.threshold:
                boundaries.append(
                    ShotBoundary(
                        timecode_us=offset,
                        frame_number=frame_at(offset),
                        cut_score=score,
                        kind="cut" if score >= 0.8 else "gradual",
                    )
                )
    result = ShotBoundarySet(
        source_asset_id=clip.asset_id,
        threshold=payload.threshold,
        boundaries=tuple(boundaries),
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "shot_boundaries": result.model_dump(mode="json"),
            "boundary_count": len(boundaries),
            "threshold": payload.threshold,
            "detector": result.detector,
            "model_digest": result.model_digest,
        },
    )


def _find_subject_moment(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for scene.find_subject_moment (TDD scenario pin)."""
    payload = FindSubjectMomentInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_FIND_SUBJECT_MOMENT)

    offsets = sample_offsets_us(clip.duration_us, payload.sample_policy)
    subject_token = f"{payload.subject_kind}:{payload.subject_id}"
    candidates: list[CandidateMoment] = []
    for index, offset in enumerate(offsets):
        confidence = round(
            0.75
            + (
                int(
                    digest_seed_hash(clip.content_sha256, subject_token, index)[7:13],
                    16,
                )
                % 250
            )
            / 1000.0,
            6,
        )
        if confidence < payload.minimum_confidence:
            continue
        evidence_half = min(60_000, max(1, clip.duration_us // 2))
        evidence_start = max(0, offset - evidence_half)
        evidence_end = min(max(1, clip.duration_us), offset + evidence_half)
        candidates.append(
            CandidateMoment(
                candidate_id=f"moment_{index:03d}",
                timecode_us=offset,
                frame_number=frame_at(offset),
                confidence=confidence,
                evidence_start_us=evidence_start,
                evidence_end_us=max(evidence_start + 1, evidence_end),
                subject_id=payload.subject_id,
            )
        )
    if payload.event == "first_visible":
        chosen = candidates[:1]
    elif payload.event == "last_visible":
        chosen = candidates[-1:]
    else:  # best_visible
        chosen = sorted(candidates, key=lambda c: (-c.confidence, c.timecode_us))[:1]

    result = CandidateMomentSet(
        source_asset_id=clip.asset_id,
        event=payload.event,
        minimum_confidence=payload.minimum_confidence,
        candidates=tuple(chosen),
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "moments": result.model_dump(mode="json"),
            "candidate_count": len(chosen),
            "event": payload.event,
            "minimum_confidence": payload.minimum_confidence,
            "detector": result.detector,
            "model_digest": result.model_digest,
        },
    )


def _remove_logo(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMATION) handler for scene.remove_logo (legal surface)."""
    payload = RemoveLogoInput.model_validate(context.input_data)
    if not payload.confirmed:
        raise CommandValidationError(
            "scene.remove_logo requires explicit user confirmation (confirmed=true) in Level C"
        )
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_REMOVE_LOGO)
    mask = _require_mask(known, payload.mask_asset_id, OPERATION_REMOVE_LOGO)

    policy = LOGO_POLICIES[payload.legal_policy]
    output_id = payload.output_asset_id or f"{clip.asset_id}_nologo"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "remove_logo",
            mask.content_sha256,
            payload.legal_policy,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, mask.asset_id),
        provenance={
            "effect": "logo_remove",
            "source_clip_id": clip.asset_id,
            "logo_mask_asset_id": mask.asset_id,
            "legal_policy": payload.legal_policy,
            "fill_radius_px": policy["fill_radius_px"],
            "legal_surface": True,
            "processor": "nagar.scene.remove_logo.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "logo_mask_asset_id": mask.asset_id,
            "legal_policy": payload.legal_policy,
            "fill_radius_px": policy["fill_radius_px"],
            "content_sha256": derived.content_sha256,
        },
    )


def _auto_reframe_subject(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for scene.auto_reframe_subject → TransformCurve."""
    payload = AutoReframeSubjectInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_AUTO_REFRAME_SUBJECT)

    width_ratio, height_ratio = ASPECT_RATIOS[payload.aspect]
    offsets = sample_offsets_us(clip.duration_us, "standard")
    keyframes: list[tuple[int, float, float, float, float]] = []
    for index, offset in enumerate(offsets):
        unit = (
            int(digest_seed_hash(clip.content_sha256, payload.object_track_id, index)[7:13], 16)
            % 2000
        ) / 1000.0  # 0..2 → -1..1 center drift
        drift = unit - 1.0
        # Keep the subject inside the safe area of the target frame.
        max_shift = 0.5 - payload.safe_area
        dx = max(-max_shift, min(max_shift, -drift * 0.25))
        zoom = 1.0 + (0.25 if height_ratio > width_ratio else 0.0)
        keyframes.append((offset, round(dx, 6), 0.0, zoom, 0.0))

    curve = transform_curve_from_offsets(
        curve_id=f"reframe-{digest_seed_hash(clip.content_sha256, payload.aspect)[7:19]}",
        source_asset_id=clip.asset_id,
        policy=f"aspect:{payload.aspect}:safe:{payload.safe_area}",
        duration_us=clip.duration_us,
        offsets_xy_scale_rot=keyframes,
        confidence=0.87,
    )
    output_id = payload.output_asset_id or f"{clip.asset_id}_reframe"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "auto_reframe_subject",
            payload.object_track_id,
            payload.aspect,
            payload.safe_area,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "auto_reframe",
            "source_clip_id": clip.asset_id,
            "object_track_id": payload.object_track_id,
            "aspect": payload.aspect,
            "safe_area": payload.safe_area,
            "curve_id": curve.curve_id,
            "processor": "nagar.scene.auto_reframe_subject.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "transform_curve": curve.model_dump(mode="json"),
            "curve_id": curve.curve_id,
            "aspect": payload.aspect,
            "width_ratio": width_ratio,
            "height_ratio": height_ratio,
            "content_sha256": derived.content_sha256,
        },
    )


def register_scene_operations(registry: CapabilityRegistry) -> None:
    """Register all scene operations with the capability registry."""
    specs: tuple[OperationSpec, ...] = (
        OperationSpec(
            operation_id=OPERATION_SEGMENT_SUBJECT,
            description="Semantic subject segmentation into a MaskRef asset (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=SegmentSubjectInput,
            handler=_segment_subject,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_REMOVE_OBJECT,
            description="Tracked object removal with temporal inpaint plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RemoveObjectInput,
            handler=_remove_object,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_REPLACE_SKY,
            description="Sky segmentation replacement with horizon policy (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ReplaceSkyInput,
            handler=_replace_sky,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_REMOVE_BACKGROUND,
            description="SubjectMask alpha extraction (AlphaLayerRef) (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RemoveBackgroundInput,
            handler=_remove_background,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_TRACK_OBJECT,
            description="Detector + flow object tracking into an ObjectTrack (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=TrackObjectInput,
            handler=_track_object,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_TRACK_FACE,
            description="Face detection/tracking into a FaceTrackSet (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=TrackFaceInput,
            handler=_track_face,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_DETECT_SHOT_BOUNDARIES,
            description="Histogram/embedding shot boundary detection (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=DetectShotBoundariesInput,
            handler=_detect_shot_boundaries,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_FIND_SUBJECT_MOMENT,
            description="Pin subject-visible candidate moments with evidence (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=FindSubjectMomentInput,
            handler=_find_subject_moment,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_REMOVE_LOGO,
            description="Legal-policy logo removal over a LogoMask (Level C).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=RemoveLogoInput,
            handler=_remove_logo,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_AUTO_REFRAME_SUBJECT,
            description="Subject-tracking reframe TransformCurve plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AutoReframeSubjectInput,
            handler=_auto_reframe_subject,
            required_packs=(SCENE_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    for spec in specs:
        capability = spec.operation_id.split(".", 1)[1]
        registry.register_operation(DOMAIN, capability, spec)


def build_scene_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with the scene operations."""
    registry = CapabilityRegistry()
    register_scene_operations(registry)
    return registry
