"""Provider-neutral Execution Contract (NEXUS V1).

This module is the **intent** half of the execution core: it describes what a
business request *is*, what identity it carries, how it may fail, and what a
backend must be able to answer — without naming any provider, queue, database,
or storage engine.

Authority law (unchanged by this module)
----------------------------------------
The contract carries **no authority**.  The single execution authority in this
repository remains :class:`~nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue`
(the durable SQLite row); the fencing model remains the per-row ``attempt``
counter; and the only authoritative completion path remains the queue-owned,
fenced, atomic ``_mark_completed`` compare-and-set.  An
:class:`ExecutionBackend` implementation *delegates* to that authority — it
never creates a second one (no second queue, no second persistence, no second
commit authority, no second verifier).

Four verbs, never ``execute``
-----------------------------
The contract exposes exactly four verbs — :meth:`ExecutionBackend.submit`,
:meth:`ExecutionBackend.observe`, :meth:`ExecutionBackend.cancel`,
:meth:`ExecutionBackend.reconcile` — and **no** combined ``execute``.  A single
``execute`` that submitted, polled, verified and committed in one call would
collapse the intent/authority separation the rest of this repository enforces
(execution success ≠ job success; only the queue may complete a job), so it is
forbidden by construction and asserted by
``tests/architecture/test_execution_contract_boundary.py``.

Identity separation
-------------------
The identities below are deliberately distinct and must never be conflated:

``request_id`` / ``idempotency_key``
    the *Nexus* business request identity (idempotency stays Nexus-owned);
``job_id``
    the one authoritative durable job row for that request;
``attempt_id`` / ``fencing_token``
    the per-attempt execution identity and its fencing token (the ``attempt``
    counter).  A Nexus *attempt* is **not** a provider retry: a provider's own
    internal retry must never mint a new Nexus fencing token;
``worker_id`` / ``backend``
    which execution substrate ran the attempt;
``provider_run_id``
    an *observation* from a provider.  It is never authority — the queue row
    is the authority, and this field can never be used to complete a job.

Failure model
-------------
:class:`FailureDisposition` is an **execution-layer projection**, not a second
business failure taxonomy.  The single business taxonomy remains
:class:`~nexus_ai_agent.jobs.failure_semantics.FailureClass`
(``RETRYABLE`` / ``TERMINAL``); the bridge functions below project it.  The
projection additionally names the outcomes that are *not* business failures at
all — ``UNKNOWN`` (the world did not answer), ``CANCELLED`` (a process-lifecycle
event), ``STALE`` (a superseded attempt) — and these must never be recorded as a
terminal business failure.  Only ``NON_RETRYABLE`` is terminal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.failure_semantics import FailureClass

__all__ = [
    "ExecutionBackend",
    "ExecutionContext",
    "ExecutionFailure",
    "ExecutionIdentity",
    "ExecutionObservation",
    "ExecutionPolicy",
    "ExecutionRequest",
    "ExecutionResult",
    "FailureDisposition",
    "ObservationState",
    "RetryPolicy",
    "disposition_from_failure_class",
    "disposition_from_status",
    "failure_from_status",
    "observation_state_from_status",
    "stale_failure",
    "unknown_failure",
]


def _freeze(payload: Mapping[str, object] | None) -> Mapping[str, object]:
    """Return an immutable, read-only view of a caller-owned mapping."""
    return MappingProxyType(dict(payload or {}))


def _require_token(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


# --------------------------------------------------------------------------- #
# Failure model
# --------------------------------------------------------------------------- #
class FailureDisposition(str, Enum):
    """How an execution outcome may be treated — not a business taxonomy.

    ``RETRYABLE`` / ``NON_RETRYABLE`` project the existing
    :class:`FailureClass` business taxonomy.  ``UNKNOWN`` / ``CANCELLED`` /
    ``STALE`` are execution-layer outcomes that are explicitly **not** terminal
    business failures: ``UNKNOWN`` must be reconciled before any retry, a
    ``CANCELLED`` attempt is recoverable, and a ``STALE`` attempt was superseded
    by a newer fencing token.
    """

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    STALE = "stale"

    @property
    def is_terminal_business_failure(self) -> bool:
        """True only for a proven, non-retryable business failure.

        ``UNKNOWN`` and ``STALE`` (and ``CANCELLED`` / ``RETRYABLE``) are never
        terminal business failures — this is invariant I7.
        """
        return self is FailureDisposition.NON_RETRYABLE

    @property
    def retryable(self) -> bool:
        return self is FailureDisposition.RETRYABLE


@dataclass(frozen=True)
class ExecutionFailure:
    """A classified execution outcome that is not success."""

    disposition: FailureDisposition
    code: str
    message: str = ""

    def __post_init__(self) -> None:
        _require_token(self.code, field_name="failure code")

    @property
    def is_terminal_business_failure(self) -> bool:
        return self.disposition.is_terminal_business_failure


def disposition_from_failure_class(failure_class: FailureClass) -> FailureDisposition:
    """Project the single business taxonomy onto an execution disposition."""
    if failure_class is FailureClass.RETRYABLE:
        return FailureDisposition.RETRYABLE
    return FailureDisposition.NON_RETRYABLE


def disposition_from_status(status: JobStatus) -> FailureDisposition | None:
    """Project a durable failure status; ``None`` for a non-failure status."""
    if status is JobStatus.FAILED_RETRYABLE:
        return FailureDisposition.RETRYABLE
    if status is JobStatus.FAILED_TERMINAL:
        return FailureDisposition.NON_RETRYABLE
    return None


def failure_from_status(
    status: JobStatus, *, code: str, message: str = ""
) -> ExecutionFailure | None:
    disposition = disposition_from_status(status)
    if disposition is None:
        return None
    return ExecutionFailure(disposition=disposition, code=code, message=message)


def unknown_failure(code: str = "unknown", message: str = "") -> ExecutionFailure:
    """An execution whose fate the substrate could not answer.

    Never terminal: a caller must ``reconcile`` before treating it as failed.
    """
    return ExecutionFailure(disposition=FailureDisposition.UNKNOWN, code=code, message=message)


def stale_failure(code: str = "stale_execution", message: str = "") -> ExecutionFailure:
    """A superseded attempt (its fencing token is no longer current)."""
    return ExecutionFailure(disposition=FailureDisposition.STALE, code=code, message=message)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecutionIdentity:
    """The separated identities of one execution (see module docstring)."""

    request_id: str
    idempotency_key: str
    job_id: str
    attempt_id: str | None = None
    fencing_token: int | None = None
    worker_id: str | None = None
    backend: str = "native_local"
    provider_run_id: str | None = None

    def __post_init__(self) -> None:
        _require_token(self.request_id, field_name="request_id")
        _require_token(self.idempotency_key, field_name="idempotency_key")
        _require_token(self.job_id, field_name="job_id")
        _require_token(self.backend, field_name="backend")
        if self.attempt_id is not None:
            _require_token(self.attempt_id, field_name="attempt_id")
        if self.fencing_token is not None:
            # Malformed identities fail closed at construction: the token is
            # the attempt fence, so ``bool``/``float``/``str`` impostors must
            # never reach the authority (``True`` would otherwise alias 1).
            if type(self.fencing_token) is not int:
                raise ValueError("fencing_token must be an int when present")
            if self.fencing_token < 1:
                raise ValueError("fencing_token must be >= 1 when present")
        if self.provider_run_id is not None:
            _require_token(self.provider_run_id, field_name="provider_run_id")

    @property
    def has_fencing_token(self) -> bool:
        return self.fencing_token is not None

    def with_attempt(self, *, attempt_id: str, fencing_token: int) -> ExecutionIdentity:
        """Return a copy bound to a concrete attempt (never mutates in place)."""
        return ExecutionIdentity(
            request_id=self.request_id,
            idempotency_key=self.idempotency_key,
            job_id=self.job_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            worker_id=self.worker_id,
            backend=self.backend,
            provider_run_id=self.provider_run_id,
        )


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryPolicy:
    """Whether the *Nexus* job may be re-attempted.

    A re-attempt mints a strictly higher fencing token.  This policy never
    describes a provider's internal retry: a provider retry must not generate a
    new Nexus attempt (invariant I2).
    """

    max_attempts: int = 1
    retry_on: frozenset[FailureDisposition] = field(
        default_factory=lambda: frozenset({FailureDisposition.RETRYABLE})
    )
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")
        if FailureDisposition.NON_RETRYABLE in self.retry_on:
            raise ValueError("a non-retryable disposition must never be in retry_on")

    def allows_retry(self, disposition: FailureDisposition) -> bool:
        return disposition in self.retry_on


@dataclass(frozen=True)
class ExecutionPolicy:
    """Declarative execution policy for one request."""

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: float | None = None
    requires_verification: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0 when present")


# --------------------------------------------------------------------------- #
# Request / context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecutionRequest:
    """A provider-neutral request to execute one unit of work."""

    job_type: str
    idempotency_key: str
    payload: Mapping[str, object] = field(default_factory=dict)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)

    def __post_init__(self) -> None:
        _require_token(self.job_type, field_name="job_type")
        _require_token(self.idempotency_key, field_name="idempotency_key")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class ExecutionContext:
    """The immutable context bound to a submitted execution."""

    identity: ExecutionIdentity
    policy: ExecutionPolicy
    submitted_at: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_token(self.submitted_at, field_name="submitted_at")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


# --------------------------------------------------------------------------- #
# Observation / result
# --------------------------------------------------------------------------- #
class ObservationState(str, Enum):
    """The provider-neutral state of an observed execution."""

    PENDING = "pending"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ObservationState.SUCCEEDED,
            ObservationState.FAILED,
            ObservationState.CANCELLED,
        }


def observation_state_from_status(status: JobStatus) -> ObservationState:
    """Project a durable :class:`JobStatus` onto an observation state."""
    return {
        JobStatus.PENDING: ObservationState.PENDING,
        JobStatus.PROCESSING: ObservationState.PROCESSING,
        JobStatus.VERIFYING: ObservationState.VERIFYING,
        JobStatus.COMPLETED: ObservationState.SUCCEEDED,
        JobStatus.FAILED_RETRYABLE: ObservationState.FAILED,
        JobStatus.FAILED_TERMINAL: ObservationState.FAILED,
    }[status]


@dataclass(frozen=True)
class ExecutionObservation:
    """A point-in-time, provider-neutral view of an execution."""

    identity: ExecutionIdentity
    state: ObservationState
    result: Mapping[str, object] | None = None
    failure: ExecutionFailure | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", None if self.result is None else _freeze(self.result))

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal


@dataclass(frozen=True)
class ExecutionResult:
    """The terminal outcome of an execution, with verification evidence.

    ``verification`` is the independent verifier's evidence block (or ``None``
    when no verifier is registered for the job type).  An execution result is
    never itself verified evidence — the verifier stays independent
    (invariant I6).
    """

    identity: ExecutionIdentity
    state: ObservationState
    result: Mapping[str, object] | None = None
    failure: ExecutionFailure | None = None
    verification: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", None if self.result is None else _freeze(self.result))
        object.__setattr__(
            self, "verification", None if self.verification is None else _freeze(self.verification)
        )


# --------------------------------------------------------------------------- #
# The backend contract
# --------------------------------------------------------------------------- #
@runtime_checkable
class ExecutionBackend(Protocol):
    """The provider-neutral execution substrate contract.

    Exactly four verbs.  There is deliberately **no** ``execute`` method: a
    combined submit+poll+verify+commit would violate the single-authority law.
    """

    async def submit(self, request: ExecutionRequest) -> ExecutionIdentity:
        """Accept a request and return its durable identity (idempotent)."""
        ...

    async def observe(self, identity: ExecutionIdentity) -> ExecutionObservation:
        """Return a point-in-time view; never mutates authority."""
        ...

    async def cancel(self, identity: ExecutionIdentity) -> bool:
        """Request cancellation; ``True`` iff an in-flight attempt was affected.

        Cancellation is **attempt-scoped**: the identity must carry its
        fencing token (``identity.fencing_token``), and the request may only
        affect the attempt that token names.  A stale identity (an older
        token) is rejected and must never cancel the current attempt; an
        unbound identity (``job_id`` without a fencing token) holds no
        cancellation authority at all — ``job_id`` alone is never sufficient.
        """
        ...

    async def reconcile(self, identity: ExecutionIdentity) -> ExecutionObservation:
        """Re-establish truth after a crash/unknown; never invents success."""
        ...
