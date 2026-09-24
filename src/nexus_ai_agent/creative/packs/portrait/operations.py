"""Pure operations for the ``nexus.vision.portrait`` pack (TDD §1.3).

Ten Nagar Command Bus operations for portrait work.  Every handler is a pure
state reducer: validate → resolve assets → derive content-addressed evidence /
derived assets → return :class:`OperationOutcome`.  Source assets are never
mutated (TDD §0.6); a failing handler leaves the central state untouched.

* ``portrait.detect_landmarks``  (A) FaceTrackSet contract evidence
* ``portrait.smooth_skin``      (B) derived graded clip + effect provenance
* ``portrait.retouch_blemish``  (B) local inpaint plan over a FaceMask
* ``portrait.relight_face``     (B) typed LightModel relight layer
* ``portrait.whiten_teeth``     (B) TeethMask color lift
* ``portrait.correct_gaze``     (C) identity-sensitive warp (explicit confirmation)
* ``portrait.enhance_eyes``     (B) clarity / red-eye layer
* ``portrait.mask_hair``        (A) derived hair-mask image asset (MaskRef)
* ``portrait.background_blur``  (B) SubjectMask depth blur
* ``portrait.stabilize_face``   (B) TransformCurve plan + smoothed clip

Pixel work happens in the apply lane / native adapters; these handlers emit the
deterministic plans and state those lanes compile from (same contract style as
``motion.warp``'s mesh plan and ``audio.remove_vocal``'s stem table).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.portrait.models import (
    BLEMISH_POLICIES,
    BLUR_PROFILES,
    DOMAIN,
    EDGE_FEATHER_PX,
    OPERATION_BACKGROUND_BLUR,
    OPERATION_CORRECT_GAZE,
    OPERATION_DETECT_LANDMARKS,
    OPERATION_ENHANCE_EYES,
    OPERATION_MASK_HAIR,
    OPERATION_RELIGHT_FACE,
    OPERATION_RETOUCH_BLEMISH,
    OPERATION_SMOOTH_SKIN,
    OPERATION_STABILIZE_FACE,
    OPERATION_WHITEN_TEETH,
    PORTRAIT_PACKAGE_ID,
    BackgroundBlurInput,
    CorrectGazeInput,
    DetectLandmarksInput,
    EnhanceEyesInput,
    MaskHairInput,
    RelightFaceInput,
    RetouchBlemishInput,
    SmoothSkinInput,
    StabilizeFaceInput,
    WhitenTeethInput,
)
from nexus_ai_agent.creative.packs.vision_common import (
    MASK_RESOLUTION,
    MaskEvidence,
    derive_asset,
    digest_seed_hash,
    face_track_from_asset,
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


def _detect_landmarks(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for portrait.detect_landmarks → FaceTrackSet."""
    payload = DetectLandmarksInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_DETECT_LANDMARKS)

    track = face_track_from_asset(
        source_asset_id=clip.asset_id,
        content_sha256=clip.content_sha256,
        duration_us=clip.duration_us,
        policy=payload.sample_policy,
        landmark_count=payload.landmark_count,
        subject_id=payload.subject_id,
        max_faces=payload.max_faces,
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "face_track": track.model_dump(mode="json"),
            "track_id": track.track_id,
            "subject_id": track.subject_id,
            "face_count": 1 if payload.max_faces >= 1 else 0,
            "sample_count": len(track.samples),
            "sample_policy": payload.sample_policy,
            "confidence": track.confidence,
            "detector": track.detector,
            "model_digest": track.model_digest,
        },
    )


