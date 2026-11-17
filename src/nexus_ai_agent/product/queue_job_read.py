"""Studio/legacy read adapter over the canonical durable job queue.

Single job authority law: ``InProcessJobQueue``'s durable rows (the
``{db_path}.jobs.sqlite3`` sidecar, the same file the bot/CLI composition
roots address) are the one and only job-state store for both execution
workers and the HTTP API.  This adapter exists so the Studio read port and
the legacy ``/creative/jobs/{id}`` endpoint keep their stable dictionaries
*without* a second database: every fact below is derived from the queue row
through ``get_job_facts`` (status spellings, fencing ``attempt``, payload,
result, error) — never from a copied or cached mirror.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

#: Legacy spelling map for the deprecated ``/creative/jobs/{id}`` consumers.
#: The canonical spellings stay untouched in ``.canonical_status`` so no
#: information is lost for newer readers.
_LEGACY_STATUS: dict[str, str] = {
    "pending": "pending",
    "processing": "processing",
    "verifying": "processing",
    "completed": "done",
    "failed_terminal": "failed",
    "failed_retryable": "failed",
    "cancelled": "failed",
}


class QueueJobReadAdapter:
    """Facade over the durable queue exposing the Studio read-port shape.

    ``create_job`` enqueues on the same queue the workers drain, so any job a
    client submits is immediately visible (with its fencing ``attempt``) to
    ``GET /api/studio/projects/{project_id}/jobs/{job_id}/state``.
    """

    def __init__(self, queue: InProcessJobQueue) -> None:
        self._queue = queue

    @property
    def queue(self) -> InProcessJobQueue:
        return self._queue

    async def create_job(self, job_type: str, input_data: dict[str, Any]) -> str:
        """Enqueue one job; the queue row *is* the job record."""
        return await self._queue.enqueue(
            job_type=job_type,
            idempotency_key=uuid4().hex,
            payload=input_data,
        )

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Project one authoritative queue row into the read dictionary."""
        facts = await self._queue.get_job_facts_async(job_id)
        if facts is None:
            return None
        status = facts.status
        return {
            "id": facts.job_id,
            "job_type": facts.job_type,
            "status": _LEGACY_STATUS.get(status, status),
            "canonical_status": status,
            "status_known": facts.status_known,
            "attempt": facts.attempt,
            "input_data": facts.payload,
            "result": facts.result,
            "error": facts.error,
            "created_at": facts.created_at,
            "updated_at": facts.finished_at or facts.started_at or facts.created_at,
        }


__all__ = ["QueueJobReadAdapter"]
