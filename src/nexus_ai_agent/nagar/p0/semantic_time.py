"""Semantic labels over canonical timeline time, never a replacement for PTS."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EvidenceKind(str, Enum):
    RAW_OBSERVATION = "RAW_OBSERVATION"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    HUMAN_ASSERTION = "HUMAN_ASSERTION"
    DETERMINISTIC_CHECK = "DETERMINISTIC_CHECK"


class SemanticSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    segment_id: str = Field(min_length=1, max_length=128)
    project_id: str
    tenant_id: str
    asset_id: str
    timeline_revision: int = Field(ge=0)
    label: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    start_pts: int = Field(ge=0)
    end_pts: int = Field(gt=0)
    timebase_num: int = Field(gt=0)
    timebase_den: int = Field(gt=0)
    evidence_refs: tuple[str, ...] = ()
    evidence_kind: EvidenceKind
    confidence: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    source_model: str = Field(min_length=1, max_length=128)
    valid: bool = True

    def overlaps(self, *, start_pts: int, end_pts: int) -> bool:
        return self.start_pts < end_pts and start_pts < self.end_pts


class SegmentResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str
    candidates: tuple[SemanticSegment, ...] = ()
    selected: SemanticSegment | None = None
    clarification_required: bool = False
    reason: str | None = None


def resolve_segment(
    *,
    label: str,
    project_id: str,
    tenant_id: str,
    timeline_revision: int,
    candidates: list[SemanticSegment],
    min_confidence: float = 0.7,
) -> SegmentResolution:
    visible = tuple(
        item
        for item in candidates
        if item.project_id == project_id
        and item.tenant_id == tenant_id
        and item.label == label
        and item.timeline_revision == timeline_revision
        and item.valid
    )
    if not visible:
        return SegmentResolution(
            label=label, clarification_required=True, reason="segment_not_found"
        )
    if any(
        item.confidence < min_confidence or item.confidence - item.uncertainty < min_confidence
        for item in visible
    ):
        return SegmentResolution(
            label=label,
            candidates=visible,
            clarification_required=True,
            reason="confidence_below_threshold",
        )
    if len(visible) != 1:
        return SegmentResolution(
            label=label,
            candidates=visible,
            clarification_required=True,
            reason="ambiguous_candidates",
        )
    return SegmentResolution(label=label, candidates=visible, selected=visible[0])


def invalidate_after_revision(segment: SemanticSegment, new_revision: int) -> SemanticSegment:
    if new_revision <= segment.timeline_revision:
        return segment
    return segment.model_copy(update={"valid": False})
