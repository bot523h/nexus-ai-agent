"""Pure contracts for durable plan deduplication.

SQLite and filesystem access live in the infrastructure adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeVar

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
class ExternalEffectObservation:
    """Destination's observation, never an authorization or proof by itself."""

    effect_key: str
    fencing_epoch: int
    status: Literal["ABSENT", "PRESENT", "UNKNOWN"]
    evidence_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.effect_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.effect_key) is None
        ):
            raise ValueError("effect_key must be lowercase SHA-256 hex")
        if (
            isinstance(self.fencing_epoch, bool)
            or not isinstance(self.fencing_epoch, int)
            or self.fencing_epoch < 1
        ):
            raise ValueError("fencing_epoch must be a positive integer")
        if self.status not in ("ABSENT", "PRESENT", "UNKNOWN"):
            raise ValueError("invalid effect observation status")
        if self.status == "UNKNOWN":
            if self.evidence_digest != "":
                raise ValueError("unknown observations cannot claim evidence")
        elif (
            not isinstance(self.evidence_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.evidence_digest) is None
        ):
            raise ValueError("observed effects require a lowercase SHA-256 evidence digest")


class DurableEffectDestination(Protocol):
    """Trusted external destination contract; the bus cannot enforce its internals.

    Implementations must atomically reject stale epochs, bind each effect key
    to one payload, deduplicate redelivery, and expose crash-persistent
    observations. An ABSENT observation used in recovery must also ensure the
    observed effect cannot subsequently be published at or below that epoch. Only
    a separate verifier may authenticate an observation; a destination
    assertion alone is not proof. Structural conformance cannot prove these
    properties and registering this port never makes an arbitrary handler safe.
    """

    def apply(
        self, *, effect_key: str, fencing_epoch: int, payload: bytes
    ) -> ExternalEffectObservation: ...

    def inspect(self, *, effect_key: str, fencing_epoch: int) -> ExternalEffectObservation: ...


class EffectObservationVerifier(Protocol):
    """Independent, trusted verifier of a destination's observed effect."""

    def verify(self, observation: ExternalEffectObservation) -> bool: ...


class EffectCompensator(Protocol):
    """Trusted idempotent destination remediation; not request-supplied code."""

    def compensate(self, observation: ExternalEffectObservation) -> ExternalEffectObservation: ...


@dataclass(frozen=True)
class RecoveryRoute:
    """Trusted, deployment-configured destination and separate verifier.

    For ABSENT, the verifier must independently establish that the requested
    epoch is durably fenced. A route supplied by a request is not authority.
    """

    destination: DurableEffectDestination
    verifier: EffectObservationVerifier
    verifier_id: str
    compensator: EffectCompensator | None = None


@dataclass(frozen=True)
class VerifiedEffectRecoveryEvidence:
    step_id: str
    effect_key: str
    fencing_epoch: int
    observation_status: Literal["PRESENT", "ABSENT"]
    observation_digest: str
    verifier_id: str
    destination_fenced: bool


_RecoveryTime = TypeVar("_RecoveryTime")


@dataclass(frozen=True)
class RecoveryEvidence(Generic[_RecoveryTime]):
    """A store-issued snapshot; its nonce alone is neither proof nor authority.

    The generic timestamp permits the pure port to avoid a clock import; the
    SQLite adapter issues timezone-aware datetime instances.
    """

    project_id: str
    plan_id: str
    plan_hash: str
    expected_status: str
    expected_attempt: int
    operator_id: str
    recovery_nonce: str
    issued_at: _RecoveryTime
    expires_at: _RecoveryTime
    journal_digest: str
    effect_evidences: tuple[VerifiedEffectRecoveryEvidence, ...]


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

    def monotonic_ns(self) -> int: ...

    def utc_timestamp(self) -> float: ...

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

    def find_committed(
        self, plan_id: str, plan_hash: str, *, project_id: str
    ) -> DurablePlanRecord | None:
        """Look up evidence only after independently authorizing its disclosure."""
        ...

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
        deadline_monotonic_ns: int | None = None,
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

    def verify_effect_intent(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        verification_digest: str,
        project_id: str = "default",
        deadline_monotonic_ns: int | None = None,
    ) -> None:
        """Record a separately verified forward effect under the current fence."""
        ...

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

    def quarantined_journal(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        project_id: str = "default",
    ) -> tuple[CompensationJournalEntry, ...]: ...

    def inspect_recovery(
        self,
        project_id: str,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        operator_id: str,
    ) -> RecoveryEvidence[Any]: ...

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

    def repair_in_doubt(
        self,
        project_id: str,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        operator_id: str,
        resolution_action: str,
        reason: str,
        recovery_evidence: RecoveryEvidence[Any] | None = None,
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