def _smooth_skin(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.smooth_skin."""
    payload = SmoothSkinInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_SMOOTH_SKIN)
    if payload.mask_asset_id is not None:
        _require_mask(known, payload.mask_asset_id, OPERATION_SMOOTH_SKIN)

    output_id = payload.output_asset_id or f"{clip.asset_id}_smoothskin"
    parents = tuple([clip.asset_id] + ([payload.mask_asset_id] if payload.mask_asset_id else []))
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "smooth_skin",
            payload.strength,
            payload.temporal_stability,
            payload.face_track_id or "",
            payload.mask_asset_id or "",
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=parents,
        provenance={
            "effect": "skin_smooth",
            "source_clip_id": clip.asset_id,
            "strength": payload.strength,
            "temporal_stability": payload.temporal_stability,
            "face_track_id": payload.face_track_id,
            "mask_asset_id": payload.mask_asset_id,
            "processor": "nagar.portrait.smooth_skin.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "strength": payload.strength,
            "temporal_stability": payload.temporal_stability,
            "content_sha256": derived.content_sha256,
        },
    )


def _retouch_blemish(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.retouch_blemish."""
    payload = RetouchBlemishInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_RETOUCH_BLEMISH)
    mask = _require_mask(known, payload.mask_asset_id, OPERATION_RETOUCH_BLEMISH)

    policy = BLEMISH_POLICIES[payload.blemish_policy]
    output_id = payload.output_asset_id or f"{clip.asset_id}_blemish"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "retouch_blemish",
            mask.content_sha256,
            payload.blemish_policy,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, mask.asset_id),
        provenance={
            "effect": "blemish_retouch",
            "source_clip_id": clip.asset_id,
            "face_mask_asset_id": mask.asset_id,
            "blemish_policy": payload.blemish_policy,
            "inpaint_radius_px": policy["inpaint_radius_px"],
            "blend": policy["blend"],
            "processor": "nagar.portrait.retouch_blemish.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "face_mask_asset_id": mask.asset_id,
            "inpaint_radius_px": policy["inpaint_radius_px"],
            "blend": policy["blend"],
            "content_sha256": derived.content_sha256,
        },
    )


def _relight_face(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.relight_face."""
    payload = RelightFaceInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_RELIGHT_FACE)

    output_id = payload.output_asset_id or f"{clip.asset_id}_relit"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "relight_face",
            payload.face_track_id,
            payload.light.model_dump(mode="json"),
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "face_relight",
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "light": payload.light.model_dump(mode="json"),
            "processor": "nagar.portrait.relight_face.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "light": payload.light.model_dump(mode="json"),
            "content_sha256": derived.content_sha256,
        },
    )


def _whiten_teeth(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.whiten_teeth."""
    payload = WhitenTeethInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_WHITEN_TEETH)
    mask = _require_mask(known, payload.mask_asset_id, OPERATION_WHITEN_TEETH)

    feather_px = EDGE_FEATHER_PX[payload.edge_quality]
    output_id = payload.output_asset_id or f"{clip.asset_id}_teeth"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "whiten_teeth",
            mask.content_sha256,
            payload.intensity,
            payload.edge_quality,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, mask.asset_id),
        provenance={
            "effect": "teeth_whiten",
            "source_clip_id": clip.asset_id,
            "teeth_mask_asset_id": mask.asset_id,
            "intensity": payload.intensity,
            "feather_px": feather_px,
            "processor": "nagar.portrait.whiten_teeth.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "teeth_mask_asset_id": mask.asset_id,
            "intensity": payload.intensity,
            "feather_px": feather_px,
            "content_sha256": derived.content_sha256,
        },
    )


