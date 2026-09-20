"""Contract tests for the in-process ``JobQueuePort`` adapter (R-001 / R-026).

The adapter replaces the Celery/Redis topology.  Everything here runs against
a real SQLite sidecar file in ``tmp_path`` — no broker, no mocks of the
adapter itself — so the tests prove the actual runtime behaviour:

* jobs execute in-process and expose status + result;
* the same idempotency key never produces a second effect;
* failures are recorded (``failed`` + error), never swallowed or raised into
  the enqueuing coroutine;
* state is durable across a "process restart" (a fresh adapter over the same
  file) and can be resumed explicitly;
* every persisted transition obeys the frozen domain state machine.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobRecord,
    JobStatus,
)
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.domain.policies.retention import ALLOWED_TRANSITIONS, JournalStatus


def _queue(tmp_path: Path) -> InProcessJobQueue:
    return InProcessJobQueue(tmp_path / "jobs.sqlite")


async def _until_running(queue: InProcessJobQueue, job_id: str, *, timeout: float = 5.0) -> None:
    """Block until the job's row is ``running`` (deterministic stand-in for a fixed sleep)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        record = await queue.get_job(job_id)
        if record is not None and record.status is JobStatus.RUNNING:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach running within {timeout}s")


async def test_adapter_satisfies_the_stage0_port(tmp_path: Path) -> None:
    """The adapter is a structural ``JobQueuePort``; the port itself is untouched."""
    queue = _queue(tmp_path)
    port: JobQueuePort = queue  # static check: mypy verifies the Protocol
    assert hasattr(port, "enqueue") and hasattr(port, "get_status")
    assert JobStatus is JournalStatus, "job states reuse the frozen domain vocabulary"
    await queue.close()


async def test_in_process_job_queue_execution(tmp_path: Path) -> None:
    """A registered handler runs in-process; status and result are observable."""
    queue = _queue(tmp_path)
    seen: list[dict[str, Any]] = []

    async def echo(payload: dict[str, object]) -> dict[str, object]:
        seen.append(dict(payload))
        return {"echo": payload["value"]}

    queue.register("echo", echo)
    job_id = await queue.enqueue(job_type="echo", idempotency_key="k-1", payload={"value": 42})

    assert isinstance(job_id, str) and job_id
    status = await queue.wait(job_id, timeout=5)
    assert status == JobStatus.SUCCEEDED
    assert await queue.get_status(job_id) == "succeeded"
    assert await queue.get_result(job_id) == {"echo": 42}
    assert seen == [{"value": 42}]

    record = await queue.get_job(job_id)
    assert isinstance(record, JobRecord)
    assert record.job_type == "echo"
    assert record.idempotency_key == "k-1"
    assert record.attempts == 1
    assert record.error is None
    await queue.close()


async def test_enqueue_returns_before_the_job_finishes(tmp_path: Path) -> None:
    """Submission is non-blocking: the caller gets a job id while work is pending."""
    queue = _queue(tmp_path)
    release = asyncio.Event()

    async def slow(payload: dict[str, object]) -> None:
        await release.wait()

    queue.register("slow", slow)
    job_id = await queue.enqueue(job_type="slow", idempotency_key="k", payload={})
    assert await queue.get_status(job_id) in {"pending", "running"}
    assert await queue.get_result(job_id) is None
    release.set()
    assert await queue.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
    await queue.close()


