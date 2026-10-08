"""``NativeLocalBackend`` — the first :class:`ExecutionBackend` implementation.

It is a *thin* adapter over the single execution authority,
:class:`~nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue`.  It
creates **no** second queue, **no** persistence, **no** verifier and **no**
commit authority: every verb delegates to the existing durable SQLite row, the
existing lifecycle, the existing worker, and the existing fenced atomic
completion.  The queue is injected, never constructed here (invariant I9).

Verb mapping
------------
``submit``
    ``queue.enqueue`` (idempotency-keyed; exact duplicate delivery reuses the
    one row) → the durable identity, read back from the row.
``observe``
    ``queue.get_job_facts`` → a provider-neutral :class:`ExecutionObservation`.
    An unreadable row status is reported as ``UNKNOWN`` — never guessed.
``cancel``
    ``queue.cancel`` (the additive, fenced cancellation primitive): an
    in-flight attempt is reset to ``pending`` under its current fencing token,
    so the cancelled attempt's later completion is rejected by the fenced CAS.
    A terminal row is never reopened.
``reconcile``
    observe; if the row is in-flight, reclaim orphaned rows via
    ``queue.resume_pending`` (the only sanctioned ownership transfer) and
    observe again.  Reconciliation never invents success.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from nexus_ai_agent.execution.contract import (
    ExecutionIdentity,
    ExecutionObservation,
    ExecutionRequest,
    ObservationState,
    failure_from_status,
    observation_state_from_status,
    unknown_failure,
)
from nexus_ai_agent.jobs.creative_passport import attempt_id as _attempt_id
from nexus_ai_agent.jobs.creative_passport import request_identity as _request_identity
from nexus_ai_agent.jobs.lifecycle import parse_job_status

if TYPE_CHECKING:  # pragma: no cover - typing only; no runtime adapter import
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

__all__ = ["NativeLocalBackend"]

_IN_FLIGHT = frozenset({ObservationState.PROCESSING, ObservationState.VERIFYING})


class NativeLocalBackend:
    """Provider-neutral backend over the in-process durable job queue."""

    def __init__(
        self,
        queue: InProcessJobQueue,
        *,
        backend_name: str = "native_local",
        worker_id: str | None = None,
        stale_after: timedelta | None = None,
    ) -> None:
        if queue is None:
            raise ValueError("NativeLocalBackend requires an existing job queue")
        self._queue = queue
        self._backend_name = backend_name
        self._worker_id = worker_id
        self._stale_after = stale_after

    # -- identity -------------------------------------------------------- #
    def _identity_from_facts(
        self, job_id: str, idempotency_key: str, facts: object | None
    ) -> ExecutionIdentity:
        if facts is None:
            return ExecutionIdentity(
                request_id=job_id,
                idempotency_key=idempotency_key,
                job_id=job_id,
                backend=self._backend_name,
                worker_id=self._worker_id,
            )
        request_id = getattr(facts, "request_id", None)
        if not request_id:
            request_id = _request_identity(
                str(getattr(facts, "job_type", "")),
                idempotency_key,
                dict(getattr(facts, "payload", {}) or {}),
            ).request_id
        attempt = int(getattr(facts, "attempt", 0) or 0)
        return ExecutionIdentity(
            request_id=str(request_id),
            idempotency_key=idempotency_key,
            job_id=job_id,
            attempt_id=_attempt_id(job_id, attempt) if attempt >= 1 else None,
            fencing_token=attempt if attempt >= 1 else None,
            worker_id=self._worker_id,
            backend=self._backend_name,
        )

    # -- verbs ----------------------------------------------------------- #
    async def submit(self, request: ExecutionRequest) -> ExecutionIdentity:
        job_id = await self._queue.enqueue(
            job_type=request.job_type,
            idempotency_key=request.idempotency_key,
            payload=dict(request.payload),
        )
        facts = await self._queue.get_job_facts_async(job_id)
        return self._identity_from_facts(job_id, request.idempotency_key, facts)

    async def observe(self, identity: ExecutionIdentity) -> ExecutionObservation:
        facts = await self._queue.get_job_facts_async(identity.job_id)
        if facts is None:
            return ExecutionObservation(
                identity=identity,
                state=ObservationState.UNKNOWN,
                failure=unknown_failure("job_not_found", f"no durable row for {identity.job_id}"),
            )
        if not getattr(facts, "status_known", True):
            return ExecutionObservation(
                identity=self._identity_from_facts(
                    identity.job_id, identity.idempotency_key, facts
                ),
                state=ObservationState.UNKNOWN,
                failure=unknown_failure(
                    "status_unreadable", f"unreadable status {getattr(facts, 'status', None)!r}"
                ),
            )
        status = parse_job_status(str(facts.status))
        state = observation_state_from_status(status)
        bound = self._identity_from_facts(identity.job_id, identity.idempotency_key, facts)
        result = facts.result if state is ObservationState.SUCCEEDED else None
        failure = failure_from_status(
            status, code=str(facts.error or "failed"), message=str(facts.error or "")
        )
        return ExecutionObservation(identity=bound, state=state, result=result, failure=failure)

    async def cancel(self, identity: ExecutionIdentity) -> bool:
        return await self._queue.cancel(identity.job_id)

    async def reconcile(self, identity: ExecutionIdentity) -> ExecutionObservation:
        observation = await self.observe(identity)
        if observation.state in _IN_FLIGHT:
            # The only sanctioned ownership transfer: reclaim orphaned rows,
            # which mints a strictly higher fencing token on the next
            # reservation and rejects the stale attempt's later completion.
            await self._queue.resume_pending(stale_after=self._stale_after)
            observation = await self.observe(identity)
        return observation
