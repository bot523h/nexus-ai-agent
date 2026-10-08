"""NativeLocalBackend integration tests over the real durable queue (NEXUS V1).

These are *not* mock-only: every test drives two real ``InProcessJobQueue``
instances over one real SQLite sidecar (the bot process and the ``nexus jobs
resume`` CLI / a second process sharing the durable queue).  Each test names
the invariant it proves:

    I1  one business request  -> one authoritative Nexus job identity
    I3  only the current fencing token may authoritatively complete
    I4  a stale attempt can never produce authoritative completion
    I5  provider_run_id is never authority
    I7  UNKNOWN is not automatically FAILED
    I8  idempotency remains Nexus-owned
    I9  NativeLocalBackend creates no second queue
    I10 success notification occurs only after the authoritative commit
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue, JobCompletion
from nexus_ai_agent.adapters.native_local_backend import NativeLocalBackend
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.execution.contract import (
    ExecutionIdentity,
    ExecutionRequest,
    FailureDisposition,
    ObservationState,
)

pytestmark = pytest.mark.integration

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _row(db: Path, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT status, attempt, result_json FROM nexus_job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


async def _wait_status(queue: InProcessJobQueue, job_id: str, wanted: JobStatus) -> None:
    for _ in range(500):
        if await queue.get_status(job_id) is wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {wanted}: {await queue.get_status(job_id)}")


def _backdate_started_at(db: Path, job_id: str) -> None:
    """Make an in-flight row look long-orphaned (expiry-gated takeover idiom)."""
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )


async def _drain(queue: InProcessJobQueue, job_id: str) -> JobStatus:
    for _ in range(500):
        status = await queue.get_status(job_id)
        if status in TERMINAL:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class _Gate:
    """A handler whose N-th call parks on its own event (worker N)."""

    def __init__(self) -> None:
        self.calls = 0
        self.events: dict[int, asyncio.Event] = {}

    def event(self, call: int) -> asyncio.Event:
        return self.events.setdefault(call, asyncio.Event())

    async def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        call = self.calls
        await self.event(call).wait()
        return {"who": f"worker-{call}"}


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[JobStatus, object]] = []

    async def __call__(self, completion: JobCompletion) -> None:
        who = (completion.result or {}).get("who") if completion.result else None
        self.events.append((completion.status, who))

    def successes(self) -> list[object]:
        return [who for status, who in self.events if status is JobStatus.COMPLETED]


def _queues(
    db: Path, handler: Any, *, hook_b: Any = None
) -> tuple[InProcessJobQueue, InProcessJobQueue]:
    queue_a = InProcessJobQueue(db, artifact_verifiers={})
    queue_b = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=hook_b)
    queue_a.register_handler("fenced", handler)
    queue_b.register_handler("fenced", handler)
    return queue_a, queue_b


def _request(key: str = "k") -> ExecutionRequest:
    return ExecutionRequest(job_type="fenced", idempotency_key=key, payload={})


# --------------------------------------------------------------------------- #
# I1 / I8 — submit returns the one authoritative identity; idempotency collapses
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_submit_returns_the_authoritative_identity(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue, worker_id="w1")

    identity = await backend.submit(_request("idem-1"))
    assert identity.job_id
    assert identity.idempotency_key == "idem-1"
    assert identity.request_id
    assert identity.backend == "native_local"

    gate.event(1).set()
    assert await _drain(queue, identity.job_id) is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_submit_is_idempotent_on_the_nexus_key(tmp_path: Path) -> None:
    """I8: idempotency is Nexus-owned; a duplicate delivery reuses one row."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    first = await backend.submit(_request("same-key"))
    second = await backend.submit(_request("same-key"))
    assert first.job_id == second.job_id
    with sqlite3.connect(db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM nexus_job_queue").fetchone()[0]
    assert count == 1
    gate.event(1).set()
    await _drain(queue, first.job_id)


# --------------------------------------------------------------------------- #
# I7 — observe maps durable status; an unknown job is UNKNOWN, never FAILED
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_observe_projects_the_durable_status(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    mid = await backend.observe(identity)
    assert mid.state is ObservationState.PROCESSING
    assert not mid.is_terminal

    gate.event(1).set()
    await _drain(queue, identity.job_id)
    done = await backend.observe(identity)
    assert done.state is ObservationState.SUCCEEDED
    assert done.result == {"who": "worker-1"}


@pytest.mark.asyncio
async def test_observe_unknown_job_is_unknown_not_failed(tmp_path: Path) -> None:
    """I7: a missing row is UNKNOWN; it is never recorded as a terminal failure."""
    db = tmp_path / "jobs.sqlite3"
    queue, _ = _queues(db, _Gate())
    backend = NativeLocalBackend(queue)

    ghost = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="does-not-exist")
    observation = await backend.observe(ghost)
    assert observation.state is ObservationState.UNKNOWN
    assert observation.failure is not None
    assert observation.failure.disposition is FailureDisposition.UNKNOWN
    assert not observation.failure.is_terminal_business_failure


# --------------------------------------------------------------------------- #
# I5 — a provider run id is never authority
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_run_id_never_changes_observed_truth(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    claimed = ExecutionIdentity(
        request_id=identity.request_id,
        idempotency_key=identity.idempotency_key,
        job_id=identity.job_id,
        provider_run_id="provider-says-done",
    )
    observation = await backend.observe(claimed)
    assert observation.state is ObservationState.PROCESSING, "a provider claim is not authority"
    gate.event(1).set()
    await _drain(queue, identity.job_id)


# --------------------------------------------------------------------------- #
# I2 — a provider retry never mints a Nexus attempt
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_retry_never_mints_a_new_nexus_attempt(tmp_path: Path) -> None:
    """I2: a provider's internal retry is an observation, never a Nexus attempt."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 1

    # A provider retry (new provider run id, same Nexus job) observes the SAME
    # attempt and mints NO new fencing token.
    provider_retry = ExecutionIdentity(
        request_id=identity.request_id,
        idempotency_key=identity.idempotency_key,
        job_id=identity.job_id,
        provider_run_id="provider-retry-2",
    )
    observation = await backend.observe(provider_retry)
    assert observation.identity.fencing_token == 1
    assert observation.identity.provider_run_id is None, "provider id is never bound as authority"
    assert _row(db, identity.job_id)["attempt"] == 1

    # And a provider run id can never be turned into a fencing token.
    assert not provider_retry.has_fencing_token
    gate.event(1).set()
    await _drain(queue, identity.job_id)


# --------------------------------------------------------------------------- #
# I6 — verification is independent from execution
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handler_success_without_independent_verification_is_not_job_success(
    tmp_path: Path,
) -> None:
    """I6: execution success != verified evidence; a refusing verifier blocks success."""
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": True, "artifact_path": "claims-to-be-done"}

    def refusing_verifier(payload: dict[str, object], result: dict[str, object]) -> Any:
        from nexus_ai_agent.jobs.verification import VerificationOutcome

        return VerificationOutcome(
            ok=False,
            reason_code="missing_artifact",
            summary={"status": "failed", "reason_code": "missing_artifact"},
        )

    queue.register_handler("creative_render", handler)
    queue.register_artifact_verifier("creative_render", refusing_verifier)

    job_id = await queue.enqueue(job_type="creative_render", idempotency_key="k", payload={})
    for _ in range(500):
        if await queue.get_status(job_id) in TERMINAL:
            break
        await asyncio.sleep(0.01)
    # The handler claimed success, but the independent verifier refused: the job
    # is a failure, never COMPLETED.
    assert await queue.get_status(job_id) is not JobStatus.COMPLETED
    assert _row(db, job_id)["status"] != JobStatus.COMPLETED.value
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# Cancellation — attempt-scoped, fenced, recoverable, terminal rows never reopened
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_running_job_is_fenced_and_recoverable(tmp_path: Path) -> None:
    """I3/I4: cancelling resets *our* attempt to pending (recoverable);
    the cancelled worker's later completion cannot commit."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 1
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)

    assert await backend.cancel(current) is True
    await _wait_status(queue, identity.job_id, JobStatus.PENDING)
    assert _row(db, identity.job_id)["attempt"] == 1  # token unchanged; row recoverable

    # Release the (now cancelled) worker: it must not be able to complete.
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert await queue.get_status(identity.job_id) is JobStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_without_a_fencing_token_is_refused(tmp_path: Path) -> None:
    """P0-1: ``job_id`` alone is never cancellation authority (fail closed).

    An unbound identity (submit-time identity, no fencing token) cannot name
    the attempt it speaks for, so it must not cancel the current attempt.
    """
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    assert identity.fencing_token is None  # the submit-time identity is unbound

    assert await backend.cancel(identity) is False
    row = _row(db, identity.job_id)
    assert row["status"] == JobStatus.PROCESSING.value  # the attempt is untouched
    assert not queue._tasks[identity.job_id].cancelled()

    # The same job, presented with its current fencing token, cancels cleanly.
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(current) is True
    assert (await queue.get_status(identity.job_id)) is JobStatus.PENDING
    gate.event(1).set()
    await queue.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancel_is_safe_and_idempotent(tmp_path: Path) -> None:
    """A duplicate cancel never re-mutates state and never raises."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)

    assert await backend.cancel(current) is True
    assert await backend.cancel(current) is False  # duplicate: already pending
    assert await backend.cancel(current) is False  # and again: still safe
    row = _row(db, identity.job_id)
    assert row["status"] == JobStatus.PENDING.value
    assert row["attempt"] == 1
    gate.event(1).set()
    await queue.shutdown()


@pytest.mark.asyncio
async def test_cancel_terminal_job_is_refused(tmp_path: Path) -> None:
    """I3: a terminal row is never reopened (even by its own attempt token)."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    gate.event(1).set()
    await _drain(queue, identity.job_id)
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(current) is False
    assert await queue.get_status(identity.job_id) is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_unknown_job_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue, _ = _queues(db, _Gate())
    backend = NativeLocalBackend(queue)
    ghost = ExecutionIdentity(
        request_id="r", idempotency_key="k", job_id="nope", attempt_id="a#1", fencing_token=1
    )
    assert await backend.cancel(ghost) is False


@pytest.mark.asyncio
async def test_cancel_never_emits_a_success_notification(tmp_path: Path) -> None:
    """I10: no success notification without an authoritative commit."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    recorder = _Recorder()
    queue_a, queue_b = _queues(db, gate, hook_b=recorder)
    backend = NativeLocalBackend(queue_a)

    identity = await backend.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(current) is True
    await asyncio.sleep(0.05)
    assert recorder.successes() == []


# --------------------------------------------------------------------------- #
# I3 / I4 — the required race: attempt N late commit vs attempt N+1 takeover
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stale_attempt_cannot_complete_after_takeover(tmp_path: Path) -> None:
    """T1/T2 at the contract level: A owns attempt 1 and parks; B takes the row
    over (attempt 2); A's late completion is rejected, B's is accepted."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 1

    # Second process takes the orphaned row over -> a strictly higher token.
    assert await queue_b.resume_pending() == [identity.job_id]
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2
    assert gate.calls == 2

    # The stale worker (attempt 1) finishes late: its commit must be rejected.
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert await queue_b.get_status(identity.job_id) is JobStatus.PROCESSING
    assert _row(db, identity.job_id)["attempt"] == 2

    # The current owner (attempt 2) commits and wins.
    gate.event(2).set()
    assert await _drain(queue_b, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


# --------------------------------------------------------------------------- #
# Crash / recovery — reconcile reclaims an orphaned in-flight row
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reconcile_recovers_an_orphaned_in_flight_job(tmp_path: Path) -> None:
    """A process crashes mid-execution; a fresh process reconciles under an
    *explicit* stale policy and the job completes exactly once, under a higher
    fencing token.  (A default, policy-less reconcile never takes over.)"""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B", stale_after=timedelta(hours=1))

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 1

    # The orphaned row has been in flight "for two hours".
    _backdate_started_at(db, identity.job_id)

    # queue_b is a fresh process; reconcile must reclaim the orphaned row.
    # The reclaim itself leaves the row ``pending`` until the scheduled task
    # re-reserves it, so reconcile may report either the reclaimed ``pending``
    # or the freshly reserved ``processing`` — never a fabricated success.
    observation = await backend_b.reconcile(identity)
    assert observation.state in {ObservationState.PENDING, ObservationState.PROCESSING}
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2

    gate.event(1).set()  # the crashed worker's late finish (rejected)
    gate.event(2).set()  # the recovered worker (accepted)
    assert await _drain(queue_b, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


@pytest.mark.asyncio
async def test_reconcile_of_a_completed_job_invents_nothing(tmp_path: Path) -> None:
    """Reconciliation of a settled job returns the durable truth unchanged."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    gate.event(1).set()
    await _drain(queue, identity.job_id)
    observation = await backend.reconcile(identity)
    assert observation.state is ObservationState.SUCCEEDED
    assert observation.result == {"who": "worker-1"}


# --------------------------------------------------------------------------- #
# P0-2 — reconcile must observe by default; takeover needs an explicit policy
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reconcile_without_a_stale_policy_never_takes_over(tmp_path: Path) -> None:
    """Case A: no stale policy -> observe only; a live peer is never superseded."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B")  # no stale_after

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 1

    observation = await backend_b.reconcile(identity)
    assert observation.state is ObservationState.PROCESSING
    # No takeover: the attempt is untouched and the live worker still owns it.
    assert _row(db, identity.job_id)["attempt"] == 1
    assert _row(db, identity.job_id)["status"] == JobStatus.PROCESSING.value
    assert gate.calls == 1, "reconcile must not start a second execution"

    gate.event(1).set()
    assert await _drain(queue_a, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_a.get_result(identity.job_id) or {})["who"] == "worker-1"


@pytest.mark.asyncio
async def test_reconcile_with_explicit_stale_policy_recovers_the_job(tmp_path: Path) -> None:
    """Case B: an explicit stale window recovers a genuinely orphaned job."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B", stale_after=timedelta(hours=1))

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    _backdate_started_at(db, identity.job_id)

    observation = await backend_b.reconcile(identity)
    assert observation.state in {ObservationState.PENDING, ObservationState.PROCESSING}
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2  # successor minted

    gate.event(1).set()  # stale token rejected
    gate.event(2).set()  # current token succeeds
    assert await _drain(queue_b, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


class _TaggedGate:
    """A handler that parks per payload tag (independent per-job control)."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.events: dict[str, asyncio.Event] = {}

    def event(self, tag: str) -> asyncio.Event:
        return self.events.setdefault(tag, asyncio.Event())

    async def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        tag = str(payload.get("tag"))
        self.calls[tag] = self.calls.get(tag, 0) + 1
        await self.event(tag).wait()
        return {"who": tag}


@pytest.mark.asyncio
async def test_reconcile_of_one_job_never_touches_an_unrelated_job(tmp_path: Path) -> None:
    """Case C: job-scoped recovery; an unrelated in-flight job stays untouched."""
    db = tmp_path / "jobs.sqlite3"
    gate = _TaggedGate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")

    job_a = await backend_a.submit(
        ExecutionRequest(job_type="fenced", idempotency_key="A", payload={"tag": "A"})
    )
    job_b = await backend_a.submit(
        ExecutionRequest(job_type="fenced", idempotency_key="B", payload={"tag": "B"})
    )
    await _wait_status(queue_a, job_a.job_id, JobStatus.PROCESSING)
    await _wait_status(queue_a, job_b.job_id, JobStatus.PROCESSING)
    _backdate_started_at(db, job_a.job_id)  # only A is orphaned

    recovered = await queue_b.recover_job(job_a.job_id, stale_after=timedelta(hours=1))
    assert recovered == [job_a.job_id]
    await _wait_status(queue_b, job_a.job_id, JobStatus.PROCESSING)
    assert _row(db, job_a.job_id)["attempt"] == 2

    # Job B is in-flight and unrelated: it must remain exactly as it was.
    row_b = _row(db, job_b.job_id)
    assert row_b["attempt"] == 1
    assert row_b["status"] == JobStatus.PROCESSING.value

    gate.event("A").set()
    gate.event("B").set()
    assert await _drain(queue_b, job_a.job_id) is JobStatus.COMPLETED
    assert await _drain(queue_a, job_b.job_id) is JobStatus.COMPLETED
    assert (await queue_a.get_result(job_b.job_id) or {})["who"] == "B"


# --------------------------------------------------------------------------- #
# P0-2 continued — staleness must be *proven*; ambiguity fails closed
# --------------------------------------------------------------------------- #
def _set_started_at(db: Path, job_id: str, value: str | None) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            (value, job_id),
        )


@pytest.mark.asyncio
async def test_reconcile_with_policy_never_steals_a_fresh_live_job(tmp_path: Path) -> None:
    """A live peer's *fresh* in-flight row is never taken over — even when the
    reconciling backend carries an explicit stale window (the window is the
    staleness proof; a fresh row is by definition not stale)."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B", stale_after=timedelta(hours=1))

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)

    observation = await backend_b.reconcile(identity)
    assert observation.state is ObservationState.PROCESSING
    assert _row(db, identity.job_id)["attempt"] == 1
    assert _row(db, identity.job_id)["status"] == JobStatus.PROCESSING.value
    assert gate.calls == 1, "a live job must never be re-executed by reconcile"

    gate.event(1).set()
    assert await _drain(queue_a, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_a.get_result(identity.job_id) or {})["who"] == "worker-1"


@pytest.mark.asyncio
async def test_reconcile_refuses_an_in_flight_row_whose_age_is_unprovable(tmp_path: Path) -> None:
    """Ambiguity fails closed: a row missing ``started_at`` cannot *prove* it is
    stale, so an expiry-gated reconcile must refuse to take it over."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B", stale_after=timedelta(hours=1))

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    _set_started_at(db, identity.job_id, None)  # unprovable age

    observation = await backend_b.reconcile(identity)
    assert observation.state is ObservationState.PROCESSING
    assert _row(db, identity.job_id)["attempt"] == 1
    assert _row(db, identity.job_id)["status"] == JobStatus.PROCESSING.value
    assert await queue_b.recover_job(identity.job_id, stale_after=timedelta(hours=1)) == []
    assert gate.calls == 1

    gate.event(1).set()
    assert await _drain(queue_a, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_a.get_result(identity.job_id) or {})["who"] == "worker-1"


