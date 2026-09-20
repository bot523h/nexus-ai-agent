"""In-process ``JobQueuePort`` adapter with SQLite durability (R-001 / R-026).

This is the Stage 0 implementation promised by ``docs/architecture/PORTS.md``
(*"JobQueue starts with an in-process implementation; Redis/Celery are not
part of Stage 0"*).  It replaces the Celery/Redis topology: the bot is a
modular monolith and background work runs inside the bot process.

Design
------
* One ``asyncio`` task per job, in the same process as the bot.  Handlers
  that do CPU-bound work push it to a worker thread themselves (see
  ``jobs.py``) so the event loop keeps serving updates.
* Durable state in a dedicated SQLite **sidecar file** — never a table in the
  Alembic-managed application schema.  This is the same convention as the
  creative job registry and the SQLite-path lifecycle index; there is no
  migration and no touch of the application database.  The only startup
  effect is ``CREATE TABLE IF NOT EXISTS`` inside that sidecar, executed
  lazily on first use.
* Idempotency: ``UNIQUE (job_type, idempotency_key)``.  Re-submitting the
  same key (e.g. a redelivered webhook update) returns the existing job id
  and has **no second effect** — the PORTS.md contract.
* Every persisted status change is an edge of the frozen domain state
  machine (``domain/policies/retention.py::ALLOWED_TRANSITIONS``), applied as
  a compare-and-set ``UPDATE ... WHERE status IN (...)`` so two runners can
  never both claim one job.  Only ``pending → running → succeeded | failed``
  and ``failed → retrying → running`` are used.
* Failures are recorded (``failed`` + ``"ExcType: message"``), logged, and
  never raised into the enqueuing coroutine.  Nothing is retried implicitly.
* Recovery after a process restart is **explicit** (``resume_pending()``);
  the adapter never re-dispatches work on its own at construction or on
  first use.  Rows left ``running`` by a dead process are treated as
  interrupted (``running → failed → retrying → running``) when resumed.
* Single-process ownership: the sidecar belongs to one bot process (that is
  the point of the monolith).  Two live processes sharing the file would
  misclassify each other's in-flight rows on ``resume_pending()``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import aiosqlite

from nexus_ai_agent.domain.policies.retention import ALLOWED_TRANSITIONS, JournalStatus
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

#: Job states reuse the frozen domain vocabulary rather than inventing a
#: parallel one.  ``JobStatus.SUCCEEDED`` is the terminal success state.
JobStatus = JournalStatus

JobPayload = dict[str, object]
JobHandler = Callable[[JobPayload], Awaitable[JobPayload | None]]

_TABLE = "jobs"
_CLAIMABLE = (JobStatus.PENDING, JobStatus.RETRYING)
_UNFINISHED = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRYING)
_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})
_INTERRUPTED = "interrupted: the process exited while the job was running"
_WAIT_POLL_SECONDS = 0.02

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id              TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL,
    payload         TEXT NOT NULL,
    result          TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (job_type, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
"""

