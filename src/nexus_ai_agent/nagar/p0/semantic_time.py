"""Semantic labels over canonical timeline time, never a replacement for PTS."""

from __future__ import annotations

from enum import Enum
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceKind(str, Enum):
    RAW_OBSERVATION = "RAW_OBSERVATION"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    HUMAN_ASSERTION = "HUMAN_ASSERTION"
    DETERMINISTIC_CHECK = "DETERMINISTIC_CHECK"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: str = Field(min_length=1, max_length=128)
    project_id: str
    tenant_id: str
    timeline_revision: int = Field(ge=0)
    kind: EvidenceKind
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified: bool = False


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
    evidence_refs: tuple[EvidenceRef, ...] = ()
    evidence_kind: EvidenceKind
    confidence: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    source_model: str = Field(min_length=1, max_length=128)
    valid: bool = True

    @model_validator(mode="after")
    def validate_extent(self) -> SemanticSegment:
        if self.end_pts <= self.start_pts:
            raise ValueError("end_pts must be greater than start_pts")
        return self

    def overlaps(self, *, start_pts: int, end_pts: int) -> bool:
        return self.start_pts < end_pts and start_pts < self.end_pts


class SegmentResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str
    candidates: tuple[SemanticSegment, ...] = ()
    selected: SemanticSegment | None = None
    clarification_required: bool = False
    reason: str | None = None


def _round_half_up(value: Fraction) -> int:
    if value < 0:
        return -_round_half_up(-value)
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def pts_to_microseconds(*, pts: int, timebase_num: int, timebase_den: int) -> int:
    """Convert PTS using ``seconds = pts * num / den`` without floats."""
    if pts < 0 or timebase_num <= 0 or timebase_den <= 0:
        raise ValueError("PTS and timebase must be non-negative/positive")
    return _round_half_up(Fraction(pts * timebase_num * 1_000_000, timebase_den))


def pts_with_delta_to_microseconds(
    *, pts: int, delta_microseconds: int, timebase_num: int, timebase_den: int
) -> int:
    """Convert a PTS endpoint plus a microsecond delta before one rounding."""
    if pts < 0 or timebase_num <= 0 or timebase_den <= 0:
        raise ValueError("PTS and timebase must be non-negative/positive")
    value = Fraction(pts * timebase_num * 1_000_000, timebase_den) + delta_microseconds
    if value < 0:
        raise ValueError("converted coordinate must be non-negative")
    return _round_half_up(value)


def microseconds_to_pts(*, microseconds: int, timebase_num: int, timebase_den: int) -> int:
    """Convert microseconds to the nearest PTS tick with explicit rounding."""
    if microseconds < 0 or timebase_num <= 0 or timebase_den <= 0:
        raise ValueError("microseconds and timebase must be non-negative/positive")
    return _round_half_up(Fraction(microseconds * timebase_den, timebase_num * 1_000_000))


def resolve_segment(
    *,
    label: str,
    project_id: str,
    tenant_id: str,
    timeline_revision: int,
    candidates: list[SemanticSegment],
    min_confidence: float = 0.7,
) -> SegmentResolution:
    if timeline_revision < 0:
        return SegmentResolution(
            label=label, clarification_required=True, reason="invalid_revision"
        )
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