@pytest.mark.asyncio
async def test_recover_job_respects_the_stale_window_boundary(tmp_path: Path) -> None:
    """The stale window is a strict boundary: younger-or-equal rows are live,
    strictly-older rows are recoverable."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)

    # Inside the window (started 30 minutes ago; window is one hour): live.
    _set_started_at(
        db, identity.job_id, (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    )
    assert await queue_b.recover_job(identity.job_id, stale_after=timedelta(hours=1)) == []
    assert _row(db, identity.job_id)["attempt"] == 1

    # Strictly past the window (started two hours ago): provably stale.
    _set_started_at(
        db, identity.job_id, (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    )
    assert await queue_b.recover_job(identity.job_id, stale_after=timedelta(hours=1)) == [
        identity.job_id
    ]
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2

    gate.event(1).set()
    gate.event(2).set()
    assert await _drain(queue_b, identity.job_id) is JobStatus.COMPLETED
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


@pytest.mark.asyncio
async def test_reconcile_racing_the_live_owner_duplicates_nothing(tmp_path: Path) -> None:
    """Race: reconcile fires while the live worker is still executing *and*
    finishing.  Exactly one handler execution, exactly one authoritative
    completion, and the reconciler never fabricates a second run."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")
    backend_b = NativeLocalBackend(queue_b, worker_id="B", stale_after=timedelta(hours=1))

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)

    gate.event(1).set()  # let the live worker finish...
    observation, _ = await asyncio.gather(
        backend_b.reconcile(identity),
        _drain(queue_a, identity.job_id),
    )
    assert gate.calls == 1, "the handler must run exactly once"
    assert observation.state in {
        ObservationState.PROCESSING,
        ObservationState.VERIFYING,
        ObservationState.SUCCEEDED,
    }
    assert await queue_a.get_status(identity.job_id) is JobStatus.COMPLETED
    assert _row(db, identity.job_id)["attempt"] == 1  # no successor was minted