async def test_enqueue_is_idempotent_per_key(tmp_path: Path) -> None:
    """Same ``(job_type, idempotency_key)`` ⇒ same job id, exactly one effect."""
    queue = _queue(tmp_path)
    calls = 0

    async def count(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"n": payload["n"]}

    queue.register("count", count)
    first = await queue.enqueue(job_type="count", idempotency_key="same", payload={"n": 1})
    second = await queue.enqueue(job_type="count", idempotency_key="same", payload={"n": 2})
    # Concurrent duplicates (webhook redelivery) collapse as well.
    third, fourth = await asyncio.gather(
        queue.enqueue(job_type="count", idempotency_key="same", payload={"n": 3}),
        queue.enqueue(job_type="count", idempotency_key="same", payload={"n": 4}),
    )
    assert first == second == third == fourth
    assert await queue.wait(first, timeout=5) == JobStatus.SUCCEEDED
    assert calls == 1
    assert await queue.get_result(first) == {"n": 1}

    # A different job type with the same key is a different job.
    queue.register("other", count)
    other = await queue.enqueue(job_type="other", idempotency_key="same", payload={"n": 9})
    assert other != first
    assert await queue.wait(other, timeout=5) == JobStatus.SUCCEEDED
    assert calls == 2
    await queue.close()


async def test_job_queue_failure_handling(tmp_path: Path) -> None:
    """A raising handler ⇒ ``failed`` + stored error; nothing propagates to the caller."""
    queue = _queue(tmp_path)

    async def boom(payload: dict[str, object]) -> None:
        raise ValueError("kaboom")

    queue.register("boom", boom)
    job_id = await queue.enqueue(job_type="boom", idempotency_key="k", payload={"x": 1})

    assert await queue.wait(job_id, timeout=5) == JobStatus.FAILED
    assert await queue.get_status(job_id) == "failed"
    assert await queue.get_result(job_id) is None
    record = await queue.get_job(job_id)
    assert record is not None
    assert record.error == "ValueError: kaboom"
    assert record.attempts == 1
    await queue.close()


async def test_unknown_job_type_is_rejected_at_enqueue(tmp_path: Path) -> None:
    """Submitting work nobody can run fails loudly instead of parking a dead row."""
    queue = _queue(tmp_path)
    with pytest.raises(LookupError):
        await queue.enqueue(job_type="nope", idempotency_key="k", payload={})
    # Rejected before any write: the sidecar has not even been created.
    assert not (tmp_path / "jobs.sqlite").exists()
    await queue.close()


async def test_non_json_payload_is_rejected_at_enqueue(tmp_path: Path) -> None:
    queue = _queue(tmp_path)

    async def noop(payload: dict[str, object]) -> None:
        return None

    queue.register("noop", noop)
    with pytest.raises(TypeError):
        await queue.enqueue(job_type="noop", idempotency_key="k", payload={"bad": object()})
    await queue.close()


