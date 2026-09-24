"""Durable, application-owned background jobs for the Modular Monolith.

The queue deliberately has no broker, worker process, or external service. A
small SQLite table is the durable hand-off point; execution is scheduled on
the current asyncio event loop. A new process can call ``resume_pending``
to continue rows left pending, processing, or verifying by an earlier
process, or ``resume_pending_jobs`` (CLI/operator entry point) to requeue
only rows still sitting in ``pending``. Terminal states fan out to an
optional, strictly fail-safe completion hook (job-finished notifications).

Canonical lifecycle (task-178, failure taxonomy task-181):

    pending → processing → verifying → completed
                 │             │
                 ├─────────────┴──→ failed_retryable   (classified RETRYABLE)
                 ├─────────────┴──→ failed_terminal    (classified TERMINAL)
                 └─────────────┴──→ pending            (cancel/recover)

Every edge is a guarded, status-conditioned UPDATE (compare-and-set, never a
blind write). For job types with a registered artifact verifier, a handler
result is NEVER trusted on its own: the queue moves the row to ``verifying``
and re-measures the claimed artifact independently (exists, size > 0,
sha256 recompute, expected-path containment, probe evidence for media).
Execution success + verification success = success eligibility.  For lanes
whose artifact destination lives outside the job workspace (``pdf_extract``)
the order is stage → verify → **publish atomically** → **re-probe the
published bytes** → persist success (registered
:class:`ArtifactPublication`); a refusal retracts the staged temp and leaves
any previous published artifact untouched.

Failure semantics (task-181, GAP-A): a typed user failure
(``{"success": False, "error_code": ...}``) is a FAILURE of the job — the
queue classifies it and persists ``failed_retryable``/``failed_terminal``
with a ``typed_failure:<code>`` error (result payload preserved for the
notifier/audit).  Verification refusals persist
``verification_failed:<code>`` in a failure state the same way.  The two
failure states record the classifier's verdict
(``jobs/failure_semantics``); there is no retry scheduler — both are
terminal as implemented.  Rows without a registered verifier keep the
historical two-phase semantics otherwise unchanged.

Trace (task-181): every event emitted while a job runs is bound to its
``job_id`` (structlog contextvars — the worker's events carry it
automatically) and the queue's own lifecycle lines name it explicitly.

Execution ownership (task-181 Gate-5 repair): every reservation mints one
fenced **execution generation** — ``attempt`` increments (monotone fence) and
a fresh ``owner_token`` is stored (durable identity), see
``jobs.fencing.ExecutionToken``.  Every post-reservation transition is a
guarded UPDATE carrying that token (``_fence_update``): a displaced or
cancelled worker matches zero rows and can never move a newer execution's
state, publish over its artifact, or announce its outcome.  Reservation is a
strict ``PENDING → PROCESSING`` CAS (a live ``processing`` row is never
re-claimed as a fresh execution); takeover happens only through startup
recovery of *lease-expired* rows (``started_at`` older than the lease TTL)
and invalidates the old ``owner_token`` when it reclaims.  Terminal fan-out
(notify) fires only when the fenced commit/failure UPDATE actually applied —
one truthful notification per durable outcome, never a success announcement
for an outcome that did not commit."""

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

import structlog

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.failure_semantics import (
    FailureClass,
    classify_exception,
    classify_typed_code,
    classify_verification_reason,
    failure_status,
    is_typed_user_failure,
    typed_failure_error,
)
from nexus_ai_agent.jobs.fencing import (
    DEFAULT_LEASE_TTL_SECONDS,
    PUBLICATION_JOURNAL_KEY,
    ExecutionToken,
    lease_cutoff,
)
from nexus_ai_agent.jobs.lifecycle import parse_job_status
from nexus_ai_agent.jobs.verification import VerificationOutcome

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
JobCompletionHook = Callable[["JobCompletion"], Awaitable[None]]

#: A verifier re-measures a handler result against the filesystem. It must
#: be sync (the queue runs it in a worker thread) and side-effect free.
ArtifactVerifier = Callable[[dict[str, object], dict[str, object]], VerificationOutcome]


