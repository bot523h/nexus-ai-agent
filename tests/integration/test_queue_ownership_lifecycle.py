"""task-283 — queue ownership lifecycle (in-process).

Proves the single-executor contract of
:class:`nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue`:

* the first process acquires ownership,
* a second live instance on the same sidecar cannot,
* release is idempotent and safe after a failed acquisition,
* shutdown stops workers/heartbeat children *before* releasing the lock,
* a crashed owner leaves no unrecoverable stale lock,
* ownership exclusion — not PID bookkeeping — is what prevents a second
  executor from recovering the same live job.

The cross-process half of this contract lives in
``tests/integration/test_queue_ownership_processes.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    QueueOwnershipError,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus


async def _wait(
    queue: InProcessJobQueue, job_id: str, status: JobStatus, timeout: float = 5.0
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await queue.get_status(job_id) is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {status}")


# ── acquisition / release ────────────────────────────────────────────────────


async def test_first_process_acquires_ownership(tmp_path: Path) -> None:
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    assert queue.owns_process_lock is False
    await queue.acquire_ownership()
    assert queue.owns_process_lock is True
    await queue.shutdown()
    assert queue.owns_process_lock is False


async def test_second_live_instance_cannot_acquire(tmp_path: Path) -> None:
    """Two instances over one sidecar: exactly one may own it."""
    db = str(tmp_path / "jobs.sqlite3")
    first = InProcessJobQueue(db)
    second = InProcessJobQueue(db)
    await first.acquire_ownership()
    with pytest.raises(QueueOwnershipError):
        await second.acquire_ownership()
    assert second.owns_process_lock is False, "a refused acquisition must not report ownership"
    await first.shutdown()

    # Once the owner releases, the second instance may acquire.
    await second.acquire_ownership()
    assert second.owns_process_lock is True
    await second.shutdown()


async def test_acquisition_is_idempotent_for_the_owner(tmp_path: Path) -> None:
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    await queue.acquire_ownership()
    fd = queue._lock_fd
    await queue.acquire_ownership()
    assert queue._lock_fd == fd, "re-acquiring must not replace the held descriptor"
    await queue.shutdown()


async def test_release_is_idempotent(tmp_path: Path) -> None:
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    await queue.acquire_ownership()
    await queue.shutdown()
    await queue.shutdown()  # must not raise
    queue._release_flock()  # and must stay safe after descriptors are gone
    assert queue.owns_process_lock is False


async def test_release_after_failed_acquisition_is_safe(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.sqlite3")
    owner = InProcessJobQueue(db)
    await owner.acquire_ownership()
    loser = InProcessJobQueue(db)
    with pytest.raises(QueueOwnershipError):
        await loser.acquire_ownership()
    # Shutting down the instance that never acquired must not free the owner's
    # lock (this is the "ambiguous ownership on a failed startup" hazard).
    await loser.shutdown()
    assert owner.owns_process_lock is True
    await owner.acquire_ownership()  # still ours
    await owner.shutdown()


async def test_lock_descriptor_is_closed_on_release(tmp_path: Path) -> None:
    """Release must close the descriptor it opened (the old code leaked it)."""
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    await queue.acquire_ownership()
    fd = queue._lock_fd
    assert fd is not None
    await queue.shutdown()
    with pytest.raises(OSError):
        import os

        os.fstat(fd)  # a closed descriptor is an error, never a silent leak


async def test_memory_queue_never_takes_a_lock(tmp_path: Path) -> None:
    queue = InProcessJobQueue(":memory:")
    await queue.acquire_ownership()
    assert queue.owns_process_lock is False
    await queue.shutdown()


# ── shutdown ordering ────────────────────────────────────────────────────────


async def test_shutdown_stops_workers_and_heartbeats_before_releasing(tmp_path: Path) -> None:
    """Ownership is released only after in-flight work is stopped."""
    queue = InProcessJobQueue(str(tmp_path / "jobs.sqlite3"))
    started = asyncio.Event()
    observed_lock_during_job: list[bool] = []

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        observed_lock_during_job.append(queue.owns_process_lock)
        started.set()
        await asyncio.sleep(30)  # long enough to still be running at shutdown
        return {}

    queue.register_handler("slow", handler)
    await queue.acquire_ownership()
    job_id = await queue.enqueue(job_type="slow", idempotency_key="k1", payload={})
    await asyncio.wait_for(started.wait(), timeout=5)

    assert queue._tasks, "the worker must still be running"
    assert queue._heartbeat_tasks, "a heartbeat child must exist for the in-flight attempt"

    await queue.shutdown()

    assert not queue._tasks, "workers must be gone before the lock is released"
    assert not queue._heartbeat_tasks, "heartbeat children must be gone too"
    assert queue.owns_process_lock is False
    assert observed_lock_during_job == [True], "the worker ran while ownership was held"

    # The interrupted attempt stays recoverable rather than completing.
    assert await queue.get_status(job_id) is JobStatus.PENDING


# ── crash / stale lock ───────────────────────────────────────────────────────


async def test_dead_owner_leaves_no_unrecoverable_stale_lock(tmp_path: Path) -> None:
    """A lock abandoned by a dead process is immediately acquirable.

    The previous PID-lock implementation needed an explicit "stale" reclamation
    pass (read PID -> test liveness -> unlink -> retry), and that pass was
    itself the race.  An OS lock needs no reclamation: the kernel drops it.
    """
    import subprocess
    import sys

    db = tmp_path / "jobs.sqlite3"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import asyncio\n"
                "from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue\n"
                f"q = InProcessJobQueue({str(db)!r})\n"
                "asyncio.run(q.acquire_ownership())\n"
                "print('READY', flush=True)\n"
                "import os\n"
                "os._exit(7)\n"  # simulated crash: no release, no shutdown
            ),
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = child.stdout.readline()
    assert line.strip() == "READY", f"child failed to acquire: {child.stderr.read()}"
    child.wait(timeout=10)  # fully reaped: this is not a PID-liveness shortcut

    queue = InProcessJobQueue(str(db))
    await queue.acquire_ownership()  # must not raise
    assert queue.owns_process_lock is True
    await queue.shutdown()


# ── exclusion prevents duplicate recovery ────────────────────────────────────


async def test_owner_can_recover_its_pending_job(tmp_path: Path) -> None:
    """A pending row left by a *dead* process is recoverable by the new owner."""
    db = tmp_path / "jobs.sqlite3"
    first = InProcessJobQueue(str(db))
    job_id = await first.enqueue(job_type="noop", idempotency_key="k1", payload={})
    await first.shutdown()  # releases ownership, leaves the row pending

    second = InProcessJobQueue(str(db))
    second.register_handler("noop", lambda payload: _ok())
    await second.acquire_ownership()
    recovered = await second.resume_pending()
    assert job_id in recovered
    await _wait(second, job_id, JobStatus.COMPLETED)
    await second.shutdown()


async def _ok() -> dict[str, object]:
    return {}


# ── the primitive itself ─────────────────────────────────────────────────────


async def test_lock_file_is_never_deleted_on_release(tmp_path: Path) -> None:
    """The rendezvous inode must survive release.

    Deleting it is the classic route to *two* owners on *two* inodes: A holds
    inode 1, A releases and unlinks, B creates inode 2, while C — which opened
    the path earlier — locks the orphaned inode 1.  B and C both believe they
    own the queue.
    """
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(str(db))
    await queue.acquire_ownership()
    lock_path = db.parent / (db.name + ".lock")
    assert lock_path.exists()
    inode_before = lock_path.stat().st_ino
    await queue.shutdown()
    assert lock_path.exists(), "the lock anchor must outlive the lease"
    assert lock_path.stat().st_ino == inode_before

    again = InProcessJobQueue(str(db))
    await again.acquire_ownership()
    assert (db.parent / (db.name + ".lock")).stat().st_ino == inode_before, (
        "re-acquisition must lock the same inode, not a freshly created one"
    )
    await again.shutdown()


async def test_pid_content_is_diagnostics_only(tmp_path: Path) -> None:
    """A garbage/absent PID in the anchor cannot grant or deny ownership."""
    import os

    db = tmp_path / "jobs.sqlite3"
    lock_path = db.parent / (db.name + ".lock")
    lock_path.write_text("not-a-pid\n")

    owner = InProcessJobQueue(str(db))
    await owner.acquire_ownership()  # unreadable PID is irrelevant to flock
    assert owner.owns_process_lock is True

    # And it is rewritten with a real hint for operators.
    assert lock_path.read_text().strip() == str(os.getpid())

    rival = InProcessJobQueue(str(db))
    with pytest.raises(QueueOwnershipError):
        await rival.acquire_ownership()
    await owner.shutdown()
