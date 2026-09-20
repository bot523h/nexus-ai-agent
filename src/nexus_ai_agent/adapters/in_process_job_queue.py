"""Durable, application-owned background jobs for the Modular Monolith.

The queue deliberately has no broker, worker process, or external service. A
small SQLite table is the durable hand-off point; execution is scheduled on
the current asyncio event loop. A new process can call ``resume_pending``
to continue rows left pending or processing by an earlier process.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from nexus_ai_agent.application.ports.job_queue import JobStatus

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


class InProcessJobQueue:
    """Persist jobs in SQLite and execute them in the current process."""

    def __init__(
        self,
        db_path: Path | str,
        handlers: dict[str, JobHandler] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        if self._sqlite_path != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._handlers: dict[str, JobHandler] = dict(handlers or {})
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._db_lock = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        self._initialize_db()

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        """Register the explicit application handler for ``job_type``."""
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        self._handlers[normalized] = handler

    async def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> str:
        """Persist a job and schedule it exactly once for this process.

        Reusing an idempotency key returns the original job id and does not
        create a second effect.
        """
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")

        job_id = await asyncio.to_thread(
            self._insert_or_get,
            normalized,
            idempotency_key,
            payload,
        )
        self._schedule(job_id)
        return job_id

    async def get_status(self, job_id: str) -> JobStatus:
        row = await asyncio.to_thread(self._fetch_row, job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        try:
            return JobStatus(str(row["status"]))
        except ValueError as exc:
            raise RuntimeError(f"invalid persisted job status for {job_id}") from exc

    async def get_result(self, job_id: str) -> dict[str, object] | None:
        row = await asyncio.to_thread(self._fetch_row, job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        result_json = row["result_json"]
        if result_json is None:
            return None
        result = json.loads(str(result_json))
        if not isinstance(result, dict):
            raise RuntimeError(f"invalid persisted result for {job_id}")
        return {str(key): value for key, value in result.items()}

    async def resume_pending(self) -> list[str]:
        """Requeue jobs left unfinished by a previous process."""
        job_ids = await asyncio.to_thread(self._reset_unfinished)
        for job_id in job_ids:
            self._schedule(job_id)
        return job_ids

    async def shutdown(self) -> None:
        """Cancel local tasks and leave them recoverable as pending jobs."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self._reset_unfinished)

    def _schedule(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._process_job(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def _process_job(self, job_id: str) -> None:
        row = await asyncio.to_thread(self._fetch_row, job_id)
        if row is None:
            return
        if row["status"] in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
            return

        await asyncio.to_thread(self._mark_processing, job_id)
        handler = self._handlers.get(str(row["job_type"]))
        try:
            if handler is None:
                raise RuntimeError(f"no handler registered for job type {row['job_type']}")
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid persisted payload for {job_id}")
            result = await handler({str(key): value for key, value in payload.items()})
            if not isinstance(result, dict):
                raise TypeError("job handler must return a dictionary")
            await asyncio.to_thread(self._mark_completed, job_id, result)
        except asyncio.CancelledError:
            # Cancellation is a process-lifecycle event, not a business
            # failure. Keep the row recoverable for the next process.
            await asyncio.to_thread(self._mark_pending, job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - job failure must be persisted
            await asyncio.to_thread(self._mark_failed, job_id, str(exc))

    def _initialize_db(self) -> None:
        """Create only the queue-owned table; no application migration is run."""
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_job_queue (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )

    def _insert_or_get(
        self,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> str:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        job_id = uuid4().hex
        with self._db_lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM nexus_job_queue WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            connection.execute(
                """
                INSERT INTO nexus_job_queue
                    (id, job_type, idempotency_key, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    idempotency_key,
                    payload_json,
                    JobStatus.PENDING.value,
                    _now(),
                ),
            )
        return job_id

    def _fetch_row(self, job_id: str) -> sqlite3.Row | None:
        with self._db_lock, self._connection() as connection:
            return connection.execute(
                """
                SELECT id, job_type, payload_json, status, result_json, error
                FROM nexus_job_queue WHERE id = ?
                """,
                (job_id,),
            ).fetchone()

    def _reset_unfinished(self) -> list[str]:
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM nexus_job_queue
                WHERE status IN (?, ?)
                ORDER BY created_at, id
                """,
                (JobStatus.PENDING.value, JobStatus.PROCESSING.value),
            ).fetchall()
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = NULL
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                ),
            )
        return [str(row[0]) for row in rows]

    def _mark_processing(self, job_id: str) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.PROCESSING.value,
                    _now(),
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                ),
            )

    def _mark_pending(self, job_id: str) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = NULL
                WHERE id = ?
                """,
                (JobStatus.PENDING.value, job_id),
            )

    def _mark_completed(self, job_id: str, result: dict[str, object]) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, result_json = ?, error = NULL, finished_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    _now(),
                    job_id,
                ),
            )

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (JobStatus.FAILED.value, error, _now(), job_id),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            try:
                yield self._memory_connection
            except Exception:
                self._memory_connection.rollback()
                raise
            else:
                self._memory_connection.commit()
            return

        connection = sqlite3.connect(self._sqlite_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