@dataclass(frozen=True)
class ArtifactPublication:
    """Queue-owned publication step for lanes with an external destination.

    ``publish`` atomically moves the verified staged artifact to its final
    name and returns the result patch (``artifact_path`` → published).  It
    MUST preserve whatever it replaces: back the destination up before the
    swap and journal the swap under :data:`PUBLICATION_JOURNAL_KEY`
    (``backup`` path + ``published_inode``) so a later rollback or crash
    recovery can find the pre-swap bytes.

    ``retract`` undoes this attempt's publication namespace after a refusal:
    the staged temp, and — if this attempt already swapped and the
    destination is still *this* swap's inode — the destination is restored
    from the backup.  A newer owner's publication is never overwritten
    (inode guard: side-effect fencing for the filesystem).

    ``recover`` (optional) resolves crash windows of a *previous* execution
    before a new one starts: with a journal present and the swap
    uncommitted, the pre-swap bytes are restored (inode-guarded); a stray
    backup without a journal is a duplicate and is removed.

    ``retire`` (optional) runs after the fenced commit confirmed: the backup
    is dead weight once the swap is durable and is removed.

    ``publish``/``retract``/``recover``/``retire`` run in worker threads and
    must be idempotent (recovery may re-run them).
    """

    publish: Callable[[dict[str, object], dict[str, object]], dict[str, object]]
    retract: Callable[[dict[str, object], dict[str, object]], None]
    recover: Callable[[dict[str, object], dict[str, object] | None], None] | None = None
    retire: Callable[[dict[str, object], dict[str, object]], None] | None = None


