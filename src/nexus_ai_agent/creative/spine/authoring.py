"""Deterministic construction of the existing Creative IR from a typed strategy."""

from __future__ import annotations

from nexus_ai_agent.creative.intelligence.ir import (
    Asset,
    AssetKind,
    Constraint,
    ConstraintTarget,
    CreativeBrief,
    CreativeWork,
    Layer,
    MediaContent,
    NarrativeRole,
    Origin,
    OutputRequirement,
    Priority,
    Scene,
    Segment,
    SemanticRole,
    Timing,
    TimingConstraint,
)
from nexus_ai_agent.creative.intelligence.semantics import apply_semantics
from nexus_ai_agent.creative.spine.models import Intent
from nexus_ai_agent.creative.spine.strategy import (
    SourceAssetDescriptor,
    StrategyProposal,
)


class AuthoringError(ValueError):
    """The strategy cannot be represented honestly by the current Creative IR."""


def build_creative_work(
    intent: Intent,
    proposal: StrategyProposal,
    source: SourceAssetDescriptor,
) -> CreativeWork:
    """Build, normalize, seal and validate one source-preserving trim Work.

    Original media paths are deliberately absent here.  The Asset URI carries an
    opaque server-assigned id plus its measured digest; the queue worker later
    receives the separately trusted staged path from application context.
    """
    if proposal.source_asset_id != source.asset_id:
        raise AuthoringError("strategy source asset does not match trusted request context")
    if intent.source_range is None or (
        proposal.in_point_us != intent.source_range.in_point_us
        or proposal.out_point_us != intent.source_range.out_point_us
    ):
        raise AuthoringError("strategy may not alter the user's explicit source range")
    if proposal.semantic_intents != intent.semantic_intents:
        raise AuthoringError("strategy may not drop or invent semantic intents")
    if proposal.objective != "video_trim" or proposal.operation != "timeline.trim":
        raise AuthoringError("the authoring slice supports only timeline.trim")
    if proposal.out_point_us > source.duration_us:
        raise AuthoringError("trim range exceeds measured source duration")
    if intent.unresolved_requirements:
        raise AuthoringError(
            "unresolved requirements cannot be dropped: "
            + "; ".join(intent.unresolved_requirements)
        )
    if intent.constraints:
        raise AuthoringError(
            "the trim slice cannot prove additional constraints yet: "
            + "; ".join(intent.constraints)
        )

    duration_us = proposal.out_point_us - proposal.in_point_us
    user_origin = Origin(
        source="user",
        detail=(intent.source_text or intent.goal)[:500],
        reference_id=intent.request_id or intent.intent_id,
    )
    strategy_origin = Origin(
        source="strategy",
        detail=proposal.rationale,
        reference_id=proposal.strategy_id,
    )
    source_asset = Asset(
        asset_id="source",
        kind=AssetKind.VIDEO,
        uri=f"asset://{source.asset_id}/{source.content_sha256.removeprefix('sha256:')}",
        role=SemanticRole.SUBJECT,
        duration_us=source.duration_us,
        width_px=source.width_px,
        height_px=source.height_px,
        frame_rate_milli=source.frame_rate_milli,
        origin=user_origin,
    )
    layer = Layer(
        layer_id="subject-layer",
        role=SemanticRole.SUBJECT,
        content=MediaContent(
            asset_ref="source",
            segment=Segment(
                source_in_us=proposal.in_point_us,
                source_duration_us=duration_us,
                timeline=Timing(start_us=0, duration_us=duration_us),
            ),
        ),
        origin=strategy_origin,
    )
    scene = Scene(
        scene_id="trimmed-source",
        label="Trimmed source",
        narrative_role=NarrativeRole.DEVELOPMENT,
        timing=Timing(start_us=0, duration_us=duration_us),
        layers=(layer,),
        origin=strategy_origin,
    )
    exact_duration = Constraint(
        priority=Priority.HARD,
        spec=TimingConstraint(
            target=ConstraintTarget(kind="work"),
            min_us=duration_us,
            max_us=duration_us,
        ),
        origin=user_origin,
    )
    work = CreativeWork(
        brief=CreativeBrief(
            goal=intent.goal,
            semantic_intents=proposal.semantic_intents,
            origin=user_origin,
        ),
        assets=(source_asset,),
        scenes=(scene,),
        constraints=(exact_duration,),
        # Match the live trim lane's explicit defaults (1280x720, 30fps); the
        # compiler refuses a CreativeWork asking for an unimplemented profile.
        output=OutputRequirement(
            width_px=1280,
            height_px=720,
            frame_rate_milli=30000,
            max_duration_us=duration_us,
        ),
    )
    normalized = apply_semantics(work)
    if normalized.brief.unresolved_intents:
        raise AuthoringError(
            "semantic intents remain unresolved: " + "; ".join(normalized.brief.unresolved_intents)
        )
    normalized.assert_valid()
    normalized.assert_constraints()
    normalized.verify_identity()
    return normalized


__all__ = ["AuthoringError", "build_creative_work"]
