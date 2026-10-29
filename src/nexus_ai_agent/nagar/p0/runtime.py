"""Pure P0 preview and canonical commit-boundary contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.packs.edit.models import TrimInput

from .nel import NELDiagnostic, TypedTrimIR, canonical_hash, compile_nel
from .semantic_time import (
    SemanticSegment,
    pts_to_microseconds,
    pts_with_delta_to_microseconds,
    resolve_segment,
)


class P0PlanEnvelope(BaseModel):
    """Preview metadata/preconditions kept outside the operation input schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "nagar.plan.v1"
    project_id: str
    tenant_id: str
    semantic_segment_id: str
    timeline_revision: int = Field(ge=0)
    canonical_nel_hash: str
    required_evidence: tuple[str, ...] = ()


class P0Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: str
    ir: TypedTrimIR | None = None
    segment: SemanticSegment | None = None
    plan: P0PlanEnvelope | None = None
    diagnostics: tuple[NELDiagnostic | str, ...] = ()
    job_created: bool = False
    requires_confirmation: bool = True


def preview_nel(
    source: str,
    *,
    project_id: str,
    tenant_id: str,
    timeline_revision: int,
    segments: list[SemanticSegment],
    source_duration_us: int | None = None,
) -> P0Preview:
    """Compile and resolve a plan; this function is incapable of creating a job."""
    compiled = compile_nel(source)
    if compiled.diagnostics or compiled.ir is None or compiled.ast is None:
        return P0Preview(status="REJECTED", diagnostics=compiled.diagnostics)
    if compiled.ir.project_id != project_id:
        return P0Preview(status="REJECTED", ir=compiled.ir, diagnostics=("project_mismatch",))
    if source_duration_us is not None and source_duration_us <= 0:
        return P0Preview(
            status="REJECTED", ir=compiled.ir, diagnostics=("invalid_source_duration",)
        )
    resolution = resolve_segment(
        label=compiled.ir.semantic_label,
        project_id=project_id,
        tenant_id=tenant_id,
        timeline_revision=timeline_revision,
        candidates=segments,
    )
    if resolution.clarification_required or resolution.selected is None:
        return P0Preview(
            status="NEEDS_CLARIFICATION",
            ir=compiled.ir,
            diagnostics=(resolution.reason or "unresolved_segment",),
        )
    segment = resolution.selected
    try:
        start_us = pts_to_microseconds(
            pts=segment.start_pts,
            timebase_num=segment.timebase_num,
            timebase_den=segment.timebase_den,
        )
    except ValueError as exc:
        return P0Preview(
            status="REJECTED", ir=compiled.ir, segment=segment, diagnostics=(str(exc),)
        )
    try:
        final_end_us = pts_with_delta_to_microseconds(
            pts=segment.end_pts,
            delta_microseconds=compiled.ir.duration_delta_us,
            timebase_num=segment.timebase_num,
            timebase_den=segment.timebase_den,
        )
    except ValueError as exc:
        return P0Preview(
            status="REJECTED", ir=compiled.ir, segment=segment, diagnostics=(str(exc),)
        )
    if final_end_us <= start_us:
        return P0Preview(
            status="REJECTED",
            ir=compiled.ir,
            segment=segment,
            diagnostics=("trim_exceeds_segment",),
        )
    if source_duration_us is not None and final_end_us > source_duration_us:
        return P0Preview(
            status="REJECTED",
            ir=compiled.ir,
            segment=segment,
            diagnostics=("source_bounds_exceeded",),
        )
    if compiled.ir.duration_delta_us > 0 and source_duration_us is None:
        return P0Preview(
            status="NEEDS_CLARIFICATION",
            ir=compiled.ir,
            segment=segment,
            diagnostics=("source_bounds_required_for_positive_delta",),
        )
    available = set(segment.evidence_refs) | {segment.source_model}
    missing = tuple(item for item in compiled.ir.required_evidence if item not in available)
    if missing:
        return P0Preview(
            status="NEEDS_CLARIFICATION",
            ir=compiled.ir,
            segment=segment,
            diagnostics=tuple(f"missing_evidence:{item}" for item in missing),
        )
    plan = P0PlanEnvelope(
        project_id=project_id,
        tenant_id=tenant_id,
        semantic_segment_id=segment.segment_id,
        timeline_revision=timeline_revision,
        canonical_nel_hash=canonical_hash(compiled.ast),
        required_evidence=compiled.ir.required_evidence,
    )
    return P0Preview(status="PREVIEW_READY", ir=compiled.ir, segment=segment, plan=plan)


def canonical_command_input(preview: P0Preview) -> dict[str, Any]:
    """Return only fields accepted by the canonical ``TrimInput`` model."""
    if preview.status != "PREVIEW_READY" or preview.ir is None or preview.segment is None:
        raise ValueError("preview_not_commit_ready")
    start_us = pts_to_microseconds(
        pts=preview.segment.start_pts,
        timebase_num=preview.segment.timebase_num,
        timebase_den=preview.segment.timebase_den,
    )
    end_us = (
        pts_to_microseconds(
            pts=preview.segment.end_pts,
            timebase_num=preview.segment.timebase_num,
            timebase_den=preview.segment.timebase_den,
        )
        + preview.ir.duration_delta_us
    )
    payload = {
        "clip_asset_id": preview.segment.asset_id,
        "in_point_us": start_us,
        "out_point_us": end_us,
    }
    return TrimInput.model_validate(payload).model_dump(mode="json", exclude_none=True)
