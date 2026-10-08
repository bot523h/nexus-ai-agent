"""Explicit crash matrix C1–C8 for the NEXUS V1 execution core.

Crash *windows* are injected with real primitives — a controlled worker gate
(an ``asyncio.Event`` the handler parks on), a parked sync verifier, a direct
row mutation, or an abandoned queue instance — over a real file-backed SQLite
sidecar.  A "restart" is a fresh ``InProcessJobQueue`` over the same sidecar
(the bot process / the ``nexus jobs resume`` CLI sharing one durable queue).
Every case asserts its exact post-condition; no crash is faked with a comment.

C1 crash after submit          -> durable job exists, truth recoverable
C2 crash during execution      -> no fabricated success; stale worker refused
C3 crash after verification    -> evidence interpretable; no invented success
C4 crash before final commit   -> physical artifact exists but is NOT authority
C5 crash after final commit    -> recovery observes COMPLETED, no re-execution
C6 notification failure        -> durable success intact
C7 late stale worker           -> late commit refused, no stale success notice
C8 unknown execution           -> UNKNOWN stays UNKNOWN, never terminal FAILED
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import timedelta
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
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.jobs.verification import VerificationOutcome

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


def _backdate_started_at(db: Path, job_id: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )


async def _wait_status(queue: InProcessJobQueue, job_id: str, wanted: JobStatus) -> None:
    for _ in range(500):
        if await queue.get_status(job_id) is wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {wanted}: {await queue.get_status(job_id)}")


async def _drain(queue: InProcessJobQueue, job_id: str) -> JobStatus:
    for _ in range(500):
        status = await queue.get_status(job_id)
        if status in TERMINAL:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class _Gate:
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
# C1 — crash after submit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c1_crash_after_submit_leaves_a_recoverable_durable_job(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, _ = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a)

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)

    # "Restart": a fresh process over the same sidecar.
    queue_b = InProcessJobQueue(db, artifact_verifiers={})
    queue_b.register_handler("fenced", gate)
    backend_b = NativeLocalBackend(queue_b)

    # The durable job exists and its truth is recoverable (never invented).
    observed = await backend_b.observe(identity)
    assert observed.state in {ObservationState.PENDING, ObservationState.PROCESSING}
    assert observed.state is not ObservationState.SUCCEEDED

    # Startup recovery reclaims the orphaned row and it completes exactly once.
    assert await queue_b.resume_pending() == [identity.job_id]
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2
    gate.event(2).set()
    assert await _drain(queue_b, identity.job_id) is JobStatus.COMPLETED
    gate.event(1).set()  # the crashed worker's late finish (refused)
    await asyncio.sleep(0.05)
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


# --------------------------------------------------------------------------- #
# C2 — crash during execution
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c2_crash_during_execution_fabricates_no_success(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _queues(db, gate)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)

    # A live peer is still working; nothing has committed a success.
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value

    # Recovery supersedes the crashed attempt; the stale worker cannot complete.
    _backdate_started_at(db, job_id)
    assert await queue_b.recover_job(job_id, stale_after=timedelta(hours=1)) == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    assert _row(db, job_id)["attempt"] == 2
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value  # no fabricated success
    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED


# --------------------------------------------------------------------------- #
# C3 — crash after verification (row is VERIFYING, not yet committed)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c3_crash_after_verification_invents_no_success(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    verifier_entered = threading.Event()
    verifier_release = threading.Event()

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        verifier_entered.set()
        verifier_release.wait(timeout=5)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "ok"})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"artifact": "x"}

    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler("v", handler)
    queue.register_artifact_verifier("v", verifier)

    job_id = await queue.enqueue(job_type="v", idempotency_key="k", payload={})
    await _wait_status(queue, job_id, JobStatus.VERIFYING)
    assert await asyncio.to_thread(verifier_entered.wait, 5)

    # Crash window: the process dies while the verifier is parked.  The row is
    # VERIFYING (evidence interpretable), never a fabricated SUCCEEDED.
    assert _row(db, job_id)["status"] == JobStatus.VERIFYING.value

    # A fresh process observes the durable truth: still in-flight, not success.
    fresh = InProcessJobQueue(db, artifact_verifiers={})
    fresh.register_handler("v", handler)
    fresh.register_artifact_verifier("v", verifier)
    backend = NativeLocalBackend(fresh)
    ghost = ExecutionIdentity(request_id="r", idempotency_key="k", job_id=job_id)
    observed = await backend.observe(ghost)
    assert observed.state is ObservationState.VERIFYING
    assert not observed.is_terminal

    # A policy-less reconcile observes only: no invented success, no takeover.
    reconciled = await backend.reconcile(ghost)
    assert reconciled.state is ObservationState.VERIFYING
    assert _row(db, job_id)["status"] == JobStatus.VERIFYING.value

    # The parked execution is still the current owner (attempt 1): releasing it
    # commits exactly once; nothing was fabricated during the crash window.
    verifier_release.set()
    for _ in range(500):
        if await queue.get_status(job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert _row(db, job_id)["status"] == JobStatus.COMPLETED.value
    assert _row(db, job_id)["attempt"] == 1
    await fresh.shutdown()
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# C4 — crash immediately before the final commit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c4_physical_artifact_is_not_authoritative_completion(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _queues(db, gate)

    job_id = await queue.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)

    # A physical artifact reaches the final namespace...
    staging = AttemptStaging(
        tmp_path / "staging_root",
        job_id=job_id,
        attempt_id="attempt_deadbe",
        final_root=tmp_path / "final_root",
    )
    staged = staging.write("artifact.bin", b"verified-bytes")
    published = staging.publish(staged, "artifact.bin")
    assert published.exists() and published.read_bytes() == b"verified-bytes"

    # ...but the DB authority is decisive: the job is NOT complete.
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value

    # Only the fenced commit makes it authoritative.
    gate.event(1).set()
    assert await _drain(queue, job_id) is JobStatus.COMPLETED


# --------------------------------------------------------------------------- #
# C5 — crash immediately after the final commit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c5_crash_after_commit_is_not_re_executed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, _ = _queues(db, gate)
    backend_a = NativeLocalBackend(queue_a)

    identity = await backend_a.submit(_request())
    gate.event(1).set()
    assert await _drain(queue_a, identity.job_id) is JobStatus.COMPLETED

    # "Restart": recovery must observe COMPLETED and never re-run the job.
    queue_b = InProcessJobQueue(db, artifact_verifiers={})
    queue_b.register_handler("fenced", gate)
    assert await queue_b.resume_pending() == []
    assert await queue_b.resume_pending_jobs() == []
    assert gate.calls == 1, "a completed job must never be executed again"
    backend_b = NativeLocalBackend(queue_b)
    observed = await backend_b.observe(identity)
    assert observed.state is ObservationState.SUCCEEDED
    assert observed.result == {"who": "worker-1"}


# --------------------------------------------------------------------------- #
# C6 — notification failure
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c6_notification_failure_leaves_durable_success(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"

    async def broken_hook(completion: JobCompletion) -> None:
        raise RuntimeError("notifier down")

    queue = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=broken_hook)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("plain", handler)
    job_id = await queue.enqueue(job_type="plain", idempotency_key="k", payload={})
    for _ in range(500):
        if await queue.get_status(job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert _row(db, job_id)["status"] == JobStatus.COMPLETED.value
    assert (await queue.get_result(job_id) or {})["ok"] is True
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# C7 — late stale worker
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c7_late_stale_worker_is_refused_without_a_success_notice(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    notices: list[JobStatus] = []

    async def hook(completion: JobCompletion) -> None:
        notices.append(completion.status)

    queue_a, queue_b = _queues(db, gate, hook_b=hook)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)

    # The stale worker finishes late: refused, no authoritative completion,
    # and no stale success notification.
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert notices == []
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value

    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED
    assert notices == [JobStatus.COMPLETED]


# --------------------------------------------------------------------------- #
# C8 — unknown execution
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c8_unknown_stays_unknown_until_reconcile(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue, _ = _queues(db, _Gate())
    backend = NativeLocalBackend(queue)

    # (a) a missing row is UNKNOWN, never a terminal business failure.
    ghost = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="nope")
    unknown = await backend.observe(ghost)
    assert unknown.state is ObservationState.UNKNOWN
    assert unknown.failure is not None
    assert unknown.failure.disposition is FailureDisposition.UNKNOWN
    assert not unknown.failure.is_terminal_business_failure

    # reconcile (no policy) does not invent a terminal failure either.
    reconciled = await backend.reconcile(ghost)
    assert reconciled.state is ObservationState.UNKNOWN
    assert not reconciled.is_terminal

    # (b) an unreadable/corrupt persisted status is also UNKNOWN, not FAILED.
    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET status = ? WHERE id = ?",
            ("corrupt_status_spelling", identity.job_id),
        )
    corrupt = await backend.observe(identity)
    assert corrupt.state is ObservationState.UNKNOWN
    assert corrupt.failure is not None
    assert corrupt.failure.disposition is FailureDisposition.UNKNOWN
    assert not corrupt.failure.is_terminal_business_failure
    await queue.shutdown()
