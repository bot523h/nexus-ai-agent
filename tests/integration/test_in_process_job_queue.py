"""Integration contract for the in-process durable job queue."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobCompletion,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.worker import (
    extract_pdf_text,
    generate_story_job,
    process_pdf_job,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


async def _wait_for_status(
    queue: InProcessJobQueue,
    job_id: str,
    expected: JobStatus,
    *,
    timeout: float = 2.0,
) -> None:
    async def _poll() -> None:
        while await queue.get_status(job_id) != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


def test_queue_schema_isolated_to_owned_table(tmp_path: Path) -> None:
    queue_db = tmp_path / "jobs.sqlite3"
    InProcessJobQueue(queue_db)

    with sqlite3.connect(queue_db) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert tables == [("nexus_job_queue",)]


@pytest.mark.asyncio
async def test_in_memory_job_queue_execution() -> None:
    queue = InProcessJobQueue(":memory:")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"value": payload["value"]}

    queue.register_handler("memory", handler)
    job_id = await queue.enqueue(
        job_type="memory",
        idempotency_key="memory-1",
        payload={"value": 7},
    )

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert await queue.get_result(job_id) == {"value": 7}


@pytest.mark.asyncio
async def test_in_process_job_queue_execution(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"echo": payload["value"]}

    queue.register_handler("echo", handler)
    job_id = await queue.enqueue(
        job_type="echo",
        idempotency_key="echo-1",
        payload={"value": "completed in process"},
    )

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert await queue.get_result(job_id) == {"echo": "completed in process"}


@pytest.mark.asyncio
async def test_job_queue_persistence_across_adapter_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    first_queue = InProcessJobQueue(db_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"value": payload["value"]}

    first_queue.register_handler("persist", handler)
    job_id = await first_queue.enqueue(
        job_type="persist",
        idempotency_key="persist-1",
        payload={"value": 42},
    )
    await _wait_for_status(first_queue, job_id, JobStatus.COMPLETED)

    # A new adapter instance represents a process restart. The completed row
    # and result must remain available without an external broker.
    second_queue = InProcessJobQueue(db_path)
    assert await second_queue.get_status(job_id) is JobStatus.COMPLETED
    assert await second_queue.get_result(job_id) == {"value": 42}


@pytest.mark.asyncio
async def test_job_queue_resumes_unfinished_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    InProcessJobQueue(db_path)
    job_id = "unfinished-job"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, "resume", "resume-1", '{"value": 99}', "processing", "2026-01-01"),
        )

    queue = InProcessJobQueue(db_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"value": payload["value"]}

    queue.register_handler("resume", handler)
    assert await queue.resume_pending() == [job_id]
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert await queue.get_result(job_id) == {"value": 99}


@pytest.mark.asyncio
async def test_resume_pending_jobs_requeues_only_pending_rows(tmp_path: Path) -> None:
    """D1: the operator entry point claims ``pending`` rows, never ``processing``."""
    db_path = tmp_path / "jobs.sqlite3"
    InProcessJobQueue(db_path)
    pending_id = "pending-job"
    processing_id = "busy-job"
    with sqlite3.connect(db_path) as connection:
        for job_id, status in ((pending_id, "pending"), (processing_id, "processing")):
            connection.execute(
                """
                INSERT INTO nexus_job_queue
                    (id, job_type, idempotency_key, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, "echo", f"seed-{job_id}", '{"value": 1}', status, "2026-01-01"),
            )

    queue = InProcessJobQueue(db_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"value": payload["value"]}

    queue.register_handler("echo", handler)
    resumed = await queue.resume_pending_jobs()

    assert resumed == [pending_id]
    await _wait_for_status(queue, pending_id, JobStatus.COMPLETED)
    # The processing row belongs to a (hypothetical) live owner process and
    # must not be claimed, re-run, or duplicated here.
    row = (
        sqlite3.connect(db_path)
        .execute("SELECT status FROM nexus_job_queue WHERE id = ?", (processing_id,))
        .fetchone()
    )
    assert row is not None
    assert row[0] == "processing"


def _job_completion_log(log: list[JobCompletion]) -> Any:
    async def hook(completion: JobCompletion) -> None:
        log.append(completion)

    return hook


