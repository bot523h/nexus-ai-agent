"""PostgreSQL-backed job queue — task-163, D-0010 / ADR 0005.

The application-owned queue with PostgreSQL as the durable hand-off point.
On the scale-to-zero path the queue rows must survive the container: a job
enqueued right before a kill is claimed and finished by the *next*
container, never lost and never double-executed.

Two concurrency properties, both in SQL:

* **claim** — ``UPDATE ... WHERE id = (SELECT id ... FOR UPDATE SKIP
  LOCKED LIMIT 1)``: two containers claiming the same moment take
  different rows (or one takes none); ``SKIP LOCKED`` means the loser
  skips the locked row instead of blocking.
* **stale steal** — a ``processing`` row whose owner has been dead for
  longer than the processing timeout (a killed container) returns to
  ``pending`` and becomes claimable again.  A live owner's row is never
  touched: the steal window (900 s) is far larger than any legitimate
  job, and the reference timestamp is the *claim* moment
  (``coalesce(locked_at, started_at, created_at)``) so even a crash
  between claim and handler start is covered.

Timestamps are TEXT (ISO-8601 UTC) — identical to the SQLite queue
(``adapters/in_process_job_queue.py``) — so both backends serialize jobs
the same way.  The table ``nexus_job_queue_pg`` is created by migration
``a41c9e2b7f63`` (PostgreSQL only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from collections.abc import Callable as _CallableType
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobStatus

logger = logging.getLogger(__name__)

__all__ = ["JOB_QUEUE_PG_TABLE", "JobCompletion", "PgJobQueue"]

#: Canonical table name (owned by migration ``a41c9e2b7f63``; also part of
#: the Postgres "head state" set in ``storage/adopt_pg.py``).
JOB_QUEUE_PG_TABLE = "nexus_job_queue_pg"

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]] | dict[str, object]]


@dataclass(frozen=True)
class JobCompletion:
    """Terminal-state snapshot (mirrors the in-process queue's shape)."""

    job_id: str
    job_type: str
    status: JobStatus
    result: dict[str, object] | None
    error: str | None
    payload: dict[str, object]


def _default_connector(url: str) -> Any:
    import psycopg  # noqa: PLC0415 — core dep, lazy like the adapters

    return psycopg.connect(url, autocommit=True)


def _is_transient(exc: BaseException) -> bool:
    import psycopg

    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PgJobQueue:
    """Application-owned durable jobs in PostgreSQL (SKIP LOCKED claims)."""

    def __init__(
        self,
        database_url: str,
        handlers: dict[str, JobHandler] | None = None,
        *,
        table: str = JOB_QUEUE_PG_TABLE,
        processing_timeout_seconds: float = 900.0,
        connection: Any | None = None,
        connector: _CallableType[[str], Any] | None = None,
    ) -> None:
        self._database_url = database_url
        self._table = table
        self._processing_timeout = float(processing_timeout_seconds)
        self._owner_id = f"pgq-{uuid.uuid4().hex[:10]}"
        self._connector = connector or _default_connector
        self._closed = False
        self._handlers: dict[str, JobHandler] = dict(handlers or {})
        # Lazy by design: construction must not require the database to be
        # reachable; the first statement connects.
        self._conn = connection

    # ── JobQueuePort (async facade over the sync core) ────────────────────

    async def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> str:
        """Persist a job (``pending``). Reusing the key returns the original id."""
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        return await asyncio.to_thread(self._enqueue_sync, normalized, idempotency_key, payload)

    async def get_status(self, job_id: str) -> JobStatus:
        row = await asyncio.to_thread(self._get_row, job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        try:
            return JobStatus(str(row["status"]))
        except ValueError as exc:
            raise RuntimeError(f"invalid persisted job status for {job_id}") from exc

    async def get_result(self, job_id: str) -> dict[str, object] | None:
        row = await asyncio.to_thread(self._get_row, job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        result = row["result"]
        if result is None:
            return None
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise RuntimeError(f"invalid persisted result for {job_id}")
        return {str(key): value for key, value in result.items()}

    async def process_next(self) -> JobCompletion | None:
        """Claim one job (stealing stale ones first) and run its handler."""
        return await asyncio.to_thread(self._claim_next)

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        self._handlers[normalized] = handler

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — shutdown path: never raise
            logger.debug("job queue close failed (already gone?)", exc_info=True)

    # ── sync core (run on worker threads / tests) ─────────────────────────

    def _enqueue_sync(self, job_type: str, idempotency_key: str, payload: dict[str, object]) -> str:
        job_id = uuid.uuid4().hex
        insert_sql = (
            f"INSERT INTO {self._table} "
            "(id, job_type, idempotency_key, payload, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'pending', %s)"
        )
        try:
            self._execute(insert_sql, (job_id, job_type, idempotency_key, payload, _now_iso()))
            return job_id
        except Exception as exc:
            import psycopg

            if not isinstance(exc, psycopg.errors.UniqueViolation):
                raise
            # Idempotent reuse: hand back the original job, no second effect.
            row = self._execute(
                f"SELECT id FROM {self._table} WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            return str(row[0]) if row else job_id

    def _get_row(self, job_id: str) -> dict[str, Any] | None:
        row = self._execute(
            f"SELECT id, job_type, status, result, payload FROM {self._table} WHERE id = %s",
            (job_id,),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "job_type": row[1],
            "status": row[2],
            "result": row[3],
            "payload": row[4],
        }

    def _claim_next(self) -> JobCompletion | None:
        """Steal stale rows, claim one pending row, run it, persist the outcome."""
        self._steal_stale()
        row = self._claim_one()
        if row is None:
            return None
        job_id, job_type, payload = str(row[0]), str(row[1]), row[2]
        if not isinstance(payload, dict):
            payload = json.loads(payload) if isinstance(payload, str) else {}
        handler = self._handlers.get(job_type)
        if handler is None:
            error = f"no handler registered for {job_type!r}"
            self._finish(job_id, None, error)
            return JobCompletion(
                job_id=job_id,
                job_type=job_type,
                status=JobStatus.FAILED,
                result=None,
                error=error,
                payload=payload,
            )
        try:
            result = handler(payload)
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
            if not isinstance(result, dict):
                raise TypeError("job handler must return a dict")
        except Exception as exc:  # noqa: BLE001 — the row must record the failure
            error = f"{type(exc).__name__}: {exc}"
            logger.error("job_failed job_id=%s job_type=%s", job_id, job_type, exc_info=True)
            self._finish(job_id, None, error)
            return JobCompletion(
                job_id=job_id,
                job_type=job_type,
                status=JobStatus.FAILED,
                result=None,
                error=error,
                payload=payload,
            )
        self._finish(job_id, result, None)
        return JobCompletion(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.COMPLETED,
            result=result,
            error=None,
            payload=payload,
        )

    def _claim_one(self) -> Any:
        """One row via ``FOR UPDATE SKIP LOCKED`` — concurrent claims never collide."""
        sql = (
            f"UPDATE {self._table} "
            "SET status = 'processing', owner_id = %s, locked_at = %s, started_at = %s "
            "WHERE id = ("
            f"  SELECT id FROM {self._table} WHERE status = 'pending' "
            "  ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
            ") "
            "RETURNING id, job_type, payload"
        )
        row = self._execute(sql, (self._owner_id, _now_iso(), _now_iso()))
        return row

    def _steal_stale(self) -> None:
        """Return over-timed ``processing`` rows to ``pending`` (crashed owner)."""
        sql = (
            f"UPDATE {self._table} "
            "SET status = 'pending', owner_id = NULL, locked_at = NULL "
            "WHERE status = 'processing' AND "
            "to_timestamp(coalesce(locked_at, started_at, created_at)) "
            "< now() - make_interval(secs => %s) "
            "RETURNING id"
        )
        stolen = self._execute(sql, (self._processing_timeout,))
        if stolen:
            logger.warning(
                "job_queue_stale_steal count=%d timeout_s=%s owner=%s",
                len(stolen),
                self._processing_timeout,
                self._owner_id,
            )

    def _finish(self, job_id: str, result: dict[str, object] | None, error: str | None) -> None:
        if result is not None:
            sql = (
                f"UPDATE {self._table} "
                "SET status = 'completed', result = %s, error = NULL, finished_at = %s "
                "WHERE id = %s"
            )
            self._execute(sql, (result, _now_iso(), job_id))
        else:
            sql = (
                f"UPDATE {self._table} "
                "SET status = 'failed', result = NULL, error = %s, finished_at = %s "
                "WHERE id = %s"
            )
            self._execute(sql, (error, _now_iso(), job_id))

    def _execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        """Run one statement; on a transient failure, reconnect once and retry."""
        try:
            return self._execute_noreconnect(sql, params)
        except Exception as exc:
            if not _is_transient(exc) or self._closed:
                raise
            logger.warning("job queue connection lost — reconnecting once", exc_info=True)
            self._reconnect()
            return self._execute_noreconnect(sql, params)

    def _execute_noreconnect(self, sql: str, params: tuple[Any, ...]) -> Any:
        if self._conn is None:
            if self._closed:
                raise RuntimeError("job queue is closed")
            self._conn = self._connector(self._database_url)
        cur = self._conn.execute(sql, params)
        if cur.description is not None:
            rows = cur.fetchall()
            if not rows:
                return None
            return rows[0] if len(rows) == 1 else rows
        return None

    def _reconnect(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001 — the old handle is what we are replacing
            pass
        self._conn = self._connector(self._database_url)