async def test_unknown_job_id_raises(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(KeyError):
        await queue.get_status("does-not-exist")
    assert await queue.get_job("does-not-exist") is None
    assert await queue.get_result("does-not-exist") is None
    await queue.close()


async def test_memory_path_is_refused(tmp_path: Path) -> None:
    """``:memory:`` cannot be durable across connections; refuse it explicitly."""
    with pytest.raises(ValueError):
        InProcessJobQueue(":memory:")


async def test_job_queue_persistence_across_restart(tmp_path: Path) -> None:
    """Jobs survive a process restart: still visible, never falsely completed."""
    db = tmp_path / "jobs.sqlite"
    started = asyncio.Event()
    release = asyncio.Event()

    async def interrupted(payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"done": True}

    # Process 1: the job is mid-flight when the process goes away.
    first = InProcessJobQueue(db)
    first.register("work", interrupted)
    job_id = await first.enqueue(job_type="work", idempotency_key="k", payload={"n": 1})
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await first.get_status(job_id) == "running"
    await first.close(timeout=0)  # simulate SIGTERM/crash: in-flight task torn down

    # Process 2: a fresh adapter over the same file.
    runs: list[dict[str, object]] = []

    async def completes(payload: dict[str, object]) -> dict[str, object]:
        runs.append(dict(payload))
        return {"done": True}

    second = InProcessJobQueue(db)
    second.register("work", completes)
    # Still in the queue (not lost, not silently succeeded), result absent.
    assert await second.get_status(job_id) in {"pending", "running"}
    assert await second.get_result(job_id) is None
    # Idempotency holds across the restart: re-submission yields the same job.
    assert await second.enqueue(job_type="work", idempotency_key="k", payload={"n": 1}) == job_id

    # Explicit recovery (never implicit at boot) re-runs the interrupted job.
    assert await second.resume_pending() == [job_id]
    assert await second.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
    assert await second.get_result(job_id) == {"done": True}
    assert runs == [{"n": 1}]
    record = await second.get_job(job_id)
    assert record is not None
    assert record.attempts == 2  # one interrupted attempt + one successful re-run
    await second.close()


async def test_resume_pending_is_a_no_op_without_unfinished_work(tmp_path: Path) -> None:
    queue = _queue(tmp_path)

    async def ok(payload: dict[str, object]) -> None:
        return None

    queue.register("ok", ok)
    job_id = await queue.enqueue(job_type="ok", idempotency_key="k", payload={})
    assert await queue.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
    assert await queue.resume_pending() == []
    await queue.close()


async def test_resume_pending_fails_rows_without_a_handler(tmp_path: Path) -> None:
    """After a restart without the handler, the row fails visibly (no zombie)."""
    db = tmp_path / "jobs.sqlite"
    release = asyncio.Event()

    async def hang(payload: dict[str, object]) -> None:
        await release.wait()

    first = InProcessJobQueue(db)
    first.register("gone", hang)
    job_id = await first.enqueue(job_type="gone", idempotency_key="k", payload={})
    await _until_running(first, job_id)
    await first.close(timeout=0)

    second = InProcessJobQueue(db)  # no handler for "gone" registered
    assert await second.resume_pending() == [job_id]
    assert await second.wait(job_id, timeout=5) == JobStatus.FAILED
    record = await second.get_job(job_id)
    assert record is not None
    assert "no handler registered" in (record.error or "")
    await second.close()


async def test_persisted_transitions_obey_the_domain_state_machine(tmp_path: Path) -> None:
    """Every status change written by the adapter is an ``ALLOWED_TRANSITIONS`` edge."""
    db = tmp_path / "jobs.sqlite"
    queue = InProcessJobQueue(db)
    release = asyncio.Event()

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    async def boom(payload: dict[str, object]) -> None:
        raise RuntimeError("no")

    async def hang(payload: dict[str, object]) -> None:
        await release.wait()

    queue.register("ok", ok)
    queue.register("boom", boom)
    queue.register("hang", hang)
    await queue.initialize()  # the audit trigger below needs the ``jobs`` table

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS transitions (job_id TEXT, old TEXT, new TEXT)")
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_status AFTER UPDATE OF status ON jobs
            WHEN OLD.status <> NEW.status
            BEGIN
              INSERT INTO transitions VALUES (NEW.id, OLD.status, NEW.status);
            END
            """
        )
        conn.commit()

    ok_id = await queue.enqueue(job_type="ok", idempotency_key="a", payload={})
    boom_id = await queue.enqueue(job_type="boom", idempotency_key="b", payload={})
    hang_id = await queue.enqueue(job_type="hang", idempotency_key="c", payload={})
    assert await queue.wait(ok_id, timeout=5) == JobStatus.SUCCEEDED
    assert await queue.wait(boom_id, timeout=5) == JobStatus.FAILED
    await _until_running(queue, hang_id)
    await queue.close(timeout=0)

    resumed = InProcessJobQueue(db)
    resumed.register("hang", ok)
    assert await resumed.resume_pending() == [hang_id]
    assert await resumed.wait(hang_id, timeout=5) == JobStatus.SUCCEEDED
    await resumed.close()

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT job_id, old, new FROM transitions").fetchall()
    assert rows, "the audit trigger must have observed transitions"
    for job_id, old, new in rows:
        assert JournalStatus(new) in ALLOWED_TRANSITIONS[JournalStatus(old)], (
            job_id,
            old,
            new,
        )
    # Rows are created directly in ``pending``; nothing is ever inserted terminal.
    with sqlite3.connect(db) as conn:
        firsts = conn.execute(
            """
            SELECT job_id, old FROM transitions t
            WHERE rowid = (SELECT MIN(rowid) FROM transitions WHERE job_id = t.job_id)
            """
        ).fetchall()
    assert {job_id for job_id, _ in firsts} == {ok_id, boom_id, hang_id}
    assert {old for _, old in firsts} == {"pending"}


async def test_payload_and_result_are_stored_as_json(tmp_path: Path) -> None:
    """Persisted columns are inspectable JSON (no pickles, no opaque blobs)."""
    db = tmp_path / "jobs.sqlite"
    queue = InProcessJobQueue(db)

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"answer": 42, "nested": {"k": [1, 2]}}

    queue.register("ok", ok)
    job_id = await queue.enqueue(job_type="ok", idempotency_key="k", payload={"q": "?"})
    assert await queue.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
    with sqlite3.connect(db) as conn:
        payload, result = conn.execute(
            "SELECT payload, result FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert json.loads(payload) == {"q": "?"}
    assert json.loads(result) == {"answer": 42, "nested": {"k": [1, 2]}}
    await queue.close()


async def test_close_drains_in_flight_work(tmp_path: Path) -> None:
    """A graceful ``close()`` lets running jobs finish inside the timeout."""
    queue = _queue(tmp_path)

    async def short(payload: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"ok": True}

    queue.register("short", short)
    job_id = await queue.enqueue(job_type="short", idempotency_key="k", payload={})
    assert queue.in_flight == 1
    await queue.close(timeout=5)
    assert queue.in_flight == 0
    assert await queue.get_status(job_id) == "succeeded"


# ── D4: completion hook ────────────────────────────────────────────────


async def test_completion_hook_receives_the_terminal_record(tmp_path: Path) -> None:
    """The hook fires once per terminal transition with the persisted record."""
    queue = _queue(tmp_path)
    seen: list[JobRecord] = []

    async def hook(record: JobRecord) -> None:
        seen.append(record)

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    async def boom(payload: dict[str, object]) -> None:
        raise RuntimeError("nope")

    queue.register("ok", ok)
    queue.register("boom", boom)
    queue.set_completion_hook(hook)
    ok_id = await queue.enqueue(job_type="ok", idempotency_key="a", payload={"chat_id": 1})
    boom_id = await queue.enqueue(job_type="boom", idempotency_key="b", payload={"chat_id": 2})
    assert await queue.wait(ok_id, timeout=5) == JobStatus.SUCCEEDED
    assert await queue.wait(boom_id, timeout=5) == JobStatus.FAILED
    await queue.close()

    by_id = {record.job_id: record for record in seen}
    assert set(by_id) == {ok_id, boom_id}
    assert by_id[ok_id].status is JobStatus.SUCCEEDED
    assert by_id[ok_id].result == {"ok": True}
    assert by_id[ok_id].payload == {"chat_id": 1}
    assert by_id[boom_id].status is JobStatus.FAILED
    assert by_id[boom_id].error == "RuntimeError: nope"


async def test_completion_hook_failure_is_logged_not_fatal(tmp_path: Path) -> None:
    """A broken notifier can never change a job's recorded outcome (fail-safe)."""
    from structlog.testing import capture_logs

    queue = _queue(tmp_path)

    async def hook(record: JobRecord) -> None:
        raise ConnectionError("telegram down")

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register("ok", ok)
    queue.set_completion_hook(hook)
    with capture_logs() as logs:
        job_id = await queue.enqueue(job_type="ok", idempotency_key="a", payload={})
        assert await queue.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
        await queue.close()
    record = await queue.get_job(job_id)
    assert record is not None and record.status is JobStatus.SUCCEEDED and record.error is None
    failures = [entry for entry in logs if entry["event"] == "job_completion_hook_failed"]
    assert failures and failures[0]["error"] == "ConnectionError: telegram down"


async def test_completion_hook_fires_for_resumed_and_unhandled_jobs(tmp_path: Path) -> None:
    """Interrupted work resumed later, and rows whose handler is gone, notify too."""
    db = tmp_path / "jobs.sqlite"
    release = asyncio.Event()

    async def hang(payload: dict[str, object]) -> None:
        await release.wait()

    first = InProcessJobQueue(db)
    first.register("hang", hang)
    first.register("gone", hang)
    hang_id = await first.enqueue(job_type="hang", idempotency_key="h", payload={})
    gone_id = await first.enqueue(job_type="gone", idempotency_key="g", payload={})
    await _until_running(first, hang_id)
    await _until_running(first, gone_id)
    await first.close(timeout=0)

    seen: list[JobRecord] = []

    async def hook(record: JobRecord) -> None:
        seen.append(record)

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    second = InProcessJobQueue(db)
    second.register("hang", ok)  # "gone" deliberately unregistered
    second.set_completion_hook(hook)
    assert set(await second.resume_pending()) == {hang_id, gone_id}
    assert await second.wait(hang_id, timeout=5) == JobStatus.SUCCEEDED
    assert await second.wait(gone_id, timeout=5) == JobStatus.FAILED
    await second.close()
    outcomes = {record.job_id: record.status for record in seen}
    assert outcomes == {hang_id: JobStatus.SUCCEEDED, gone_id: JobStatus.FAILED}


# ── D1: store ownership (no double execution across processes) ─────────


async def test_second_instance_cannot_resume_while_the_store_is_owned(tmp_path: Path) -> None:
    """``resume_pending`` reclassifies ``running`` rows; only the store owner may do that."""
    from nexus_ai_agent.adapters.in_process_job_queue import JobStoreBusyError

    db = tmp_path / "jobs.sqlite"
    owner = InProcessJobQueue(db)
    await owner.initialize()
    assert owner.owns_store is True

    other = InProcessJobQueue(db)
    await other.initialize()
    assert other.owns_store is False
    with pytest.raises(JobStoreBusyError, match="another process owns the job store"):
        await other.resume_pending()

    await owner.close()
    assert owner.owns_store is False
    assert await other.resume_pending() == []  # ownership acquired lazily once free
    assert other.owns_store is True
    await other.close()


async def test_enqueue_never_needs_ownership(tmp_path: Path) -> None:
    """The bot keeps serving even if an operator holds the store (claims are CAS-safe)."""
    from structlog.testing import capture_logs

    db = tmp_path / "jobs.sqlite"
    operator = InProcessJobQueue(db)
    await operator.initialize()

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    bot = InProcessJobQueue(db)
    bot.register("ok", ok)
    with capture_logs() as logs:
        job_id = await bot.enqueue(job_type="ok", idempotency_key="a", payload={})
    assert await bot.wait(job_id, timeout=5) == JobStatus.SUCCEEDED
    assert any(entry["event"] == "job_store_owned_elsewhere" for entry in logs)
    await bot.close()
    await operator.close()


async def test_close_releases_ownership_even_without_tasks(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite"
    first = InProcessJobQueue(db)
    await first.initialize()
    await first.close()
    second = InProcessJobQueue(db)
    await second.initialize()
    assert second.owns_store is True
    await second.close()


async def test_list_unfinished_reports_rows_in_creation_order(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite"
    release = asyncio.Event()

    async def hang(payload: dict[str, object]) -> None:
        await release.wait()

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    first = InProcessJobQueue(db)
    first.register("hang", hang)
    first.register("ok", ok)
    hang_id = await first.enqueue(job_type="hang", idempotency_key="h", payload={})
    ok_id = await first.enqueue(job_type="ok", idempotency_key="o", payload={})
    assert await first.wait(ok_id, timeout=5) == JobStatus.SUCCEEDED
    await _until_running(first, hang_id)
    await first.close(timeout=0)

    second = InProcessJobQueue(db)
    unfinished = await second.list_unfinished()
    assert [(record.job_id, record.status) for record in unfinished] == [
        (hang_id, JobStatus.RUNNING)
    ]
    await second.close()
