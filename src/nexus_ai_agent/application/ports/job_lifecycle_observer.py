"""Observation port for the canonical Job lifecycle.

The queue (``adapters.in_process_job_queue``) owns the durable Job row and
every fenced transition.  Anything that wants to *witness* what the queue
committed — the causal evidence ledger is the first consumer — does so
through this port, and only through it:

* **post-commit only.**  Every event below is raised after the queue's
  compare-and-set for that transition returned ``True``.  An observer can
  therefore never see an intent the queue failed to make durable.
* **no authority.**  Observers receive facts; they cannot deny, delay or
  alter a transition.  The queue treats an observer failure as a decoding
  problem on the observer's side: it is logged, never propagated, and never
  re-opens a decided transition (see ``_observe`` in the queue).
* **no execution.**  This port carries no command, no path to execute and no
  capability: an observer cannot become a second execution path.

The events are deliberately narrow and already-redacted: identities are
logical (job id, attempt/fencing token), and the payload/result travel as
canonical digests plus the raw mapping for the observer to whitelist.  A
listener that stores facts is responsible for storing *only* whitelisted,
non-sensitive keys — the ledger implementation does exactly that
(``causal.observer``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class JobEnqueued:
    """A job row became durable (or an existing row absorbed this request)."""

    job_id: str
    job_type: str
    idempotency_key: str
    payload: dict[str, object]
    created: bool
    payload_conflict: bool


@dataclass(frozen=True)
class JobReserved:
    """The ``PENDING → RUNNING`` reservation CAS committed.

    ``attempt`` is the fencing token this execution minted; every later
    worker-owned event carries the same token, so a ledger can prove which
    execution made which claim (and that a rejected token claimed nothing).
    """

    job_id: str
    job_type: str
    attempt: int


@dataclass(frozen=True)
class JobReservationRejected:
    """A reservation CAS was refused — the row belongs to someone else.

    This is the durable, observable face of "a stale worker tried": the
    attempt that was rejected executed nothing (no side effect is possible
    before a winning reservation).
    """

    job_id: str
    job_type: str


@dataclass(frozen=True)
class ExecutionFinished:
    """The handler returned (or its failure was classified) — nothing trusted yet."""

    job_id: str
    job_type: str
    attempt: int
    result: dict[str, object] | None
    error: str | None
    typed_failure_code: str | None


@dataclass(frozen=True)
class VerificationFinished:
    """The independent artifact re-measurement answered."""

    job_id: str
    job_type: str
    attempt: int
    ok: bool
    reason_code: str | None
    block: dict[str, object]
    published: bool


@dataclass(frozen=True)
class JobSettled:
    """A terminal state committed: ``completed`` or one of the failure states."""

    job_id: str
    job_type: str
    attempt: int
    status: str
    result: dict[str, object] | None
    error: str | None


@runtime_checkable
class JobLifecycleObserverPort(Protocol):
    """Post-commit witness of the canonical Job lifecycle.

    Implementations must be safe to call from the event loop: the queue
    invokes them through ``asyncio.to_thread`` when they are synchronous
    callables, and every implementation is expected to be fail-safe (raise
    only on genuine internal errors; the queue logs and continues).
    """

    def on_enqueued(self, event: JobEnqueued) -> None: ...

    def on_reserved(self, event: JobReserved) -> None: ...

    def on_reservation_rejected(self, event: JobReservationRejected) -> None: ...

    def on_execution_finished(self, event: ExecutionFinished) -> None: ...

    def on_verification_finished(self, event: VerificationFinished) -> None: ...

    def on_settled(self, event: JobSettled) -> None: ...


__all__ = [
    "ExecutionFinished",
    "JobEnqueued",
    "JobLifecycleObserverPort",
    "JobReservationRejected",
    "JobReserved",
    "JobSettled",
    "VerificationFinished",
]