def _correct_gaze(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMATION) handler for portrait.correct_gaze (identity-sensitive)."""
    payload = CorrectGazeInput.model_validate(context.input_data)
    if not payload.confirmed:
        raise CommandValidationError(
            "portrait.correct_gaze requires explicit user confirmation (confirmed=true) in Level C"
        )
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_CORRECT_GAZE)

    output_id = payload.output_asset_id or f"{clip.asset_id}_gaze"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "correct_gaze",
            payload.face_track_id,
            payload.gaze_target,
            payload.strength,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "gaze_correction",
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "gaze_target": payload.gaze_target,
            "strength": payload.strength,
            "identity_sensitive": True,
            "processor": "nagar.portrait.correct_gaze.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "gaze_target": payload.gaze_target,
            "strength": payload.strength,
            "content_sha256": derived.content_sha256,
        },
    )


def _enhance_eyes(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.enhance_eyes."""
    payload = EnhanceEyesInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_ENHANCE_EYES)

    output_id = payload.output_asset_id or f"{clip.asset_id}_eyes"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "enhance_eyes",
            payload.face_track_id,
            payload.clarity,
            payload.red_eye,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "eye_enhance",
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "clarity": payload.clarity,
            "red_eye": payload.red_eye,
            "processor": "nagar.portrait.enhance_eyes.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, derived]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "clarity": payload.clarity,
            "red_eye": payload.red_eye,
            "content_sha256": derived.content_sha256,
        },
    )


def _mask_hair(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for portrait.mask_hair → MaskRef image asset."""
    payload = MaskHairInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_MASK_HAIR)

    width, height = MASK_RESOLUTION
    coverage = 0.18  # deterministic contract coverage band for hair silhouette
    mask_id = f"{clip.asset_id}_hairmask"
    mask_asset = derive_asset(
        asset_id=mask_id,
        media_kind="image",
        seed_parts=(clip.content_sha256, "mask_hair", payload.face_track_id, payload.edge_quality),
        duration_us=0,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "hair_mask",
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "edge_quality": payload.edge_quality,
            "coordinate_space": "normalized",
            "processor": "nagar.portrait.mask_hair.v1",
        },
    )
    evidence = MaskEvidence(
        mask_id=mask_id,
        source_asset_id=clip.asset_id,
        width=width,
        height=height,
        coverage_ratio=coverage,
        confidence=0.9,
    )
    new_project = project.model_copy(update={"assets": [*project.assets, mask_asset]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "mask": evidence.model_dump(mode="json"),
            "mask_id": mask_id,
            "source_clip_id": clip.asset_id,
            "edge_quality": payload.edge_quality,
            "feather_px": EDGE_FEATHER_PX[payload.edge_quality],
            "content_sha256": mask_asset.content_sha256,
        },
    )


def _background_blur(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.background_blur."""
    payload = BackgroundBlurInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_BACKGROUND_BLUR)
    mask = _require_mask(known, payload.mask_asset_id, OPERATION_BACKGROUND_BLUR)

    profile = BLUR_PROFILES[payload.blur_profile]
    output_id = payload.output_asset_id or f"{clip.asset_id}_bgblur"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "background_blur",
            mask.content_sha256,
            payload.blur_profile,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id, mask.asset_id),
        provenance={
            "effect": "background_blur",
            "source_clip_id": clip.asset_id,
            "subject_mask_asset_id": mask.asset_id,
            "blur_profile": payload.blur_profile,
            "radius_px": profile["radius_px"],
            "mix": profile["mix"],
            "processor": "nagar.portrait.background_blur.v1",
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
            "blur_profile": payload.blur_profile,
            "radius_px": profile["radius_px"],
            "mix": profile["mix"],
            "content_sha256": derived.content_sha256,
        },
    )


