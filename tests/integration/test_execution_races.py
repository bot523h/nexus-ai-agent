"""Deterministic concurrency races for the NEXUS V1 execution core.

These are **not** sleep-based flaky tests: every race is driven by an explicit
synchronisation point (a ``threading.Barrier`` for the synchronous fenced
CASes, an ``asyncio.Event`` gate for the worker), over a real file-backed
``InProcessJobQueue`` (real SQLite, real ``rowcount`` CAS), so the outcome is
deterministic in its *invariant* even though the linearization order varies.

Proven properties:

* P0-4 same-attempt double commit  -> exactly one authoritative completion.
* P0-5 completion vs cancellation  -> exactly one authoritative transition.
* P0-1 stale identity cannot cancel a newer attempt.
* I10  success notification only after the authoritative commit.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobCompletion,
)
from nexus_ai_agent.adapters.native_local_backend import NativeLocalBackend
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.execution.contract import ExecutionPolicy, ExecutionRequest
from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

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


def _attempt_ledger(db: Path, job_id: str) -> list[dict[str, Any]]:
    import json

    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT attempt_history_json FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
    return json.loads(row[0] or "[]")


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


async def _wait_status(queue: InProcessJobQueue, job_id: str, wanted: JobStatus) -> None:
    for _ in range(500):
        if await queue.get_status(job_id) is wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {wanted}: {await queue.get_status(job_id)}")


def _two_queues(
    db: Path, handler: Any, *, hook_b: Any = None
) -> tuple[InProcessJobQueue, InProcessJobQueue]:
    queue_a = InProcessJobQueue(db, artifact_verifiers={})
    queue_b = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=hook_b)
    queue_a.register_handler("fenced", handler)
    queue_b.register_handler("fenced", handler)
    return queue_a, queue_b


def _request(key: str = "k") -> ExecutionRequest:
    # These queue-protocol tests intentionally opt out of artifact proof; the
    # default contract policy requires a registered verifier.
    return ExecutionRequest(
        job_type="fenced",
        idempotency_key=key,
        payload={},
        policy=ExecutionPolicy(requires_verification=False),
    )


# --------------------------------------------------------------------------- #
# P0-4 — same-attempt double commit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_attempt_double_commit_has_exactly_one_winner(tmp_path: Path) -> None:
    """Two simultaneous commits for one attempt: exactly one CAS succeeds."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _two_queues(db, gate)

    job_id = await queue.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    claim = ExecutionClaim(job_id=job_id, attempt=1)

    barrier = threading.Barrier(2)
    outcomes: dict[str, bool] = {}

    def _commit(name: str) -> None:
        barrier.wait()  # both commits are ready before either runs
        outcomes[name] = queue._mark_completed(claim, {"winner": name})

    threads = [threading.Thread(target=_commit, args=(n,)) for n in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one CAS committed; the other observed the row already terminal.
    assert sorted(outcomes.values()) == [False, True], outcomes
    winner = next(name for name, ok in outcomes.items() if ok)

    import json

    row = _row(db, job_id)
    assert row["status"] == JobStatus.COMPLETED.value
    assert json.loads(str(row["result_json"]))["winner"] == winner

    # Exactly one authoritative completion transition in the durable ledger.
    ledger = _attempt_ledger(db, job_id)
    completed = [entry for entry in ledger if entry.get("status") == "completed"]
    assert len(completed) == 1, ledger

    # Release the parked worker: its late commit is refused, never a second one.
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert _row(db, job_id)["status"] == JobStatus.COMPLETED.value
    assert len([e for e in _attempt_ledger(db, job_id) if e.get("status") == "completed"]) == 1
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# P0-5 — completion vs cancellation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_completion_vs_cancellation_exactly_one_transition(tmp_path: Path) -> None:
    """A concurrent completion and cancellation: one wins, never both."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _two_queues(db, gate)

    job_id = await queue.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    claim = ExecutionClaim(job_id=job_id, attempt=1)

    barrier = threading.Barrier(2)
    outcomes: dict[str, bool] = {}

    def _complete() -> None:
        barrier.wait()
        outcomes["complete"] = queue._mark_completed(claim, {"winner": "completion"})

    def _cancel() -> None:
        barrier.wait()
        outcomes["cancel"] = queue._mark_pending(claim)

    threads = [
        threading.Thread(target=_complete),
        threading.Thread(target=_cancel),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one authoritative state transition.
    assert sum(outcomes.values()) == 1, outcomes

    row = _row(db, job_id)
    if outcomes["complete"]:
        assert row["status"] == JobStatus.COMPLETED.value
        assert outcomes["cancel"] is False
    else:
        assert row["status"] == JobStatus.PENDING.value
        assert row["attempt"] == 1  # cancellation never mints a new token

    # The ledger has at most one terminal attempt record (never two).
    terminal = [
        entry
        for entry in _attempt_ledger(db, job_id)
        if entry.get("status") in {"completed", "interrupted"}
    ]
    assert len(terminal) <= 1, terminal

    gate.event(1).set()
    await queue.shutdown()


@pytest.mark.asyncio
async def test_completion_wins_then_cancellation_is_refused(tmp_path: Path) -> None:
    """Linearized completion-first: the later cancel must not reopen the row."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _two_queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    claim = ExecutionClaim(job_id=identity.job_id, attempt=1)
    assert queue._mark_completed(claim, {"winner": "completion"}) is True

    bound = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(bound) is False
    assert await queue.get_status(identity.job_id) is JobStatus.COMPLETED
    gate.event(1).set()
    await queue.shutdown()


@pytest.mark.asyncio
async def test_cancellation_wins_then_completion_is_refused(tmp_path: Path) -> None:
    """Linearized cancellation-first: the later completion must be refused."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _two_queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    bound = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(bound) is True

    claim = ExecutionClaim(job_id=identity.job_id, attempt=1)
    assert queue._mark_completed(claim, {"winner": "late"}) is False
    assert await queue.get_status(identity.job_id) is JobStatus.PENDING
    gate.event(1).set()
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# P0-1 — a stale identity can never cancel a newer attempt
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stale_identity_cannot_cancel_a_newer_attempt(tmp_path: Path) -> None:
    """The mission's required race: attempt N stale cancel vs attempt N+1 owner."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)
    backend_a = NativeLocalBackend(queue_a, worker_id="A")

    identity = await backend_a.submit(_request())
    await _wait_status(queue_a, identity.job_id, JobStatus.PROCESSING)
    stale = identity.with_attempt(attempt_id="a#1", fencing_token=1)

    # A newer attempt takes the row over (attempt 2).
    assert await queue_b.resume_pending() == [identity.job_id]
    await _wait_status(queue_b, identity.job_id, JobStatus.PROCESSING)
    assert _row(db, identity.job_id)["attempt"] == 2

    # The stale identity (attempt 1) calls cancel: it must be rejected, and the
    # current attempt (2) must remain intact and uncancelled.
    assert await backend_a.cancel(stale) is False
    row = _row(db, identity.job_id)
    assert row["attempt"] == 2
    assert row["status"] == JobStatus.PROCESSING.value
    assert not queue_b._tasks[identity.job_id].cancelled()

    # The current owner completes normally under its own token.
    gate.event(2).set()
    for _ in range(500):
        if await queue_b.get_status(identity.job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert await queue_b.get_status(identity.job_id) is JobStatus.COMPLETED
    gate.event(1).set()  # the stale worker's late finish is refused
    await asyncio.sleep(0.05)
    assert (await queue_b.get_result(identity.job_id) or {})["who"] == "worker-2"


@pytest.mark.asyncio
async def test_current_identity_can_cancel_its_own_attempt(tmp_path: Path) -> None:
    """A *current* attempt-scoped identity still cancels its own attempt."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue, _ = _two_queues(db, gate)
    backend = NativeLocalBackend(queue)

    identity = await backend.submit(_request())
    await _wait_status(queue, identity.job_id, JobStatus.PROCESSING)
    current = identity.with_attempt(attempt_id="a#1", fencing_token=1)
    assert await backend.cancel(current) is True
    assert await queue.get_status(identity.job_id) is JobStatus.PENDING
    gate.event(1).set()
    await queue.shutdown()


# --------------------------------------------------------------------------- #
# I10 — notification ordering (instrument the durable commit)
# --------------------------------------------------------------------------- #
class _RecordingQueue(InProcessJobQueue):
    """Records the unified order of (authoritative commit, notification)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.order: list[tuple[str, object]] = []

    def _mark_completed(self, *args: Any, **kwargs: Any) -> bool:
        committed = super()._mark_completed(*args, **kwargs)
        self.order.append(("commit", committed))
        return committed

    async def _notify_completion(self, completion: JobCompletion) -> None:
        self.order.append(("notify", completion.status))
        await super()._notify_completion(completion)


@pytest.mark.asyncio
async def test_success_notification_only_after_the_commit(tmp_path: Path) -> None:
    """I10: the notifier fires strictly after a committed authoritative CAS."""
    db = tmp_path / "jobs.sqlite3"
    queue = _RecordingQueue(db, artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("plain", handler)
    job_id = await queue.enqueue(job_type="plain", idempotency_key="k", payload={})
    for _ in range(500):
        if await queue.get_status(job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)

    # Exactly one authoritative commit, followed by exactly one notification.
    assert queue.order == [("commit", True), ("notify", JobStatus.COMPLETED)], queue.order
    await queue.shutdown()


@pytest.mark.asyncio
async def test_refused_commit_emits_no_success_notification(tmp_path: Path) -> None:
    """I10: when the CAS is refused the notifier must not run at all."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a = _RecordingQueue(db, artifact_verifiers={})  # records commit/notify order
    queue_b = InProcessJobQueue(db, artifact_verifiers={})
    queue_a.register_handler("fenced", gate)
    queue_b.register_handler("fenced", gate)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]  # attempt 2 supersedes attempt 1
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)

    # The stale worker (attempt 1) finishes: its commit is refused -> no notify.
    gate.event(1).set()
    await asyncio.sleep(0.05)
    assert queue_a.order == [("commit", False)], queue_a.order

    # The current owner (attempt 2) commits -> exactly one success notification.
    gate.event(2).set()
    for _ in range(500):
        if await queue_b.get_status(job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert await queue_b.get_status(job_id) is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_notification_failure_never_reverts_a_successful_commit(tmp_path: Path) -> None:
    """I10: a broken notifier cannot corrupt durable success."""
    db = tmp_path / "jobs.sqlite3"

    async def broken_hook(completion: JobCompletion) -> None:
        raise RuntimeError("notifier is down")

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
