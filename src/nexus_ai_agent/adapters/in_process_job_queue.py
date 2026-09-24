"""Durable, application-owned background jobs for the Modular Monolith.

The queue deliberately has no broker, worker process, or external service. A
small SQLite table is the durable hand-off point; execution is scheduled on
the current asyncio event loop. A new process can call ``resume_pending``
to continue rows left pending, processing, or verifying by an earlier
process, or ``resume_pending_jobs`` (CLI/operator entry point) to requeue
only rows still sitting in ``pending``. Terminal states fan out to an
optional, strictly fail-safe completion hook (job-finished notifications).

Canonical lifecycle (task-178, ``nexus_ai_agent.jobs.lifecycle``):

    pending → processing → verifying → completed
                 │             │
                 └─────────────┴──→ failed          (fail-closed)
                 └─────────────┴──→ pending         (cancel/recover)

Every edge is a guarded, status-conditioned UPDATE (compare-and-set, never a
blind write). For job types with a registered artifact verifier, a handler
result is NEVER trusted on its own: the queue moves the row to ``verifying``
and re-measures the claimed artifact independently (exists, size > 0,
sha256 recompute, expected-path containment, probe evidence for media).
Execution success + verification success = success eligibility; anything
else is terminal ``failed`` with a typed ``verification_failed:<code>``
error. Rows without a registered verifier keep the historical two-phase
semantics unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.verification import VerificationOutcome

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
JobCompletionHook = Callable[["JobCompletion"], Awaitable[None]]

#: A verifier re-measures a handler result against the filesystem. It must
#: be sync (the queue runs it in a worker thread) and side-effect free.
ArtifactVerifier = Callable[[dict[str, object], dict[str, object]], VerificationOutcome]

logger = logging.getLogger(__name__)

#: Queue-owned key under which the verification block is persisted inside
#: the result payload — the traceability link from Job Result back to the
#: verified artifact identities.
VERIFICATION_RESULT_KEY = "artifact_verification"


@dataclass(frozen=True)
class JobCompletion:
    """Terminal-state snapshot handed to the completion hook.

    ``payload`` is the original job payload, so hooks can route the
    notification (e.g. to the Telegram chat that enqueued the job).
    """

    job_id: str
    job_type: str
    status: JobStatus
    result: dict[str, object] | None
    error: str | None
    payload: dict[str, object]


def default_artifact_verifiers() -> dict[str, ArtifactVerifier]:
    """Built-in verifiers keyed by job type.

    ``creative_render`` — the canonical one-shot chain (/edit /caption
    /grade) — is verified by default: a lying, stale, truncated or
    zero-byte artifact can never complete a job. Job types without an
    entry keep the historical unverified semantics (registered verifiers
    are additive; nothing existing silently changes meaning).
    """
    from nexus_ai_agent.jobs.creative_verification import creative_render_verifier

    return {"creative_render": creative_render_verifier}


class InProcessJobQueue:
    """Persist jobs in SQLite and execute them in the current process."""

    def __init__(
        self,
        db_path: Path | str,
        handlers: dict[str, JobHandler] | None = None,
        *,
        on_job_finished: JobCompletionHook | None = None,
        artifact_verifiers: Mapping[str, ArtifactVerifier] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        self._on_job_finished = on_job_finished
        # ``artifact_verifiers=None`` installs the built-in registry
        # (creative_render verified by default); pass ``{}`` to opt out.
        self._artifact_verifiers: dict[str, ArtifactVerifier] = (
            dict(default_artifact_verifiers())
            if artifact_verifiers is None
            else dict(artifact_verifiers)
        )
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

    def register_artifact_verifier(self, job_type: str, verifier: ArtifactVerifier) -> None:
        """Register (or replace) the artifact verifier for ``job_type``."""
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        self._artifact_verifiers[normalized] = verifier

    async def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> str:
        """Persist a job and schedule it exactly once for this process.

        Reusing an idempotency key returns the original job id and does not
        create a second effect. If the same key arrives with a DIFFERENT
        payload, the original payload still wins (first dispatch wins — a
        retry can never smuggle a second, different effect under one key);
        the conflict is logged as a structured event so it is observable.
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

    async def get_result_chain(self, job_id: str) -> dict[str, object]:
        """The Result end of the canonical chain for one job.

        Assembles a ``jobs.lifecycle.JobResult`` from durable facts only:
        the row (status, attempt, error) plus the persisted payload and the
        queue-owned ``artifact_verification`` block. Raises ``KeyError``
        for unknown jobs, like ``get_status``.
        """
        from nexus_ai_agent.jobs.lifecycle import JobResult

        row = await asyncio.to_thread(self._fetch_row_full, job_id)
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        payload = json.loads(str(row["payload_json"]))
        result_json = row["result_json"]
        result = json.loads(str(result_json)) if result_json is not None else None
        verification = None
        if isinstance(result, dict) and isinstance(result.get(VERIFICATION_RESULT_KEY), dict):
            verification = dict(result[VERIFICATION_RESULT_KEY])
        job_result = JobResult.from_parts(
            job_id=job_id,
            payload=payload,
            result=result,
            attempt=int(row["attempt"] or 0),
            execution_status=str(row["status"]),
            verification=verification,
            error=str(row["error"]) if row["error"] is not None else None,
        )
        return job_result.to_dict()

    async def resume_pending(self) -> list[str]:
        """Requeue jobs left unfinished by a previous process."""
        job_ids = await asyncio.to_thread(self._reset_unfinished)
        for job_id in job_ids:
            self._schedule(job_id)
        return job_ids

    async def resume_pending_jobs(self) -> list[str]:
        """Requeue jobs currently sitting in ``pending`` and run them here.

        Operator/CLI entry point (D1: ``nexus jobs resume``). Unlike
        :meth:`resume_pending` — process-startup recovery, which also claims
        orphaned ``processing``/``verifying`` rows — this never touches
        in-flight rows: a live owner process (usually the bot) may still be
        executing those, and re-running them here would duplicate side
        effects.
        """
        job_ids = await asyncio.to_thread(self._select_pending)
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

        # Claim-time structural failure: PENDING → FAILED before any
        # reservation is useful (no side effect has occurred).
        handler = self._handlers.get(str(row["job_type"]))
        if handler is None:
            error = f"no handler registered for job type {row['job_type']}"
            await asyncio.to_thread(self._mark_failed, job_id, error)
            await self._notify_completion(
                JobCompletion(
                    job_id=job_id,
                    job_type=str(row["job_type"]),
                    status=JobStatus.FAILED,
                    result=None,
                    error=error,
                    payload={},
                )
            )
            return

        # Reservation: the RUNNING boundary. From here the handler owns the
        # execution; its own preflight (payload/capability/references)
        # precedes its first side effect by contract (jobs.lifecycle).
        await asyncio.to_thread(self._mark_processing, job_id)
        payload: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["payload_json"]))
            if not isinstance(parsed, dict):
                raise RuntimeError(f"invalid persisted payload for {job_id}")
            payload = {str(key): value for key, value in parsed.items()}
            result = await handler(payload)
            if not isinstance(result, dict):
                raise TypeError("job handler must return a dictionary")
        except asyncio.CancelledError:
            # Cancellation is a process-lifecycle event, not a business
            # failure. Keep the row recoverable for the next process.
            await asyncio.to_thread(self._mark_pending, job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - job failure must be persisted
            await asyncio.to_thread(self._mark_failed, job_id, str(exc))
            await self._notify_completion(
                JobCompletion(
                    job_id=job_id,
                    job_type=str(row["job_type"]),
                    status=JobStatus.FAILED,
                    result=None,
                    error=str(exc),
                    payload=payload,
                )
            )
            return

        verifier = self._artifact_verifiers.get(str(row["job_type"]))
        if verifier is None:
            # No verification contract for this job type: historical
            # semantics — the handler result is the completed result.
            final_result = result
            await asyncio.to_thread(self._mark_completed, job_id, result)
        else:
            # Execution success ≠ job success: verify the artifact
            # independently before the row may become terminal-success.
            if not await asyncio.to_thread(self._mark_verifying, job_id):
                return  # row was reclaimed/reset elsewhere; not ours anymore
            outcome = await self._verify_safely(verifier, payload, result)
            if outcome.ok:
                final_result = {**result, VERIFICATION_RESULT_KEY: outcome.block}
                await asyncio.to_thread(self._mark_completed, job_id, final_result)
            else:
                await asyncio.to_thread(
                    self._mark_failed, job_id, f"verification_failed:{outcome.reason_code}"
                )
                await self._notify_completion(
                    JobCompletion(
                        job_id=job_id,
                        job_type=str(row["job_type"]),
                        status=JobStatus.FAILED,
                        result=None,
                        error=f"verification_failed:{outcome.reason_code}",
                        payload=payload,
                    )
                )
                return

        await self._notify_completion(
            JobCompletion(
                job_id=job_id,
                job_type=str(row["job_type"]),
                status=JobStatus.COMPLETED,
                result=final_result,
                error=None,
                payload=payload,
            )
        )

    async def _verify_safely(
        self,
        verifier: ArtifactVerifier,
        payload: dict[str, object],
        result: dict[str, object],
    ) -> VerificationOutcome:
        """Run the verifier fail-closed: a crashing verifier fails the job.

        The verifier only reads the world (file bytes, probe); it never
        writes, so re-running verification after recovery is idempotent.
        """
        try:
            return await asyncio.to_thread(verifier, payload, result)
        except Exception as exc:  # noqa: BLE001 - fail-closed by contract
            logger.warning(
                "artifact verifier crashed; failing the job closed: %s", exc, exc_info=True
            )
            return VerificationOutcome(
                ok=False,
                reason_code="verifier_crashed",
                summary={"status": "failed", "reason_code": "verifier_crashed"},
            )

    async def _notify_completion(self, completion: JobCompletion) -> None:
        """Fan a terminal job state out to the injected hook (D4).

        Strictly fail-safe: the durable state is already persisted before
        this runs, so a broken notifier must never corrupt the queue or
        lose a job result.
        """
        if self._on_job_finished is None:
            return
        try:
            await self._on_job_finished(completion)
        except Exception:  # noqa: BLE001 - notifier must never break the queue
            logger.warning(
                "job completion notifier failed for job %s",
                completion.job_id,
                exc_info=True,
            )

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
            # task-178: per-row execution attempt counter (retry/revision
            # traceability). Guarded add for sidecars created before this
            # schema revision.
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(nexus_job_queue)")
            }
            if "attempt" not in columns:
                connection.execute(
                    "ALTER TABLE nexus_job_queue ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
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
                "SELECT id, payload_json FROM nexus_job_queue WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_id = str(existing[0])
                if str(existing[1]) != payload_json:
                    # First dispatch wins: the original payload keeps the
                    # key. Deterministic, observable, and a retry under the
                    # same key can never create a second, different effect.
                    logger.warning(
                        "job_idempotency_payload_conflict: key=%s existing_job=%s "
                        "original payload wins",
                        idempotency_key,
                        existing_id,
                    )
                return existing_id
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

    def _fetch_row_full(self, job_id: str) -> sqlite3.Row | None:
        with self._db_lock, self._connection() as connection:
            return connection.execute(
                """
                SELECT id, job_type, payload_json, status, result_json, error, attempt
                FROM nexus_job_queue WHERE id = ?
                """,
                (job_id,),
            ).fetchone()

    def _reset_unfinished(self) -> list[str]:
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM nexus_job_queue
                WHERE status IN (?, ?, ?)
                ORDER BY created_at, id
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                ),
            ).fetchall()
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = NULL
                WHERE status IN (?, ?, ?)
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                ),
            )
        return [str(row[0]) for row in rows]

    def _select_pending(self) -> list[str]:
        """Return ids of rows still in ``pending``, oldest first."""
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM nexus_job_queue
                WHERE status = ?
                ORDER BY created_at, id
                """,
                (JobStatus.PENDING.value,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def _mark_processing(self, job_id: str) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = ?, attempt = attempt + 1
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

    def _mark_verifying(self, job_id: str) -> bool:
        """PROCESSING → VERIFYING (guarded CAS). False if the row moved."""
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?
                WHERE id = ? AND status = ?
                """,
                (JobStatus.VERIFYING.value, job_id, JobStatus.PROCESSING.value),
            )
            return cursor.rowcount > 0

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
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.COMPLETED.value,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    _now(),
                    job_id,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                ),
            )

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._db_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    error,
                    _now(),
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                ),
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
