from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DirectorStatus(str, Enum):
    """Terminal result of a bounded, non-authoritative Director preview."""

    PREVIEW_READY = "preview_ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    REJECTED = "rejected"
    FAILED = "failed"


class DirectorFailureCode(str, Enum):
    INVALID_SCOPE = "invalid_scope"
    TENANT_SCOPE_UNAVAILABLE = "tenant_scope_unavailable"
    INCOMPLETE_SOURCE_EVIDENCE = "incomplete_source_evidence"
    NO_OPERATIONS = "no_operations"
    PROPOSAL_REFUSED = "proposal_refused"
    PROPOSAL_FAILED = "proposal_failed"
    PROPOSAL_TIMEOUT = "proposal_timeout"
    MALFORMED_PROPOSAL = "malformed_proposal"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SAFETY_VETO = "safety_veto"
    SAFETY_CHECK_TIMEOUT = "safety_check_timeout"
    SAFETY_CHECK_FAILED = "safety_check_failed"
    NO_CONSENSUS = "no_consensus"


class DirectorScope(BaseModel):
    """Host-bound project/subject scope; never populated from a proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)


class DirectorBudget(BaseModel):
    """Hard per-preview limits. Gateway/provider budgets remain host-owned too."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_candidates: int = Field(default=5, ge=1, le=5)
    max_total_calls: int = Field(default=5, ge=1, le=5)
    max_review_rounds: Literal[3] = 3
    max_call_timeout_seconds: int = Field(default=2, ge=1, le=10)
    max_total_wall_clock_seconds: int = Field(default=10, ge=1, le=30)
    max_candidate_input_bytes: int = Field(default=32 * 1024, ge=1, le=128 * 1024)


class SafetyVetoDecision(BaseModel):
    """A negative-only host safety verdict; clear is not execution approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    veto: bool
    reason_code: str = Field(min_length=1, max_length=64)


class DirectorCandidatePreview(BaseModel):
    """Minimized ephemeral preview; excludes prompt, rationale and provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^candidate:[0-9a-f]{64}$")
    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any]
    support_count: int = Field(ge=1, le=5)
    vetoed: bool = False
    veto_code: str | None = Field(default=None, max_length=64)

    @field_validator("input")
    @classmethod
    def _finite_json_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("candidate input must be finite JSON data") from exc
        return value


class DirectorOutcome(BaseModel):
    """A preview result with no command, dispatch handle or execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DirectorStatus
    candidates: tuple[DirectorCandidatePreview, ...] = ()
    selected_candidate_id: str | None = None
    failure_code: DirectorFailureCode | None = None
    review_rounds: int = Field(default=0, ge=0, le=3)
    execution_authority: Literal["none"] = "none"
    dispatchable: Literal[False] = False

    @model_validator(mode="after")
    def _selection_is_non_authoritative(self) -> DirectorOutcome:
        candidate_ids = {candidate.candidate_id for candidate in self.candidates}
        if self.status is DirectorStatus.PREVIEW_READY:
            if (
                self.selected_candidate_id is None
                or self.selected_candidate_id not in candidate_ids
            ):
                raise ValueError("a ready preview must select one returned candidate")
            selected = next(
                item for item in self.candidates if item.candidate_id == self.selected_candidate_id
            )
            if selected.vetoed:
                raise ValueError("a vetoed candidate cannot be selected")
            if self.failure_code is not None:
                raise ValueError("a ready preview cannot carry a failure code")
        elif self.selected_candidate_id is not None:
            raise ValueError("a non-ready outcome cannot select a candidate")
        return self


__all__ = [
    "DirectorBudget",
    "DirectorCandidatePreview",
    "DirectorFailureCode",
    "DirectorOutcome",
    "DirectorScope",
    "DirectorStatus",
    "SafetyVetoDecision",
]