def _stabilize_face(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for portrait.stabilize_face → TransformCurve."""
    payload = StabilizeFaceInput.model_validate(context.input_data)
    known = _asset_index(project)
    clip = _require_video(known, payload.clip_asset_id, OPERATION_STABILIZE_FACE)

    offsets = sample_offsets_us(clip.duration_us, "standard")
    keyframes: list[tuple[int, float, float, float, float]] = []
    for index, offset in enumerate(offsets):
        # Deterministic jitter cancel: the corrective shift is the (seeded)
        # jitter seen by the contract detector, scaled by strength and policy.
        unit = (
            int(digest_seed_hash(clip.content_sha256, "jitter", index)[7:13], 16) % 2000
        ) / 1000.0  # 0..2
        dx = -(unit - 1.0) * payload.strength * 0.05
        dy = -(1.0 - unit) * payload.strength * 0.05
        scale = 1.0
        rotation = 0.0
        if payload.stabilization_policy == "lock_position_scale":
            scale = 1.0 + (unit - 1.0) * payload.strength * 0.02
        elif payload.stabilization_policy == "smooth_only":
            dx *= 0.5
            dy *= 0.5
        keyframes.append((offset, dx, dy, scale, rotation))

    curve = transform_curve_from_offsets(
        curve_id=f"stab-{digest_seed_hash(clip.content_sha256, payload.face_track_id)[7:19]}",
        source_asset_id=clip.asset_id,
        policy=payload.stabilization_policy,
        duration_us=clip.duration_us,
        offsets_xy_scale_rot=keyframes,
        confidence=0.85,
    )
    output_id = payload.output_asset_id or f"{clip.asset_id}_facedstab"
    derived = derive_asset(
        asset_id=output_id,
        media_kind="video",
        seed_parts=(
            clip.content_sha256,
            "stabilize_face",
            payload.face_track_id,
            payload.stabilization_policy,
            payload.strength,
        ),
        duration_us=clip.duration_us,
        parent_asset_ids=(clip.asset_id,),
        provenance={
            "effect": "face_stabilize",
            "source_clip_id": clip.asset_id,
            "face_track_id": payload.face_track_id,
            "stabilization_policy": payload.stabilization_policy,
            "strength": payload.strength,
            "curve_id": curve.curve_id,
            "processor": "nagar.portrait.stabilize_face.v1",
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
            "keyframe_count": len(curve.samples),
            "stabilization_policy": payload.stabilization_policy,
            "content_sha256": derived.content_sha256,
        },
    )


def register_portrait_operations(registry: CapabilityRegistry) -> None:
    """Register all portrait operations with the capability registry."""
    specs: tuple[OperationSpec, ...] = (
        OperationSpec(
            operation_id=OPERATION_DETECT_LANDMARKS,
            description="Sample face landmarks over a clip into a FaceTrackSet (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=DetectLandmarksInput,
            handler=_detect_landmarks,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_SMOOTH_SKIN,
            description="Temporal skin smoothing over a face track or mask (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=SmoothSkinInput,
            handler=_smooth_skin,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_RETOUCH_BLEMISH,
            description="Local blemish inpaint over a FaceMask (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RetouchBlemishInput,
            handler=_retouch_blemish,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_RELIGHT_FACE,
            description="Typed LightModel face relight layer (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RelightFaceInput,
            handler=_relight_face,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_WHITEN_TEETH,
            description="TeethMask color lift with feathered edges (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=WhitenTeethInput,
            handler=_whiten_teeth,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_CORRECT_GAZE,
            description="Identity-sensitive gaze redirection (Level C — confirmation).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=CorrectGazeInput,
            handler=_correct_gaze,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_ENHANCE_EYES,
            description="Eye clarity / red-eye reduction layer (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=EnhanceEyesInput,
            handler=_enhance_eyes,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_MASK_HAIR,
            description="Hair matting into a MaskRef image asset (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=MaskHairInput,
            handler=_mask_hair,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_BACKGROUND_BLUR,
            description="SubjectMask background blur profiles (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=BackgroundBlurInput,
            handler=_background_blur,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
        OperationSpec(
            operation_id=OPERATION_STABILIZE_FACE,
            description="Face stabilization TransformCurve plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=StabilizeFaceInput,
            handler=_stabilize_face,
            required_packs=(PORTRAIT_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    for spec in specs:
        capability = spec.operation_id.split(".", 1)[1]
        registry.register_operation(DOMAIN, capability, spec)


def build_portrait_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with the portrait operations."""
    registry = CapabilityRegistry()
    register_portrait_operations(registry)
    return registry
