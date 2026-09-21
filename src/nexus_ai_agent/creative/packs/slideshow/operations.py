"""The slideshow pack's six pure operations.

Design contract (inherited from Wave 1 and the TDD):

* **handlers are pure** — ``(project, context) -> OperationOutcome`` with no
  I/O, no network, no clock and no randomness beyond id minting.  Probing,
  hashing, beat detection, image analysis and (in Wave 2c) encoding all happen
  *above* the bus; the command carries pinned evidence;
* **one transaction per intent** — ``slideshow.compose`` builds the whole
  slideshow (tracks, clips, effect layers, asset references) in a single
  atomic, undoable transaction;
* **the agent never writes FFmpeg syntax** — the plan holds parameters, the
  render IR (Wave 2c) derives the filtergraph from them.

Permission ladder: scanning registers media (B, reversible), scoring and tone
suggestion are analysis (A), composing is a real edit (B), rendering is the
first level-C operation (heavy export, requires ``confirmed=true``).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.packs.slideshow.models import (
    OPERATION_COMPOSE,
    OPERATION_RENDER,
    OPERATION_UPSCALE,
    AssetEvidence,
    ComposeInput,
    ImageScore,
    RenderInput,
    ShotPlan,
    UpscaleInput,
)
from nexus_ai_agent.creative.packs.slideshow.planning import (
    plan_slideshow,
    suggest_template_id,
)
from nexus_ai_agent.creative.packs.slideshow.templates import (
    TemplateError,
    ToneTemplateLibrary,
    load_tone_templates,
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
    Clip,
    CommandValidationError,
    EffectLayerRef,
    MediaRef,
    PermissionLevel,
    Project,
    TimeRangeUS,
    Track,
)

DOMAIN = "slideshow"
OPERATION_SCAN = "slideshow.scan_assets"
OPERATION_SCORE = "slideshow.score_images"
OPERATION_SUGGEST_TONE = "slideshow.suggest_tone"

#: Layers written into canonical state by ``slideshow.compose``.
LAYER_TEMPLATE = "slideshow.tone"
LAYER_RENDER_PROFILE = "slideshow.render_profile"
LAYER_MOTION = "slideshow.motion"
LAYER_GRADE = "slideshow.grade"
LAYER_TRANSITION = "slideshow.transition"

_LIBRARY: ToneTemplateLibrary | None = None


def tone_library() -> ToneTemplateLibrary:
    """The shipped tone-template library (loaded once per process)."""
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = load_tone_templates()
    return _LIBRARY


def use_tone_library(library: ToneTemplateLibrary | None) -> None:
    """Point the handlers at a specific library (composition seam for callers/tests)."""
    global _LIBRARY
    _LIBRARY = library


# ---------------------------------------------------------------------------
# typed operation inputs
# ---------------------------------------------------------------------------


class ScanAssetsInput(BaseModel):
    """Register probed, hashed media in the project asset registry."""

    model_config = ConfigDict(extra="forbid")

    assets: tuple[AssetEvidence, ...] = Field(min_length=1)
    replace_existing: bool = False


class ScoreImagesInput(BaseModel):
    """Normalize a score sheet (local heuristics or a hosted model) and order it.

    ``preferred_order`` lets a hosted model propose a narrative sequence; the
    pack still decides the *roles* (opener/climax/closer) and rejects anything
    that is not a permutation of the sheet, so a model can never invent or drop
    a frame.
    """

    model_config = ConfigDict(extra="forbid")

    scores: tuple[ImageScore, ...] = Field(min_length=1)
    preferred_order: tuple[str, ...] = ()
    source: Literal["gemini", "local_heuristic"] = "local_heuristic"
    reasoning: str = ""


class SuggestToneInput(BaseModel):
    """Pick a tone template from measurable evidence (no state change)."""

    model_config = ConfigDict(extra="forbid")

    image_count: int = Field(ge=1, le=500)
    tempo_bpm: float | None = Field(default=None, gt=20.0, le=300.0)
    requested_template_id: str | None = None
    recommended_template_id: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {record.asset_id: record for record in project.assets}


def _require_asset(project: Project, asset_id: str, *, kind: str | None = None) -> AssetRecord:
    record = _asset_index(project).get(asset_id)
    if record is None:
        raise CommandValidationError(
            f"asset {asset_id!r} is not registered; run slideshow.scan_assets first"
        )
    if kind is not None and record.media_kind != kind:
        raise CommandValidationError(
            f"asset {asset_id!r} is {record.media_kind!r}, expected {kind!r}"
        )
    return record


def _keeper_score(score: ImageScore) -> float:
    return 0.6 * score.aesthetic + 0.4 * score.sharpness


def _roles_for(ordered: list[ImageScore]) -> list[str]:
    """Positional narrative roles: opener, closer, and the best middle as climax."""
    roles = ["body"] * len(ordered)
    if not ordered:
        return roles
    roles[0] = "opener"
    if len(ordered) > 1:
        roles[-1] = "closer"
    if len(ordered) >= 4:
        climax = max(
            range(1, len(ordered) - 1),
            key=lambda index: (_keeper_score(ordered[index]), ordered[index].evidence_id),
        )
        roles[climax] = "climax"
    return roles


def _order_scores(
    scores: tuple[ImageScore, ...], preferred_order: tuple[str, ...] = ()
) -> tuple[ImageScore, ...]:
    """Deterministic narrative order, honouring a valid proposed sequence.

    Without a proposal the sheet is ordered by aesthetic quality (tie-break:
    keeper score, then evidence id) — the prettiest frame opens the show.
    A valid ``preferred_order`` permutation wins, but roles are always assigned
    by the pack itself.
    """
    by_id = {score.evidence_id: score for score in scores}
    if preferred_order:
        ordered = [by_id[evidence_id] for evidence_id in preferred_order if evidence_id in by_id]
        if len(ordered) == len(by_id) and len({score.evidence_id for score in ordered}) == len(
            ordered
        ):
            ranked = ordered
        else:
            ranked = sorted(scores, key=lambda score: (-score.aesthetic, score.evidence_id))
    else:
        ranked = sorted(scores, key=lambda score: (-score.aesthetic, score.evidence_id))
    roles = _roles_for(ranked)
    return tuple(
        score.model_copy(update={"suggested_role": role})
        for score, role in zip(ranked, roles, strict=False)
    )


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _scan_assets(project: Project, context: OperationContext) -> OperationOutcome:
    payload = ScanAssetsInput.model_validate(context.input_data)
    existing = _asset_index(project)
    registered: list[str] = []
    unchanged: list[str] = []
    records = list(project.assets)
    for evidence in payload.assets:
        current = existing.get(evidence.evidence_id)
        if current is not None:
            if current.content_sha256 == evidence.content_sha256:
                unchanged.append(evidence.evidence_id)
                continue
            raise CommandValidationError(
                f"asset {evidence.evidence_id!r} is already registered with a different "
                f"content hash ({current.content_sha256} != {evidence.content_sha256}); "
                "use a new evidence id for changed media"
            )
        kind: Literal["image", "audio"] = "image" if evidence.media_kind == "image" else "audio"
        record = AssetRecord(
            asset_id=evidence.evidence_id,
            media_kind=kind,
            content_sha256=evidence.content_sha256,
            duration_us=evidence.duration_us,
            provenance={
                "path": evidence.path,
                "width": evidence.width,
                "height": evidence.height,
                "mean_luma": evidence.mean_luma,
                "captured_at": evidence.captured_at.isoformat() if evidence.captured_at else None,
            },
        )
        records.append(record)
        existing[evidence.evidence_id] = record
        registered.append(evidence.evidence_id)
    new_project = project.model_copy(update={"assets": records})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "registered": registered,
            "unchanged": unchanged,
            "asset_count": len(records),
        },
    )


def _score_images(project: Project, context: OperationContext) -> OperationOutcome:
    payload = ScoreImagesInput.model_validate(context.input_data)
    known = _asset_index(project)
    unknown = [score.evidence_id for score in payload.scores if score.evidence_id not in known]
    if unknown and known:
        raise CommandValidationError(f"score sheet references unknown assets: {unknown}")
    ordered = _order_scores(payload.scores, payload.preferred_order)
    return OperationOutcome(
        project,
        context.history,
        {
            "ordered_evidence_ids": [score.evidence_id for score in ordered],
            "scores": [score.model_dump(mode="json") for score in ordered],
            "source": payload.source,
            "reasoning": payload.reasoning,
        },
    )


def _suggest_tone(project: Project, context: OperationContext) -> OperationOutcome:
    payload = SuggestToneInput.model_validate(context.input_data)
    library = tone_library()
    try:
        if payload.requested_template_id:
            template_id = library.get(payload.requested_template_id).template_id
            rationale = "explicit request"
        elif payload.recommended_template_id and payload.recommended_template_id in library:
            template_id = library.get(payload.recommended_template_id).template_id
            rationale = "analyzer recommendation"
        else:
            template_id = suggest_template_id(
                library, tempo_bpm=payload.tempo_bpm, image_count=payload.image_count
            )
            rationale = "tempo/image-count heuristic"
    except TemplateError as exc:
        raise CommandValidationError(str(exc)) from exc
    return OperationOutcome(
        project,
        context.history,
        {
            "template_id": template_id,
            "template_name_fa": library.get(template_id).display_name_fa,
            "rationale": rationale,
            "available_primary": list(library.ids(tier="primary")),
        },
    )


def _clip_id() -> str:
    return f"clip_{uuid.uuid4().hex[:12]}"


def _shot_layers(shot: ShotPlan, slot: TimeRangeUS) -> list[EffectLayerRef]:
    layers = [
        EffectLayerRef(operation=LAYER_MOTION, parameters=dict(shot.motion), range=slot),
        EffectLayerRef(operation=LAYER_GRADE, parameters=dict(shot.color), range=slot),
    ]
    transition = shot.transition_in
    if transition:
        duration_us = int(transition.get("duration_us", 0))
        if 0 < duration_us < (slot.end_us - slot.start_us):
            layers.append(
                EffectLayerRef(
                    operation=LAYER_TRANSITION,
                    parameters=dict(transition),
                    range=TimeRangeUS(start_us=slot.start_us, end_us=slot.start_us + duration_us),
                )
            )
    return layers


def _compose(project: Project, context: OperationContext) -> OperationOutcome:
    payload = ComposeInput.model_validate(context.input_data)
    records = [_require_asset(project, asset.evidence_id, kind="image") for asset in payload.assets]
    for evidence, record in zip(payload.assets, records, strict=False):
        if evidence.content_sha256 != record.content_sha256:
            raise CommandValidationError(
                f"asset {evidence.evidence_id!r} changed after it was registered "
                "(content hash mismatch); re-run slideshow.scan_assets"
            )
    audio_record = (
        _require_asset(project, payload.audio.evidence_id, kind="audio")
        if payload.audio is not None
        else None
    )
    try:
        plan = plan_slideshow(payload, library=tone_library())
    except (TemplateError, ValueError) as exc:
        raise CommandValidationError(f"slideshow.compose: {exc}") from exc

    target_us = plan.target_duration_us
    track_id = f"track_{uuid.uuid4().hex[:12]}"
    clips: list[Clip] = []
    for shot, record in zip(plan.shots, records, strict=False):
        slot = shot.slot
        duration_us = slot.end_us - slot.start_us
        clips.append(
            Clip(
                clip_id=_clip_id(),
                media_ref=MediaRef(
                    asset_id=record.asset_id,
                    content_sha256=record.content_sha256,
                    media_kind="image",
                    duration_us=duration_us,
                ),
                source_range=TimeRangeUS(start_us=0, end_us=duration_us),
                timeline_range=slot,
                effects=_shot_layers(shot, slot),
            )
        )
    track_effects = [
        EffectLayerRef(
            operation=LAYER_TEMPLATE,
            parameters={"template_id": plan.template_id},
            range=TimeRangeUS(start_us=0, end_us=target_us),
        ),
        EffectLayerRef(
            operation=LAYER_RENDER_PROFILE,
            parameters=dict(plan.render_profile),
            range=TimeRangeUS(start_us=0, end_us=target_us),
        ),
    ]
    slideshow_track = Track(
        track_id=track_id,
        name=f"Slideshow ({plan.template_id})",
        kind="video",
        clips=clips,
        effects=track_effects,
    )
    new_tracks = [*project.timeline.tracks, slideshow_track]

    if audio_record is not None:
        audio_duration = audio_record.duration_us or target_us
        audio_span = min(audio_duration, target_us)
        audio_track = Track(
            track_id=f"track_{uuid.uuid4().hex[:12]}",
            name="Slideshow audio",
            kind="audio",
            clips=[
                Clip(
                    clip_id=_clip_id(),
                    media_ref=MediaRef(
                        asset_id=audio_record.asset_id,
                        content_sha256=audio_record.content_sha256,
                        media_kind="audio",
                        duration_us=audio_record.duration_us,
                    ),
                    source_range=TimeRangeUS(start_us=0, end_us=audio_span),
                    timeline_range=TimeRangeUS(start_us=0, end_us=audio_span),
                )
            ],
        )
        new_tracks.append(audio_track)

    new_timeline = project.timeline.model_copy(
        update={"tracks": new_tracks, "duration_us": max(project.timeline.duration_us, target_us)}
    )
    new_project = project.model_copy(update={"timeline": new_timeline})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "plan": plan.model_dump(mode="json"),
            "template_id": plan.template_id,
            "track_id": track_id,
            "clip_ids": [clip.clip_id for clip in clips],
            "warnings": list(plan.warnings),
            "alignment_quality": plan.alignment_quality,
        },
    )


def _upscale(project: Project, context: OperationContext) -> OperationOutcome:
    payload = UpscaleInput.model_validate(context.input_data)
    source = _require_asset(project, payload.source_asset_id, kind="image")
    if source.content_sha256 != payload.source_sha256:
        raise CommandValidationError(
            f"asset {payload.source_asset_id!r} changed after it was registered "
            "(content hash mismatch); re-run slideshow.scan_assets"
        )
    asset_id = f"derived_{uuid.uuid4().hex[:12]}"
    target = payload.target_resolution or f"{payload.scale_factor:g}x"
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="image",
        content_sha256=payload.output_sha256,
        duration_us=0,
        parent_asset_ids=(payload.source_asset_id,),
        provenance={
            "output_path": payload.output_path,
            "source_dimensions": f"{payload.source_width}x{payload.source_height}",
            "resolution": f"{payload.width}x{payload.height}",
            "target": target,
            "filter": f"scale={payload.width}:{payload.height}:flags={payload.filter_flags}",
            "produced_by": "nagar.local.slideshow.upscale.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": asset_id,
            "output_path": payload.output_path,
            "is_derived": True,
            "parent_asset_ids": [payload.source_asset_id],
            "width": payload.width,
            "height": payload.height,
        },
    )


def _render(project: Project, context: OperationContext) -> OperationOutcome:
    payload = RenderInput.model_validate(context.input_data)
    for parent_id in payload.parent_asset_ids:
        _require_asset(project, parent_id)
    asset_id = f"derived_{uuid.uuid4().hex[:12]}"
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="video",
        content_sha256=payload.output_sha256,
        duration_us=payload.duration_us,
        parent_asset_ids=tuple(payload.parent_asset_ids),
        provenance={
            "output_path": payload.output_path,
            "render_ir_hash": payload.render_ir_hash,
            "state_hash_before_render": payload.state_hash_before_render,
            "encoder": payload.encoder,
            "template_id": payload.template_id,
            "produced_by": "nagar.local.slideshow.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": asset_id,
            "output_path": payload.output_path,
            "is_derived": True,
            "parent_asset_ids": list(record.parent_asset_ids),
        },
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_slideshow_operations(
    registry: CapabilityRegistry, *, library: ToneTemplateLibrary | None = None
) -> CapabilityRegistry:
    """Register the pack's six operations on an existing capability registry."""
    if library is not None:
        use_tone_library(library)
    registry.register_domain(DOMAIN, "Slideshow composition (pack nexus.slideshow.compose)")
    registry.register_operation(
        DOMAIN,
        "ingest",
        OperationSpec(
            operation_id=OPERATION_SCAN,
            description="Register probed, hashed media (images/audio) in the asset registry.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ScanAssetsInput,
            handler=_scan_assets,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "analysis",
        OperationSpec(
            operation_id=OPERATION_SCORE,
            description="Normalize an image score sheet and derive the narrative order.",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=ScoreImagesInput,
            handler=_score_images,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "analysis",
        OperationSpec(
            operation_id=OPERATION_SUGGEST_TONE,
            description="Suggest a tone template from tempo and image count (pure).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=SuggestToneInput,
            handler=_suggest_tone,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "composition",
        OperationSpec(
            operation_id=OPERATION_COMPOSE,
            description="Compose the slideshow as ONE atomic transaction (timeline + effects).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ComposeInput,
            handler=_compose,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "enhancement",
        OperationSpec(
            operation_id=OPERATION_UPSCALE,
            description="Record a local FFmpeg Lanczos upscale as a derived image.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=UpscaleInput,
            handler=_upscale,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "delivery",
        OperationSpec(
            operation_id=OPERATION_RENDER,
            description="Record a rendered master as a derived asset (heavy export, level C).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=RenderInput,
            handler=_render,
            required_packs=("nexus.slideshow.compose",),
            deterministic=True,
        ),
    )
    return registry


def build_slideshow_registry(*, library: ToneTemplateLibrary | None = None) -> CapabilityRegistry:
    """The Wave 1 catalog plus the slideshow pack's six operations.

    ``build_wave1_registry()`` stays untouched (the frozen Wave 1 catalog): the
    Wave 2 surface is a *composition* of the frozen skeleton and the pack.
    """
    return register_slideshow_operations(build_wave1_registry(), library=library)


__all__ = [
    "DOMAIN",
    "LAYER_GRADE",
    "LAYER_MOTION",
    "LAYER_RENDER_PROFILE",
    "LAYER_TEMPLATE",
    "LAYER_TRANSITION",
    "OPERATION_COMPOSE",
    "OPERATION_RENDER",
    "OPERATION_SCAN",
    "OPERATION_SCORE",
    "OPERATION_SUGGEST_TONE",
    "OPERATION_UPSCALE",
    "ScanAssetsInput",
    "ScoreImagesInput",
    "SuggestToneInput",
    "build_slideshow_registry",
    "register_slideshow_operations",
    "tone_library",
    "use_tone_library",
]