def default_artifact_publications() -> dict[str, ArtifactPublication]:
    """Built-in publications keyed by job type (task-181).

    Only ``pdf_extract`` — the single lane whose artifact destination
    (``<stem>.extracted.txt`` sidecar) lives *outside* the job workspace.
    Workspace-scoped lanes (creative_render / slideshow_render / story) are
    published at delivery, after verification, by the completion notifier.
    """
    from nexus_ai_agent.jobs.feature_verification import (
        publish_pdf_text_artifact,
        recover_pdf_text_publication,
        retire_pdf_text_artifact,
        retract_pdf_text_artifact,
    )

    return {
        "pdf_extract": ArtifactPublication(
            publish=publish_pdf_text_artifact,
            retract=retract_pdf_text_artifact,
            recover=recover_pdf_text_publication,
            retire=retire_pdf_text_artifact,
        ),
    }


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

    Every job type in ``worker.default_job_handlers`` is verified by
    default (task-178 established the contract with ``creative_render``;
    task-180 closed the remaining GAPs):

    * ``creative_render`` — the canonical one-shot chain (/edit /caption
      /grade);
    * ``slideshow_render`` — the Wave 2.5 render lane (GAP-A);
    * ``story`` — the locally rendered PNG story image (GAP-C);
    * ``pdf_extract`` — the persisted extracted-text artifact (GAP-B).

    A lying, stale, truncated or zero-byte artifact can never complete a
    job. The registry stays additive: ``tests/architecture/
    test_verification_registry_ratchet.py`` fails if a handler is ever
    registered without a verifier, so no job type can silently regress to
    the historical unverified semantics.
    """
    from nexus_ai_agent.jobs.creative_verification import (
        creative_render_verifier,
        slideshow_render_verifier,
    )
    from nexus_ai_agent.jobs.feature_verification import pdf_extract_verifier, story_verifier

    return {
        "creative_render": creative_render_verifier,
        "slideshow_render": slideshow_render_verifier,
        "story": story_verifier,
        "pdf_extract": pdf_extract_verifier,
    }


class InProcessJobQueue:
    """Persist jobs in SQLite and execute them in the current process."""

    def __init__(
        self,
        db_path: Path | str,
        handlers: dict[str, JobHandler] | None = None,
        *,
        on_job_finished: JobCompletionHook | None = None,
        artifact_verifiers: Mapping[str, ArtifactVerifier] | None = None,
        artifact_publications: Mapping[str, ArtifactPublication] | None = None,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        self._on_job_finished = on_job_finished
        self._lease_ttl_seconds = float(lease_ttl_seconds)
        # ``artifact_verifiers=None`` installs the built-in registry
        # (creative_render verified by default); pass ``{}`` to opt out.
        self._artifact_verifiers: dict[str, ArtifactVerifier] = (
            dict(default_artifact_verifiers())
            if artifact_verifiers is None
            else dict(artifact_verifiers)
        )
        # ``artifact_publications=None`` installs the built-in registry
        # (pdf_extract staged publication); pass ``{}`` to opt out.
        self._artifact_publications: dict[str, ArtifactPublication] = (
            dict(default_artifact_publications())
            if artifact_publications is None
            else dict(artifact_publications)
        )
        if self._sqlite_path != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._handlers: dict[str, JobHandler] = dict(handlers or {})
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: Execution generations minted by THIS instance and not yet
        #: terminal/released — shutdown may release only these (fenced
        #: self-release); rows of other processes are never touched.
        self._live_tokens: dict[str, ExecutionToken] = {}
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

    def register_artifact_publication(
        self, job_type: str, publication: ArtifactPublication
    ) -> None:
        """Register (or replace) the publication step for ``job_type``."""
        normalized = job_type.strip()
        if not normalized:
            raise ValueError("job_type must not be empty")
        self._artifact_publications[normalized] = publication

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
            return parse_job_status(str(row["status"]))
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
            execution_status=parse_job_status(str(row["status"])).value,
            verification=verification,
            error=str(row["error"]) if row["error"] is not None else None,
        )
        return job_result.to_dict()

    async def resume_pending(self) -> list[str]:
        """Continue unfinished work under the execution-ownership contract.

        Two disjoint sets are scheduled: rows sitting in ``pending`` (never
        reserved, or self-released/reclaimed), and rows a previous process
        left in ``processing``/``verifying`` **whose lease expired**
        (``started_at`` older than the lease TTL) — startup recovery reclaims
        those and invalidates the displaced holder's ``owner_token`` so every
        late write of the stale worker matches zero rows.  A live owner's
        fresh-lease row is never taken over here (that is what makes takeover
        explicit and fenced rather than a blind reset).
        """
        reclaimed = await asyncio.to_thread(self._reclaim_stale)
        pending = await asyncio.to_thread(self._select_pending)
        job_ids = list(dict.fromkeys([*pending, *reclaimed]))
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
        """Cancel local tasks and release only the rows this instance owns.

        Cancellation is a process-lifecycle event: each cancelled task
        self-releases its own fenced generation (``_mark_pending`` with its
        token).  The sweep afterwards releases only generations still in
        :attr:`_live_tokens` — other processes' rows are never reset here.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self._release_owned)

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
        try:
            if parse_job_status(str(row["status"])) in {
                JobStatus.COMPLETED,
                JobStatus.FAILED_RETRYABLE,
                JobStatus.FAILED_TERMINAL,
            }:
                return
        except ValueError:
            return  # unknown durable spelling: never execute it (fail-closed)

        # Trace (task-181): bind the durable job id for everything that runs
        # under this job — the handler's structlog events (worker) and the
        # queue's own lifecycle lines all carry it.  Task-local (each
        # _process_job runs as its own asyncio task), so concurrent jobs can
        # never cross-bind.
        structlog.contextvars.bind_contextvars(job_id=job_id)
        job_type = str(row["job_type"])

        # Claim-time structural failure: PENDING → FAILED_* before any
        # reservation is useful (no side effect has occurred).  "No handler"
        # is a deploy-level defect: the identical job cannot succeed until
        # the code changes ⇒ TERMINAL.
        handler = self._handlers.get(job_type)
        if handler is None:
            error = f"no handler registered for job type {row['job_type']}"
            status = failure_status(FailureClass.TERMINAL)
            logger.info(
                "job_failed job_id=%s type=%s status=%s error=%r",
                job_id,
                job_type,
                status.value,
                error,
            )
            if await asyncio.to_thread(self._mark_failed, job_id, error, status):
                await self._notify_completion(
                    JobCompletion(
                        job_id=job_id,
                        job_type=job_type,
                        status=status,
                        result=None,
                        error=error,
                        payload={},
                    )
                )
            return

        # Reservation: the RUNNING boundary.  Strict CAS PENDING→PROCESSING
        # mints this execution's generation (``attempt`` + fresh owner token).
        # ``None`` means the row is no longer claimable — another execution
        # owns it (or it moved terminal) and this caller must not proceed.
        token = await asyncio.to_thread(self._mark_processing, job_id)
        if token is None:
            logger.info(
                "job_claim_refused job_id=%s type=%s (row not PENDING: not ours to execute)",
                job_id,
                job_type,
            )
            return
        logger.info(
            "job_processing job_id=%s type=%s attempt=%s",
            job_id,
            job_type,
            token.generation,
        )
        payload: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["payload_json"]))
            if not isinstance(parsed, dict):
                raise RuntimeError(f"invalid persisted payload for {job_id}")
            payload = {str(key): value for key, value in parsed.items()}
        except Exception as exc:  # noqa: BLE001 - invalid payload is a durable failure
            status = failure_status(classify_exception(exc))
            if await asyncio.to_thread(self._mark_failed, job_id, str(exc), status, None, token):
                await self._notify_completion(
                    JobCompletion(
                        job_id=job_id,
                        job_type=job_type,
                        status=status,
                        result=None,
                        error=str(exc),
                        payload=payload,
                    )
                )
            return

        # Publication journal recovery (fenced side-effect repair): a
        # predecessor that crashed mid-publication leaves a journaled swap;
        # the pre-swap bytes are restored before this generation runs.
        publication = self._artifact_publications.get(job_type)
        if publication is not None and publication.recover is not None:
            full_row = await asyncio.to_thread(self._fetch_row_full, job_id)
            prior_result: dict[str, object] | None = None
            if full_row is not None and full_row["result_json"]:
                try:
                    loaded = json.loads(str(full_row["result_json"]))
                    if isinstance(loaded, dict):
                        prior_result = loaded
                except ValueError:
                    prior_result = None
            await asyncio.to_thread(publication.recover, payload, prior_result)

        try:
            result = await handler(payload)
            if not isinstance(result, dict):
                raise TypeError("job handler must return a dictionary")
        except asyncio.CancelledError:
            # Cancellation is a process-lifecycle event, not a business
            # failure. Fenced self-release: only THIS generation may go back
            # to pending — a stale cancellation can never reopen a newer
            # execution (F2).
            await asyncio.to_thread(self._mark_pending, job_id, token)
            raise
        except Exception as exc:  # noqa: BLE001 - job failure must be persisted
            # Fail-closed conversion: classify and persist a failure status.
            status = failure_status(classify_exception(exc))
            logger.info(
                "job_failed job_id=%s type=%s status=%s error=%r",
                job_id,
                job_type,
                status.value,
                str(exc),
            )
            if await asyncio.to_thread(self._mark_failed, job_id, str(exc), status, None, token):
                await self._notify_completion(
                    JobCompletion(
                        job_id=job_id,
                        job_type=job_type,
                        status=status,
                        result=None,
                        error=str(exc),
                        payload=payload,
                    )
                )
            return

        # GAP-A (task-181): a typed user failure is a FAILURE of the job —
        # never COMPLETED, whatever any verifier would answer about it.  The
        # typed result payload is preserved (error_code/dialect) so the
        # notifier can translate it and the audit chain can read it.
        if is_typed_user_failure(result):
            code = str(result.get("error_code") or "untyped_failure")
            status = failure_status(classify_typed_code(code))
            error = typed_failure_error(code)
            logger.info(
                "job_failed job_id=%s type=%s status=%s error=%s",
                job_id,
                job_type,
                status.value,
                error,
            )
            if await asyncio.to_thread(self._mark_failed, job_id, error, status, result, token):
                await self._notify_completion(
                    JobCompletion(
                        job_id=job_id,
                        job_type=job_type,
                        status=status,
                        result=result,
                        error=error,
                        payload=payload,
                    )
                )
            return

        verifier = self._artifact_verifiers.get(job_type)
        if verifier is None:
            # No verification contract for this job type: historical
            # semantics — the handler result is the completed result.
            final_result = result
            committed = await asyncio.to_thread(self._mark_completed, job_id, result, token)
            if not committed:
                # Fenced commit refused: a newer generation owns the row.
                # No success fan-out may claim an outcome that did not
                # commit (F3 / T15) — the current owner announces its own.
                logger.info("job_commit_refused job_id=%s type=%s", job_id, job_type)
                return
        else:
            # Execution success ≠ job success: verify the artifact
            # independently before the row may become terminal-success.
            if not await asyncio.to_thread(self._mark_verifying, job_id, token):
                return  # row was reclaimed/reset elsewhere; not ours anymore
            logger.info("job_verifying job_id=%s type=%s", job_id, job_type)
            outcome = await self._verify_safely(verifier, payload, result)
            if outcome.ok and publication is not None:
                # Stage → verify → **publish atomically** (previous artifact
                # backed up + swap journaled) → **re-probe** → fenced commit
                # → retire backup → notify.  Any refusal after the swap
                # restores the pre-swap bytes (inode-guarded): a refused
                # publication can no longer destroy the previous artifact.
                result, outcome = await self._publish_and_reprobe(
                    publication, verifier, payload, result, job_id, token
                )
            if outcome.ok:
                final_result = {**result, VERIFICATION_RESULT_KEY: outcome.block}
                committed = await asyncio.to_thread(
                    self._mark_completed, job_id, final_result, token
                )
                if not committed:
                    # Fenced commit refused mid-publication: roll the swap
                    # back (inode-guarded) and stay silent — this generation
                    # owns nothing anymore (F3 / T8).
                    if publication is not None:
                        await asyncio.to_thread(publication.retract, payload, result)
                    logger.info("job_commit_refused job_id=%s type=%s", job_id, job_type)
                    return
                if publication is not None and publication.retire is not None:
                    await asyncio.to_thread(publication.retire, payload, final_result)
            else:
                if publication is not None:
                    await asyncio.to_thread(publication.retract, payload, result)
                reason = str(outcome.reason_code)
                status = failure_status(classify_verification_reason(reason))
                error = f"verification_failed:{reason}"
                logger.info(
                    "job_failed job_id=%s type=%s status=%s error=%s",
                    job_id,
                    job_type,
                    status.value,
                    error,
                )
                if await asyncio.to_thread(self._mark_failed, job_id, error, status, None, token):
                    await self._notify_completion(
                        JobCompletion(
                            job_id=job_id,
                            job_type=job_type,
                            status=status,
                            result=None,
                            error=error,
                            payload=payload,
                        )
                    )
                return

        logger.info("job_completed job_id=%s type=%s", job_id, job_type)
        await self._notify_completion(
            JobCompletion(
                job_id=job_id,
                job_type=job_type,
                status=JobStatus.COMPLETED,
                result=final_result,
                error=None,
                payload=payload,
            )
        )

    async def _publish_and_reprobe(
        self,
        publication: ArtifactPublication,
        verifier: ArtifactVerifier,
        payload: dict[str, object],
        result: dict[str, object],
        job_id: str,
        token: ExecutionToken,
    ) -> tuple[dict[str, object], VerificationOutcome]:
        """Atomic publication + independent re-probe of the published bytes.

        ``publish`` backs up the previous artifact and journals the swap
        (``PUBLICATION_JOURNAL_KEY``); the journal is persisted (fenced) as
        soon as the swap lands so a crash here is recoverable and a refusal
        later can restore the pre-swap bytes.
        """
        try:
            patch = await asyncio.to_thread(publication.publish, payload, result)
        except Exception as exc:  # noqa: BLE001 - publication failure is a refusal
            logger.warning("artifact publish failed; keeping staged temp retracted: %s", exc)
            return result, VerificationOutcome(
                ok=False,
                reason_code="publish_failed",
                summary={"status": "failed", "reason_code": "publish_failed", "detail": str(exc)},
            )
        published = {**result, **patch}
        if PUBLICATION_JOURNAL_KEY in patch:
            await asyncio.to_thread(self._record_publication, job_id, token, published)
        reprobed = await self._verify_safely(verifier, payload, published)
        if not reprobed.ok:
            reason = reprobed.reason_code or "reprobe_failed"
            return published, VerificationOutcome(
                ok=False,
                reason_code="reprobe_failed",
                summary={
                    "status": "failed",
                    "reason_code": "reprobe_failed",
                    "detail": f"published artifact failed re-probe ({reason})",
                },
            )
        return published, reprobed

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
            # Gate-5 repair: durable owner identity of the live execution
            # generation (the ``attempt``+``owner_token`` lease fingerprint).
            # NULL = unreserved/reclaimed; a fenced UPDATE must match it.
            if "owner_token" not in columns:
                connection.execute("ALTER TABLE nexus_job_queue ADD COLUMN owner_token TEXT")

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

    def _reclaim_stale(self) -> list[str]:
        """Startup recovery: reclaim only lease-expired active rows.

        A row is reclaimable when it sits in ``processing``/``verifying``
        with a ``started_at`` older than the lease TTL (its owner is gone by
        the only clock the contract trusts — elapsed lease time) — or with
        no ``started_at`` at all (a pre-lease legacy row: no lease recorded
        is no lease to respect).  Reclaiming
        returns the row to ``pending`` and **invalidates the owner token** so
        every late write of the displaced generation matches zero rows; the
        ``attempt`` generation is preserved (it advances only at
        reservation).  Fresh-lease rows are left alone — takeover without a
        valid expiry is rejected here.
        """
        cutoff = lease_cutoff(now=datetime.now(timezone.utc), ttl_seconds=self._lease_ttl_seconds)
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM nexus_job_queue
                WHERE status IN (?, ?)
                  AND (started_at IS NULL OR started_at < ?)
                ORDER BY created_at, id
                """,
                (JobStatus.PROCESSING.value, JobStatus.VERIFYING.value, cutoff),
            ).fetchall()
            connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = NULL, owner_token = NULL
                WHERE status IN (?, ?)
                  AND (started_at IS NULL OR started_at < ?)
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                    cutoff,
                ),
            )
        return [str(row[0]) for row in rows]

    def _release_owned(self) -> list[str]:
        """Fenced self-release of the generations this instance still holds."""
        released: list[str] = []
        for job_id, token in list(self._live_tokens.items()):
            if self._mark_pending(job_id, token):
                released.append(job_id)
        return released

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

    def _fence_update(
        self,
        job_id: str,
        token: ExecutionToken,
        expected: tuple[JobStatus, ...],
        assignments: str,
        params: tuple[object, ...],
    ) -> bool:
        """The one fenced UPDATE every post-reservation transition goes through.

        The predicate carries the whole lease fingerprint — ``id`` + the
        generation's ``attempt`` + its ``owner_token`` — plus the expected
        source states.  A stale worker's token matches zero rows (Kleppmann
        fence at the storage side): it cannot move a newer execution's
        state, whatever transition it attempts.  ``True`` means the durable
        write applied (COMMIT_CONFIRMED for terminal moves).
        """
        marks = ", ".join("?" for _ in expected)
        sql = (
            f"UPDATE nexus_job_queue SET {assignments} "
            f"WHERE id = ? AND attempt = ? AND owner_token = ? AND status IN ({marks})"
        )
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                sql,
                (
                    *params,
                    job_id,
                    token.generation,
                    token.owner_token,
                    *(s.value for s in expected),
                ),
            )
            return cursor.rowcount > 0

    def _mark_processing(self, job_id: str) -> ExecutionToken | None:
        """Strict CAS ``PENDING → PROCESSING`` — the single claim boundary.

        Never treats ``PROCESSING → PROCESSING`` as a new claim: a live
        execution cannot be re-claimed.  On success the row's ``attempt``
        increments (monotone fence) and a fresh ``owner_token`` is issued —
        the minted :class:`ExecutionToken` is this generation's identity for
        every later fenced write.  ``None`` means another execution owns the
        row (or it moved) and this caller must not run it.
        """
        owner_token = uuid4().hex
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = ?, attempt = attempt + 1, owner_token = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.PROCESSING.value,
                    _now(),
                    owner_token,
                    job_id,
                    JobStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT attempt FROM nexus_job_queue WHERE id = ?", (job_id,)
            ).fetchone()
        token = ExecutionToken(job_id=job_id, generation=int(row[0]), owner_token=owner_token)
        self._live_tokens[job_id] = token
        return token

    def _mark_verifying(self, job_id: str, token: ExecutionToken) -> bool:
        """PROCESSING → VERIFYING (fenced CAS). False if the row moved or is not ours."""
        return self._fence_update(
            job_id,
            token,
            (JobStatus.PROCESSING,),
            "status = ?",
            (JobStatus.VERIFYING.value,),
        )

    def _mark_pending(self, job_id: str, token: ExecutionToken) -> bool:
        """Fenced self-release: ``PROCESSING|VERIFYING → PENDING`` for OUR token.

        Validates the job id, the execution generation (``attempt``), the
        owner token and the expected source states in one predicate — a
        stale worker's cancelled task can never reopen a newer execution's
        row (or a terminal one) to ``pending``.  Releases the owner token on
        success so the generation is definitively over.
        """
        released = self._fence_update(
            job_id,
            token,
            (JobStatus.PROCESSING, JobStatus.VERIFYING),
            "status = ?, started_at = NULL, owner_token = NULL",
            (JobStatus.PENDING.value,),
        )
        if released:
            self._live_tokens.pop(job_id, None)
        return released

    def _mark_completed(
        self, job_id: str, result: dict[str, object], token: ExecutionToken
    ) -> bool:
        """Fenced commit: ``PROCESSING|VERIFYING → COMPLETED`` — the durable
        success point (COMMIT_CONFIRMED only when ``True`` is returned).

        A displaced generation matches zero rows and gets ``False``: no
        success payload is stored and no success notification may fire for
        an outcome that did not commit (F3).
        """
        committed = self._fence_update(
            job_id,
            token,
            (JobStatus.PROCESSING, JobStatus.VERIFYING),
            "status = ?, result_json = ?, error = NULL, finished_at = ?",
            (
                JobStatus.COMPLETED.value,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )
        if committed:
            self._live_tokens.pop(job_id, None)
        return committed

    def _mark_failed(
        self,
        job_id: str,
        error: str,
        status: JobStatus = JobStatus.FAILED_TERMINAL,
        result: dict[str, object] | None = None,
        token: ExecutionToken | None = None,
    ) -> bool:
        """Persist a classified failure. ``True`` only when the write applied.

        Two fenced modes:

        * ``token=None`` — claim-time structural failure (no handler for the
          type, unusable payload): only a still-``PENDING`` row may take it
          (no execution exists to fence against, and a reserved row is never
          structurally failed by this path).
        * ``token`` set — post-reservation failure: the lease fingerprint and
          the expected source states (``processing``/``verifying``) are
          validated; a stale worker matches zero rows and cannot mark a
          newer attempt failed (T2/T3).

        ``result`` is stored only for typed user failures (the durable
        ``{"success": False, "error_code": ...}`` dialect the notifier
        translates); execution crashes and verification refusals persist no
        result payload — the ``error`` text is the failure reason.
        """
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
        )
        if token is None:
            with self._db_lock, self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE nexus_job_queue
                    SET status = ?, error = ?, result_json = ?, finished_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (status.value, error, result_json, _now(), job_id, JobStatus.PENDING.value),
                )
                applied = cursor.rowcount > 0
            if applied:
                self._live_tokens.pop(job_id, None)
            return applied
        applied = self._fence_update(
            job_id,
            token,
            (JobStatus.PROCESSING, JobStatus.VERIFYING),
            "status = ?, error = ?, result_json = ?, finished_at = ?",
            (status.value, error, result_json, _now()),
        )
        if applied:
            self._live_tokens.pop(job_id, None)
        return applied

    def _record_publication(
        self, job_id: str, token: ExecutionToken, result: dict[str, object]
    ) -> bool:
        """Journal a publication swap (fenced) before the re-probe runs.

        The ``_publication`` block (backup path + published inode) is written
        to ``result_json`` as durable crash-recovery state: a process death
        before the fenced commit leaves the journal visible to the next
        execution, which restores the pre-swap bytes.  Writing it is itself
        fenced — a stale publisher cannot journal over a newer execution.
        """
        return self._fence_update(
            job_id,
            token,
            (JobStatus.PROCESSING, JobStatus.VERIFYING),
            "result_json = ?",
            (json.dumps(result, ensure_ascii=False, sort_keys=True),),
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