_COLUMNS = (
    "id, job_type, idempotency_key, status, payload, result, error, attempts, "
    "created_at, updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class JobRecord:
    """Durable view of one job row (payload/result already decoded)."""

    job_id: str
    job_type: str
    idempotency_key: str
    status: JobStatus
    payload: JobPayload
    result: JobPayload | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str


def _record(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        job_id=row["id"],
        job_type=row["job_type"],
        idempotency_key=row["idempotency_key"],
        status=JobStatus(row["status"]),
        payload=json.loads(row["payload"]),
        result=json.loads(row["result"]) if row["result"] is not None else None,
        error=row["error"],
        attempts=int(row["attempts"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class InProcessJobQueue:
    """``JobQueuePort`` adapter: in-process execution, SQLite-durable state."""

    def __init__(self, db_path: str | Path) -> None:
        path = str(db_path)
        if path == ":memory:" or path.startswith("file::memory:"):
            raise ValueError(
                "InProcessJobQueue needs a file path; ':memory:' is not durable across connections"
            )
        self._db_path = Path(path)
        self._handlers: dict[str, JobHandler] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._init_lock = asyncio.Lock()
        self._initialized = False

    # ── Introspection ────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def handlers(self) -> Mapping[str, JobHandler]:
        return MappingProxyType(self._handlers)

    @property
    def in_flight(self) -> int:
        """Jobs currently being executed by *this* process."""
        return len(self._tasks)

    # ── Registration / schema ────────────────────────────────────────

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Bind a job type to its handler.  Types are persisted: keep them stable."""
        if not job_type:
            raise ValueError("job_type must be a non-empty string")
        if job_type in self._handlers:
            raise ValueError(f"handler already registered for job type {job_type!r}")
        self._handlers[job_type] = handler

    async def initialize(self) -> None:
        """Create the sidecar schema (idempotent).  Called lazily by every operation."""
        async with self._init_lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._connect() as db:
                await db.executescript(_SCHEMA)
                await db.commit()
            self._initialized = True

    def _connect(self) -> aiosqlite.Connection:
        # ``timeout`` is SQLite's busy timeout: short overlapping writers
        # (runner vs. ``wait()`` poller) wait instead of failing.
        return aiosqlite.connect(self._db_path, timeout=5.0)

    # ── JobQueuePort ─────────────────────────────────────────────────

    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str:
        """Persist a job and start it in-process; return its id.

        Raises ``LookupError`` for an unregistered job type and ``TypeError``
        for a non-JSON payload — both *before* anything is written, so a
        submission nobody can run never becomes a dead row.
        """
        if job_type not in self._handlers:
            raise LookupError(f"no handler registered for job type {job_type!r}")
        if not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        encoded = json.dumps(payload)
        await self.initialize()

        job_id = uuid4().hex
        now = _now()
        async with self._connect() as db:
            existing = await self._find(db, job_type, idempotency_key)
            if existing is not None:
                return existing
            try:
                await db.execute(
                    f"INSERT INTO {_TABLE} (id, job_type, idempotency_key, status, payload, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, job_type, idempotency_key, JobStatus.PENDING.value, encoded, now, now),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                # Lost the race against a concurrent duplicate: its row is the job.
                existing = await self._find(db, job_type, idempotency_key)
                if existing is None:
                    raise
                return existing
        self._dispatch(job_id)
        return job_id

    async def get_status(self, job_id: str) -> str:
        """Persisted status of a job; ``KeyError`` for an unknown id."""
        record = await self.get_job(job_id)
        if record is None:
            raise KeyError(job_id)
        return record.status

    # ── Adapter-level extras (not part of the Stage 0 port) ──────────

    async def get_job(self, job_id: str) -> JobRecord | None:
        await self.initialize()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def get_result(self, job_id: str) -> dict[str, object] | None:
        """Decoded result of a succeeded job; ``None`` otherwise."""
        record = await self.get_job(job_id)
        return record.result if record is not None else None

    async def wait(self, job_id: str, *, timeout: float) -> JobStatus:
        """Block until the job reaches a terminal state (``TimeoutError`` otherwise)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.wait({task}, timeout=timeout)
        while True:
            status = JobStatus(await self.get_status(job_id))
            if status in _TERMINAL:
                return status
            if loop.time() >= deadline:
                raise TimeoutError(f"job {job_id} still {status} after {timeout}s")
            await asyncio.sleep(_WAIT_POLL_SECONDS)

    async def resume_pending(self) -> list[str]:
        """Explicitly re-dispatch unfinished rows left behind by a previous process.

        Never called implicitly.  ``pending``/``retrying`` rows are dispatched
        as they are; ``running`` rows are interrupted work and go through the
        legal ``running → failed → retrying`` edges first (attempt count and
        error stay visible).  Rows already in flight here are skipped.
        """
        await self.initialize()
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT id, status FROM {_TABLE} WHERE status IN (?, ?, ?) "
                "ORDER BY created_at, rowid",
                tuple(status.value for status in _UNFINISHED),
            )
            rows = await cursor.fetchall()

        resumed: list[str] = []
        for job_id, status in rows:
            if job_id in self._tasks:
                continue
            if status == JobStatus.RUNNING.value:
                failed = await self._transition(
                    job_id, expected=(JobStatus.RUNNING,), to=JobStatus.FAILED, error=_INTERRUPTED
                )
                retrying = failed and await self._transition(
                    job_id, expected=(JobStatus.FAILED,), to=JobStatus.RETRYING, error=_INTERRUPTED
                )
                if not retrying:
                    continue
            self._dispatch(job_id)
            resumed.append(job_id)
        return resumed

    async def close(self, *, timeout: float = 30.0) -> None:
        """Drain in-flight jobs for up to ``timeout`` seconds, then cancel the rest.

        Cancelled jobs keep their ``running`` row (truthful: interrupted) and
        are eligible for an explicit ``resume_pending()`` later.
        """
        tasks = [task for task in self._tasks.values() if not task.done()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            log.warning("job_queue_close_cancelling_in_flight", count=len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ── Internals ────────────────────────────────────────────────────

    async def _find(
        self, db: aiosqlite.Connection, job_type: str, idempotency_key: str
    ) -> str | None:
        cursor = await db.execute(
            f"SELECT id FROM {_TABLE} WHERE job_type = ? AND idempotency_key = ?",
            (job_type, idempotency_key),
        )
        row = await cursor.fetchone()
        return str(row[0]) if row is not None else None

    def _dispatch(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id), name=f"job:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(functools.partial(self._forget, job_id))

    def _forget(self, job_id: str, _task: asyncio.Task[None]) -> None:
        self._tasks.pop(job_id, None)

    async def _run(self, job_id: str) -> None:
        record = await self.get_job(job_id)
        if record is None:
            log.error("job_vanished_before_run", job_id=job_id)
            return
        claimed = await self._transition(job_id, expected=_CLAIMABLE, to=JobStatus.RUNNING)
        if not claimed:
            # Another runner owns it, or it already reached a terminal state.
            return

        handler = self._handlers.get(record.job_type)
        if handler is None:
            message = f"no handler registered for job type {record.job_type!r}"
            log.error("job_failed", job_id=job_id, job_type=record.job_type, error=message)
            await self._transition(
                job_id, expected=(JobStatus.RUNNING,), to=JobStatus.FAILED, error=message
            )
            return

        try:
            result = await handler(dict(record.payload))
            encoded = json.dumps(result) if result is not None else None
        except asyncio.CancelledError:
            # Shutdown/crash semantics: the row stays ``running`` on disk and is
            # classified as interrupted by an explicit ``resume_pending()``.
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.error("job_failed", job_id=job_id, job_type=record.job_type, error=message)
            await self._transition(
                job_id, expected=(JobStatus.RUNNING,), to=JobStatus.FAILED, error=message
            )
            return

        await self._transition(
            job_id, expected=(JobStatus.RUNNING,), to=JobStatus.SUCCEEDED, result=encoded
        )
        log.info("job_succeeded", job_id=job_id, job_type=record.job_type)

    async def _transition(
        self,
        job_id: str,
        *,
        expected: Iterable[JobStatus],
        to: JobStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Compare-and-set status change along an ``ALLOWED_TRANSITIONS`` edge.

        Returns ``False`` when the row was not in one of ``expected`` states
        (someone else moved it first).  Claiming (``to == RUNNING``) clears the
        previous attempt's result/error and bumps ``attempts``.
        """
        sources = tuple(expected)
        illegal = [source for source in sources if to not in ALLOWED_TRANSITIONS[source]]
        if illegal:
            raise ValueError(f"illegal job transition {illegal} -> {to}")
        placeholders = ", ".join("?" for _ in sources)
        bump = 1 if to is JobStatus.RUNNING else 0
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE {_TABLE} SET status = ?, result = ?, error = ?, "
                f"attempts = attempts + ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (to.value, result, error, bump, _now(), job_id, *(s.value for s in sources)),
            )
            await db.commit()
            return cursor.rowcount == 1
