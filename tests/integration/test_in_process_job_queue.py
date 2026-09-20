"""Integration contract for the in-process durable job queue."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.worker import generate_story_job, process_pdf_job


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
async def test_pdf_job_executes_through_in_process_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    source.write_text("document body", encoding="utf-8")
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    queue.register_handler("pdf", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf",
        idempotency_key="pdf-1",
        payload={"user_id": 7, "file_path": str(source), "file_id": "file-7"},
    )

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert (await queue.get_result(job_id)) == {"message": "Successfully processed file-7"}
    assert captured == {
        "user_id": 7,
        "text": "document body",
        "metadata": {"file_id": "file-7"},
    }


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