@pytest.mark.asyncio
async def test_completion_hook_fires_on_terminal_states(tmp_path: Path) -> None:
    """D4: the queue notifies the injected hook on completed AND failed."""
    log: list[JobCompletion] = []
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=_job_completion_log(log))

    async def ok(payload: dict[str, object]) -> dict[str, object]:
        return {"echo": payload["value"]}

    async def boom(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(f"bad payload: {payload['value']}")

    queue.register_handler("ok", ok)
    queue.register_handler("boom", boom)
    ok_id = await queue.enqueue(
        job_type="ok", idempotency_key="ok-1", payload={"value": 1, "chat_id": 42}
    )
    failed_id = await queue.enqueue(
        job_type="boom", idempotency_key="boom-1", payload={"value": "x", "chat_id": 42}
    )

    await _wait_for_status(queue, ok_id, JobStatus.COMPLETED)
    await _wait_for_status(queue, failed_id, JobStatus.FAILED)
    # `enqueue` schedules one task per job, so the two jobs run concurrently and
    # the *order* of the notifications is a scheduling accident (it flips under
    # full-suite load). The contract is per job: each terminal state notifies
    # exactly once, with its own payload/result/error.
    assert len(log) == 2, f"expected one notification per terminal job, got {log}"
    by_job = {completion.job_id: completion for completion in log}
    assert set(by_job) == {ok_id, failed_id}, f"hook fired for the wrong jobs: {log}"
    completed, failed = by_job[ok_id], by_job[failed_id]
    assert completed.status is JobStatus.COMPLETED
    assert failed.status is JobStatus.FAILED
    assert completed.result == {"echo": 1}
    assert completed.payload == {"value": 1, "chat_id": 42}
    assert completed.error is None
    assert failed.result is None
    assert failed.error == "bad payload: x"


@pytest.mark.asyncio
async def test_completion_hook_is_fail_safe(tmp_path: Path) -> None:
    """D4: a broken notifier never corrupts the durable job state."""

    async def broken_hook(completion: JobCompletion) -> None:
        raise RuntimeError(f"telegram down (job {completion.job_id})")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=broken_hook)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"value": payload["value"]}

    queue.register_handler("ok", handler)
    job_id = await queue.enqueue(job_type="ok", idempotency_key="ok-1", payload={"value": 99})

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert await queue.get_result(job_id) == {"value": 99}


@pytest.mark.asyncio
async def test_job_queue_idempotency_returns_original_job(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    calls = 0

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": payload["value"]}

    queue.register_handler("idempotent", handler)
    first = await queue.enqueue(
        job_type="idempotent",
        idempotency_key="same-key",
        payload={"value": 1},
    )
    second = await queue.enqueue(
        job_type="idempotent",
        idempotency_key="same-key",
        payload={"value": 2},
    )

    assert second == first
    await _wait_for_status(queue, first, JobStatus.COMPLETED)
    assert calls == 1
    assert await queue.get_result(first) == {"value": 1}


@pytest.mark.asyncio
async def test_job_queue_failure_is_persisted(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    async def failing_handler(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(f"bad payload: {payload['value']}")

    queue.register_handler("failure", failing_handler)
    job_id = await queue.enqueue(
        job_type="failure",
        idempotency_key="failure-1",
        payload={"value": "boom"},
    )

    await _wait_for_status(queue, job_id, JobStatus.FAILED)
    assert await queue.get_result(job_id) is None

    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        row = connection.execute(
            "SELECT error FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "bad payload: boom"


@pytest.mark.asyncio
async def test_pdf_job_extracts_text_with_pypdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("pypdf")
    captured: dict[str, Any] = {}

    class FakeRag:
        def __init__(self) -> None:
            pass

        async def add_document(self, user_id: int, text: str, metadata: dict[str, object]) -> None:
            captured.update(user_id=user_id, text=text, metadata=metadata)

    fake_rag_module = types.ModuleType("nexus_ai_agent.features.rag")
    fake_rag_module.AdvancedRAGEngine = FakeRag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nexus_ai_agent.features.rag", fake_rag_module)
    source = tmp_path / "document.pdf"
    source.write_bytes((FIXTURES / "minimal.pdf").read_bytes())
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pdf-1",
        payload={
            "user_id": 7,
            "chat_id": 555,
            "file_path": str(source),
            "file_id": "file-7",
        },
    )

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert (await queue.get_result(job_id)) == {"message": "Successfully processed file-7"}
    assert captured == {
        "user_id": 7,
        "text": "Hello NEXUS job queue",
        "metadata": {"file_id": "file-7"},
    }


@pytest.mark.asyncio
async def test_extract_pdf_text_reads_text_layer(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    source = tmp_path / "doc.pdf"
    source.write_bytes((FIXTURES / "minimal.pdf").read_bytes())

    assert await extract_pdf_text(str(source)) == "Hello NEXUS job queue"


@pytest.mark.asyncio
async def test_extract_pdf_text_without_pypdf_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "binary.pdf"
    source.write_bytes(b"%PDF-1.4 definitely not utf-8 text \x00\x01\x02")

    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(RuntimeError, match=r"pip install pypdf"):
        await extract_pdf_text(str(source))


@pytest.mark.asyncio
async def test_pdf_job_without_pypdf_persists_clear_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "binary.pdf"
    source.write_bytes(b"%PDF-1.4 not readable as utf-8 \x00\x01\x02")
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)

    monkeypatch.setitem(sys.modules, "pypdf", None)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pdf-missing-dep",
        payload={"user_id": 1, "file_path": str(source), "file_id": "file-x"},
    )

    await _wait_for_status(queue, job_id, JobStatus.FAILED)
    row = (
        sqlite3.connect(tmp_path / "jobs.sqlite3")
        .execute("SELECT error FROM nexus_job_queue WHERE id = ?", (job_id,))
        .fetchone()
    )
    assert row is not None
    assert "pip install pypdf" in str(row[0])


@pytest.mark.asyncio
async def test_story_job_executes_through_in_process_queue(tmp_path: Path) -> None:
    output = tmp_path / "story.png"

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("story", generate_story_job)
    job_id = await queue.enqueue(
        job_type="story",
        idempotency_key="story-1",
        payload={"user_id": 9, "text": "Hello", "output_path": str(output)},
    )

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert (await queue.get_result(job_id)) == {"output_path": str(output)}
    assert output.is_file()
    assert output.stat().st_size > 0
