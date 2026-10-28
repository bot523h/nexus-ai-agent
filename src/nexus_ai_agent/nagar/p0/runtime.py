"""Pure P0 preview adapter for the existing canonical execution path.

This module plans only. A commit adapter must still call the existing
CommandBus/queue and supply backend-owned actor, tenant, policy, and revision facts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from .nel import NELDiagnostic, TypedTrimIR, compile_nel
from .semantic_time import SemanticSegment, resolve_segment


class P0Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: str
    ir: TypedTrimIR | None = None
    segment: SemanticSegment | None = None
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
) -> P0Preview:
    """Compile and resolve a plan; this function is incapable of creating a job."""
    compiled = compile_nel(source)
    if compiled.diagnostics or compiled.ir is None:
        return P0Preview(status="REJECTED", diagnostics=compiled.diagnostics)
    if compiled.ir.project_id != project_id:
        return P0Preview(status="REJECTED", ir=compiled.ir, diagnostics=("project_mismatch",))
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
            segment=None,
            diagnostics=(resolution.reason or "unresolved_segment",),
        )
    segment = resolution.selected
    delta_pts = round(
        compiled.ir.duration_delta_us * segment.timebase_den / (segment.timebase_num * 1_000_000)
    )
    if compiled.ir.duration_delta_us < 0 and segment.end_pts - segment.start_pts <= abs(delta_pts):
        return P0Preview(
            status="REJECTED",
            ir=compiled.ir,
            segment=segment,
            diagnostics=("trim_exceeds_segment",),
        )
    return P0Preview(status="PREVIEW_READY", ir=compiled.ir, segment=segment)


def canonical_command_input(preview: P0Preview) -> dict[str, Any]:
    """Map a verified preview to the existing `timeline.trim` input shape."""
    if preview.status != "PREVIEW_READY" or preview.ir is None or preview.segment is None:
        raise ValueError("preview_not_commit_ready")
    start = preview.segment.start_pts
    delta_pts = round(
        preview.ir.duration_delta_us
        * preview.segment.timebase_den
        / (preview.segment.timebase_num * 1_000_000)
    )
    end = preview.segment.end_pts + delta_pts
    if end <= start:
        raise ValueError("invalid_trim_window")
    return {
        "clip_asset_id": preview.segment.asset_id,
        "in_point_us": start,
        "out_point_us": end,
        "semantic_segment_id": preview.segment.segment_id,
        "timeline_revision": preview.segment.timeline_revision,
    }
