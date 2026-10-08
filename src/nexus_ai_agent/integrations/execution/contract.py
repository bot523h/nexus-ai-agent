"""The provider-agnostic Execution Fabric contract (task-255).

The Execution Fabric is the *seam* between Nexus — which owns the truth, the
authority and the fenced commit — and any external workflow-execution provider
(Hatchet today; Trigger/Temporal/Inngest/Windmill later). It is deliberately
tiny and **provider-neutral**: nothing in this module may import a provider SDK
or any Nexus *authority* module (queue, bus, verifier, passport, provenance).

Identity separation (rule 8 — kept explicit, never collapsed):

=================  =====================================================
``request_id``     the canonical Nexus request identity (stable across every
                   provider retry of the same request)
``idempotency_key``the durable collapse key (job identity) — Nexus-owned
``job_id``         the durable queue row id — Nexus-owned
``try_id``         the Nexus *attempt* identity (one per fencing token)
``fencing_token``  the Nexus execution claim (the queue row's ``attempt``)
``backend``        the provider kind (``in_process`` / ``hatchet`` / ...)
``provider_run_id``the provider's own run handle — opaque, evidence-only
=================  =====================================================

Provider retry count is **never** a Nexus authority field (rule 9 / PHASE E):
it is carried as :class:`ExecutionAttempt.provider_attempt` and never overrides
``fencing_token``. A result that does not match the identity the caller is
waiting for is a *stale* result and must fail closed (rule 7) — it may not
commit anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "BackendKind",
    "OutcomeState",
    "ExecutionIdentity",
    "ExecutionRequest",
    "ExecutionAttempt",
    "ExecutionResult",
    "ExecutionFailure",
    "identity_matches",
]


class BackendKind(str, Enum):
    """Which execution provider handles a request (evidence, never authority)."""

    IN_PROCESS = "in_process"
    HATCHET = "hatchet"


class OutcomeState(str, Enum):
    """The provider-neutral verdict of one execution attempt.

    ``SUCCEEDED`` is a *claim* that a downstream verifier still has to check —
    the fabric never turns it into job success. ``UNKNOWN`` is the fail-closed
    answer for a lost/missing/indeterminate provider result (rule 9): it is
    neither success nor terminal failure until Nexus reconciles it.
    """

    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ExecutionIdentity:
    """The full Nexus identity of one execution — provider-independent.

    ``provider_run_id`` is filled in only after the provider accepts the work;
    it is evidence, never an authority key, and it never replaces a Nexus id.
    """

    request_id: str
    idempotency_key: str
    job_id: str
    try_id: str
    fencing_token: int
    backend: BackendKind
    provider_run_id: str | None = None

    def __post_init__(self) -> None:
        _require(self.request_id, "ExecutionIdentity.request_id")
        _require(self.idempotency_key, "ExecutionIdentity.idempotency_key")
        _require(self.job_id, "ExecutionIdentity.job_id")
        _require(self.try_id, "ExecutionIdentity.try_id")
        if self.fencing_token < 1:
            raise ValueError("ExecutionIdentity.fencing_token must be >= 1")
        if self.provider_run_id is not None:
            _require(self.provider_run_id, "ExecutionIdentity.provider_run_id")

    def with_provider_run(self, provider_run_id: str) -> ExecutionIdentity:
        """Return a copy carrying the provider run handle (evidence only)."""
        return ExecutionIdentity(
            request_id=self.request_id,
            idempotency_key=self.idempotency_key,
            job_id=self.job_id,
            try_id=self.try_id,
            fencing_token=self.fencing_token,
            backend=self.backend,
            provider_run_id=provider_run_id,
        )

    def to_metadata(self) -> dict[str, Any]:
        """Provider-neutral metadata stamped onto the provider run."""
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "job_id": self.job_id,
            "try_id": self.try_id,
            "fencing_token": self.fencing_token,
            "backend": self.backend.value,
        }


def identity_matches(expected: ExecutionIdentity, candidate: ExecutionIdentity) -> bool:
    """Is ``candidate`` the identity the caller is still waiting for?

    The authority-bearing part is ``(job_id, fencing_token)`` — the same CAS the
    queue uses. ``provider_run_id`` is deliberately ignored (it is evidence).
    A mismatch is a stale/superseded result and must not commit (rule 7).
    """
    return (
        expected.job_id == candidate.job_id
        and expected.fencing_token == candidate.fencing_token
        and expected.request_id == candidate.request_id
    )


@dataclass(frozen=True)
class ExecutionRequest:
    """A decided, already-fenced request handed to a provider for execution.

    The payload is a **canonical** Nexus payload (e.g. the exact dict a
    ``creative_render`` queue row carries). The provider executes it in
    ``staging_dir`` — a per-attempt directory the provider may write to but
    which is *not* the final artifact destination (rule 10). Publication to the
    final destination and verification stay in Nexus.
    """

    identity: ExecutionIdentity
    operation: str
    payload: Mapping[str, Any]
    staging_dir: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.operation, "ExecutionRequest.operation")
        _require(self.staging_dir, "ExecutionRequest.staging_dir")


@dataclass(frozen=True)
class ExecutionAttempt:
    """One provider attempt. Not a Nexus try — see the module docstring."""

    identity: ExecutionIdentity
    provider_attempt: int

    def __post_init__(self) -> None:
        if self.provider_attempt < 1:
            raise ValueError("ExecutionAttempt.provider_attempt must be >= 1")


@dataclass(frozen=True)
class ExecutionResult:
    """A raw provider witness. It is data to verify, never a signed verdict."""

    identity: ExecutionIdentity
    attempt: ExecutionAttempt
    state: OutcomeState
    witness: Mapping[str, Any] = field(default_factory=dict)
    staged_artifact_path: str | None = None
    artifact_sha256: str | None = None

    @property
    def is_success_claim(self) -> bool:
        return self.state is OutcomeState.SUCCEEDED


@dataclass(frozen=True)
class ExecutionFailure:
    """A typed, provider-neutral failure. ``retryable`` is advisory only."""

    identity: ExecutionIdentity
    code: str
    detail: str = ""
    retryable: bool = False
    state: OutcomeState = OutcomeState.FAILED

    def __post_init__(self) -> None:
        _require(self.code, "ExecutionFailure.code")
