"""task-283 — the lock must outlive the publication *thread*.

Review finding 1 (High): ``publication.publish`` ran via ``asyncio.to_thread``.
Cancelling the awaiting worker task stops the *await*, not the executor thread,
so the old shutdown could cancel/gather its tasks and release the exclusive
process lock while an artifact rename was still in flight.  A successor process
could then acquire ownership and rewrite the same destination concurrently.

These tests are barrier-controlled — the publication is held open by a
``threading.Event`` and only the test releases it — so the interleaving is
deterministic instead of timing-dependent.  They assert the *contract*:

    publication thread finished  <  process lock released

and, in the failed state, that a contender cannot slip through the gap.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    ArtifactPublication,
    InProcessJobQueue,
    QueueOwnershipError,
)
from nexus_ai_agent.jobs.lifecycle import ExecutionClaim
from nexus_ai_agent.jobs.verification import VerificationOutcome

JOB_TYPE = "barrier_publication"


def _ok_verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
    return VerificationOutcome(ok=True, reason_code=None, summary={})


class _GatedPublication:
    """Publication that starts, signals, then waits for the test."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.lock_held_when_finishing: bool | None = None
        self.queue: InProcessJobQueue | None = None

    def publish(self, payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        self.started.set()
        assert self.release.wait(timeout=30), "test never released the publication"
        # The destructive filesystem step a real publication performs.
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(b"published bytes")
        queue = self.queue
        self.lock_held_when_finishing = queue.owns_process_lock if queue else None
        self.finished.set()
        return {"artifact_path": str(self.target)}

    def retract(self, payload: dict[str, object], result: dict[str, object]) -> None:
        return None

    def as_publication(self) -> ArtifactPublication:
        return ArtifactPublication(publish=self.publish, retract=self.retract)


def _queue(tmp_path: Path, publication: _GatedPublication) -> InProcessJobQueue:
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    publication.queue = queue

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"artifact_path": str(publication.target)}

    queue.register_handler(JOB_TYPE, handler)
    queue.register_artifact_verifier(JOB_TYPE, _ok_verifier)
    queue.register_artifact_publication(JOB_TYPE, publication.as_publication())
    return queue


# ── the central ordering assertion ──────────────────────────────────────────


async def test_shutdown_keeps_ownership_until_the_publication_thread_ends(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifacts" / "render.mp4"
    publication = _GatedPublication(target)
    owner = _queue(tmp_path, publication)
    await owner.acquire_ownership()
    job_id = await owner.enqueue(job_type=JOB_TYPE, idempotency_key="barrier-1", payload={"n": 1})

    assert await asyncio.to_thread(publication.started.wait, 10), "publication never began"

    shutdown = asyncio.create_task(owner.shutdown())
    # Give the coroutine every chance to (incorrectly) finish: it must not.
    await asyncio.sleep(0.2)
    assert not shutdown.done(), "shutdown returned while a publication thread was still running"
    assert owner.owns_process_lock is True, "ownership must be held while publication is in flight"

    # A contender sees a locked queue — the gap the finding described is gone.
    rival = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    with pytest.raises(QueueOwnershipError):
        await rival.acquire_ownership()
    assert rival.owns_process_lock is False

    publication.release.set()
    await asyncio.wait_for(shutdown, timeout=15)

    assert publication.finished.is_set(), "shutdown must drain the publication thread"
    assert publication.lock_held_when_finishing is True, (
        "the publication thread finished while the lock was already released"
    )
    assert owner.owns_process_lock is False

    # Only now may a successor take the queue; the job stays recoverable.
    await rival.acquire_ownership()
    assert rival.owns_process_lock is True
    assert await owner.get_status(job_id) is not None

    await rival.shutdown()


async def test_publication_that_never_started_is_not_awaited_forever(
    tmp_path: Path,
) -> None:
    """Draining waits for *started* work only — a normal shutdown stays prompt."""
    target = tmp_path / "artifacts" / "render.mp4"
    publication = _GatedPublication(target)
    publication.release.set()  # never blocks
    queue = _queue(tmp_path, publication)
    await queue.acquire_ownership()
    await queue.enqueue(job_type=JOB_TYPE, idempotency_key="fast-1", payload={})

    await asyncio.wait_for(queue.shutdown(), timeout=15)
    assert not queue._publication_threads, "no drained publication may leak a tracked future"
    assert queue.owns_process_lock is False


# ── closed publication: fail closed after shutdown starts ────────────────────


async def test_publication_is_refused_once_shutdown_has_run(tmp_path: Path) -> None:
    """Post-shutdown publication attempts must not touch the filesystem."""
    target = tmp_path / "artifacts" / "render.mp4"
    publication = _GatedPublication(target)
    publication.release.set()
    queue = _queue(tmp_path, publication)
    await queue.acquire_ownership()
    await queue.shutdown()

    result, outcome, published = await queue._publish_and_reprobe(
        ExecutionClaim(job_id="job-after-shutdown", attempt=1),
        publication.as_publication(),
        _ok_verifier,
        {},
        {"artifact_path": str(target)},
    )

    assert published is False
    assert outcome.ok is False
    assert outcome.reason_code == "shutdown_in_progress"
    assert not target.exists(), "a closed queue must publish nothing"
    assert result == {"artifact_path": str(target)}


# ── idempotency: repeated shutdown is a no-op ───────────────────────────────


async def test_shutdown_is_idempotent_and_concurrency_safe(tmp_path: Path) -> None:
    publication = _GatedPublication(tmp_path / "artifacts" / "render.mp4")
    publication.release.set()
    queue = _queue(tmp_path, publication)
    await queue.acquire_ownership()

    reopens: list[str] = []
    original = queue._mark_pending

    def counting(claim: ExecutionClaim) -> bool:
        reopens.append(claim.job_id)
        return original(claim)

    queue._mark_pending = counting  # type: ignore[method-assign]

    await asyncio.gather(queue.shutdown(), queue.shutdown())
    await queue.shutdown()

    assert queue.owns_process_lock is False
    assert reopens == [], "repeated shutdown must not reopen/resettle anything twice"
