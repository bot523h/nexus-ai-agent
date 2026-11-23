"""Pure contracts for durable plan deduplication.

SQLite and filesystem access live in the infrastructure adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from nexus_ai_agent.creative.studio.plan_identifiers import validate_plan_identity


class PlanReplayConflictError(ValueError):
    """An existing plan identity has a different fingerprint."""


class PlanInDoubtQuarantineError(RuntimeError):
    """An existing plan may have performed effects and must not be replayed."""


class PlanStoreCapacityError(RuntimeError):
    """The bounded store cannot accept another durable identity."""


class PlanStoreStateError(RuntimeError):
    """A requested transition is not permitted."""


class PlanExecutionInProgressError(PlanStoreStateError):
    """A live execution owns the requested plan identity."""


class PlanFencingTokenMismatchError(PlanStoreStateError):
    """An execution attempt cannot authorize this state transition."""


@dataclass(frozen=True)
class ExecutionLease:
    """Persisted, project-bound ownership of one execution attempt."""

    project_id: str
    plan_id: str
    plan_hash: str
    attempt: int
    owner_token: str
    lease_expires_at: float

    @property
    def fencing_epoch(self) -> int:
        """Monotonically increasing attempt number for this plan identity."""
        return self.attempt


@dataclass(frozen=True)
class DurablePlanRecord:
    plan_id: str
    plan_hash: str
    status: str
    external_effects: bool
    evidence_json: str | None


@dataclass(frozen=True)
class CompensationJournalEntry:
    """A durable, attempt-scoped compensation intent and its observed outcome."""

    step_id: str
    effect_key: str
    attempt: int
    status: str
    evidence_json: str | None


@dataclass(frozen=True)
class ApprovalBinding:
    """Claims binding an approval to a specific actor, project and plan.

    Construction validates shape only, not authenticity. A composition-root
    verifier must authenticate the approval and principal independently.
    Timestamps are finite Unix-epoch seconds, not monotonic deadlines.
    """

    project_id: str
    plan_id: str
    plan_hash: str
    actor_id: str
    approval_id: str
    step_id: str
    capability_version: str
    normalized_params_hash: str
    policy_version: str
    nonce: str
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "plan_id",
            "actor_id",
            "approval_id",
            "step_id",
            "capability_version",
            "policy_version",
            "nonce",
        ):
            validate_plan_identity(getattr(self, name), name=name)
        for name in ("plan_hash", "normalized_params_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be lowercase SHA-256 hex")
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite epoch timestamp")
            if not -float("inf") < value < float("inf"):
                raise ValueError(f"{name} must be a finite epoch timestamp")
            try:
                float(value)
            except OverflowError as exc:
                raise ValueError(f"{name} must be a finite epoch timestamp") from exc
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must follow issuance")


@dataclass(frozen=True)
class PlanApprovalRequirement:
    """Trusted bus-derived approval scope; this is not itself an approval."""

    project_id: str
    plan_id: str
    plan_hash: str
    actor_id: str
    step_id: str
    capability_version: str
    normalized_params_hash: str
    policy_version: str


class ApprovalVerifier(Protocol):
    """Trusted application verifier: authenticate the actor and approval claims."""

    def verify(self, binding: ApprovalBinding) -> bool: ...


class DurablePlanDedupePort(Protocol):
    """Persistence seam; its fence protects store transitions, not side effects."""

    def reserve(
        self, plan_id: str, plan_hash: str, *, external_effects: bool, project_id: str = "default"
    ) -> DurablePlanRecord: ...

    def reserve_and_start(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        external_effects: bool,
        nonce: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
        additional_nonces: tuple[str, ...] = (),
    ) -> ExecutionLease: ...

    def replay_committed(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        external_effects: bool,
        nonce: str,
        project_id: str = "default",
        additional_nonces: tuple[str, ...] = (),
    ) -> DurablePlanRecord: ...

    def start(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        nonce: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
    ) -> ExecutionLease: ...

    def heartbeat(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
    ) -> ExecutionLease: ...

    def finish(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        evidence: Any,
        project_id: str = "default",
    ) -> None: ...

    def mark_compensating(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
    ) -> None: ...

    def record_compensation_intent(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        project_id: str = "default",
    ) -> None: ...

    def record_compensation_step(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        succeeded: bool,
        evidence: Any,
        project_id: str = "default",
    ) -> None: ...

    def compensation_journal(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        project_id: str = "default",
    ) -> tuple[CompensationJournalEntry, ...]: ...

    def record_compensation(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        succeeded: bool,
        evidence: Any,
        project_id: str = "default",
    ) -> None: ...

    def quarantine(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
    ) -> None: ...


class ApprovalBindingError(RuntimeError):
    """An approval is untrusted, expired, or bound to a different plan."""


def verify_approval_binding(
    binding: ApprovalBinding,
    *,
    project_id: str,
    plan_id: str,
    plan_hash: str,
    actor_id: str,
    step_id: str,
    capability_version: str,
    normalized_params_hash: str,
    policy_version: str,
    now: float,
    verifier: ApprovalVerifier,
) -> None:
    """Validate binding and require independent trusted verification.

    Expected fields, especially actor_id and policy_version, must come from
    authenticated request context and the authoritative registry, not from the
    binding itself. This does not consume its nonce; callers must do so
    atomically in the durable execution transaction, before any side effect.
    """
    if not isinstance(binding, ApprovalBinding):
        raise ApprovalBindingError("invalid approval binding")
    if (
        binding.project_id != project_id
        or binding.plan_id != plan_id
        or binding.plan_hash != plan_hash
        or binding.actor_id != actor_id
        or binding.step_id != step_id
        or binding.capability_version != capability_version
        or binding.normalized_params_hash != normalized_params_hash
        or binding.policy_version != policy_version
    ):
        raise ApprovalBindingError("approval does not match the plan")
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not -float("inf") < now < float("inf")
        or not binding.issued_at <= now < binding.expires_at
    ):
        raise ApprovalBindingError("approval is not currently valid")
    if verifier.verify(binding) is not True:
        raise ApprovalBindingError("approval authority refused the binding")
