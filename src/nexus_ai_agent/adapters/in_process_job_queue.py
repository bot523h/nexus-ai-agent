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
blind write) — and, after the reservation, **fenced**: the reservation is a
PENDING-only CAS that increments ``attempt`` and hands the new value back as
the execution's fencing token (``jobs.lifecycle.ExecutionClaim``); every
later worker-owned transition carries ``AND attempt = ?`` and returns
whether it committed.  A stale execution (cancelled, reclaimed by a takeover,
or a duplicate claim from a second process over the same sidecar) can no
longer complete, fail, reopen, publish or notify anything: its CAS returns
``False`` and the queue stops (``job_transition_rejected``).  Takeover is
explicit — ``resume_pending`` (startup recovery, optionally expiry-gated)
resets orphaned rows to ``pending``; there is no ``processing → processing``
re-claim.

For job types with a registered artifact verifier, a handler
result is NEVER trusted on its own: the queue moves the row to ``verifying``
and re-measures the claimed artifact independently (exists, size > 0,
sha256 recompute, expected-path containment, probe evidence for media).
Execution success + verification success = success eligibility.  For lanes
whose artifact destination lives outside the job workspace (``pdf_extract``)
the order is stage → verify → *ownership re-check* → **publish atomically
(previous artifact kept as a recoverable backup)** → **re-probe the published
bytes** → persist success (fenced CAS) → drop the backup (registered
:class:`ArtifactPublication`).  A refusal before publication retracts only
the staged temp; a refusal *after* publication (failed re-probe) restores the
previous published artifact byte-for-byte — or removes the refused bytes when
there was none — so a refused publication never leaves a worse world behind.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import structlog

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.failure_semantics import (
    FailureClass,
    classify_exception,
    classify_typed_code,
    classify_verification_reason,
    failure_code_of,
    failure_status,
    typed_failure_error,
)
from nexus_ai_agent.jobs.lifecycle import ExecutionClaim, parse_job_status
from nexus_ai_agent.jobs.verification import VerificationOutcome

JobHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
JobCompletionHook = Callable[["JobCompletion"], Awaitable[None]]

#: A verifier re-measures a handler result against the filesystem. It must
#: be sync (the queue runs it in a worker thread) and side-effect free.
ArtifactVerifier = Callable[[dict[str, object], dict[str, object]], VerificationOutcome]


@dataclass(frozen=True)
class ArtifactPublication:
    """Queue-owned publication step for lanes with an external destination.

    Two-phase, recoverable publication (Gate 5 final repair):

    ``publish(payload, result)``
        atomically moves the verified staged artifact to its final name and
        returns the result patch (``artifact_path`` → published).  It must
        keep the *previous* published artifact recoverable (the built-in
        pdf lane hard-links it to a ``.prev`` sibling before the rename).
    ``retract(payload, result)``
        undoes this attempt after a refusal.  Called with the pre-publish
        result it removes only the staged temp; called with the
        post-publish result (``artifact_path`` already the published name —
        i.e. the re-probe refused) it restores the previous artifact from
        the backup, or removes the refused bytes when there was none.  It
        never touches an artifact it cannot prove to be this attempt's.
    ``finalize(payload, result)`` (optional)
        runs only after the durable ``COMPLETED`` commit was confirmed and
        drops the backup.  A failure here is logged, never fatal: the
        published bytes are already the verified ones and a stray backup is
        cleaned by the next publication.

    All three run in a worker thread and must be idempotent (recovery may
    re-run them).  The queue calls ``publish`` only for the *current*
    execution (ownership re-checked right before it) — see
    ``_publish_and_reprobe``.
    """

    publish: Callable[[dict[str, object], dict[str, object]], dict[str, object]]
    retract: Callable[[dict[str, object], dict[str, object]], None]
    finalize: Callable[[dict[str, object], dict[str, object]], None] | None = None


def default_artifact_publications() -> dict[str, ArtifactPublication]:
    """Built-in publications keyed by job type (task-181).

    Only ``pdf_extract`` — the single lane whose artifact destination
    (``<stem>.extracted.txt`` sidecar) lives *outside* the job workspace.
    Workspace-scoped lanes (creative_render / slideshow_render / story) are
    published at delivery, after verification, by the completion notifier.
    """
    from nexus_ai_agent.jobs.feature_verification import (
        finalize_pdf_text_artifact,
        publish_pdf_text_artifact,
        retract_pdf_text_artifact,
    )

    return {
        "pdf_extract": ArtifactPublication(
            publish=publish_pdf_text_artifact,
            retract=retract_pdf_text_artifact,
            finalize=finalize_pdf_text_artifact,
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

    async def resume_pending(self, *, stale_after: timedelta | None = None) -> list[str]:
        """Requeue jobs left unfinished by a previous process (explicit takeover).

        This is the **only** way ownership of an in-flight row changes hands:
        ``processing``/``verifying`` rows are reset to ``pending`` (attempt
        untouched) and re-scheduled; the next reservation mints a strictly
        higher fencing token, so whatever the previous owner still does is
        rejected by every fenced CAS.

        ``stale_after=None`` (process-startup recovery — the bot's
        ``post_init``/webhook startup) takes over every orphaned row: the
        composition roots guarantee one queue-owning process per sidecar, so
        at startup the previous owner is dead by construction.  With
        ``stale_after`` set, only in-flight rows whose ``started_at`` is
        older than the window are taken over (expiry-gated takeover — the
        safe form when a live peer might still own recent rows); ``pending``
        rows are always re-scheduled.  Rows this very process is executing
        are never taken over (that would strand its own live execution).
        Terminal rows are never touched.
        """
        live = {job_id for job_id, task in self._tasks.items() if not task.done()}
        job_ids = await asyncio.to_thread(self._reset_unfinished, stale_after, live)
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
        await asyncio.to_thread(self._reset_unfinished, None, set())

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
        # the code changes ⇒ TERMINAL.  Unowned CAS (PENDING only): if
        # another process reserved the row meanwhile, nothing is announced.
        handler = self._handlers.get(job_type)
        if handler is None:
            error = f"no handler registered for job type {row['job_type']}"
            status = failure_status(FailureClass.TERMINAL)
            if not await asyncio.to_thread(self._fail_unclaimed, job_id, error, status):
                self._log_rejected(job_id, None, status)
                return
            logger.info(
                "job_failed job_id=%s type=%s status=%s error=%r",
                job_id,
                job_type,
                status.value,
                error,
            )
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

        # Reservation: the RUNNING boundary and the ownership boundary.  A
        # PENDING-only CAS mints this execution's fencing token; a row that
        # is already PROCESSING/VERIFYING belongs to someone else (another
        # process over the same sidecar, or a live task) and is NOT ours.
        claim = await asyncio.to_thread(self._mark_processing, job_id)
        if claim is None:
            logger.info("job_reservation_rejected job_id=%s type=%s", job_id, job_type)
            return
        logger.info("job_processing job_id=%s type=%s attempt=%d", job_id, job_type, claim.attempt)
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
            # failure. Keep the row recoverable for the next process — but
            # only OUR row: a newer owner's execution (or a terminal row)
            # must never be reopened by a cancelled predecessor.
            if not await asyncio.to_thread(self._mark_pending, claim):
                self._log_rejected(job_id, claim, JobStatus.PENDING)
            raise
        except Exception as exc:  # noqa: BLE001 - job failure must be persisted
            # Fail-closed conversion: classify and persist a failure status.
            status = failure_status(classify_exception(exc))
            await self._fail_owned(claim, job_type, payload, str(exc), status, None)
            return

        # GAP-A (task-181) + R4: a handler result that says success=False is
        # a FAILURE of the job — never COMPLETED, whatever any verifier would
        # answer about it.  The typed dialect keeps its code; a bare
        # ``{"success": False}`` is recorded as ``unspecified`` (TERMINAL).
        # The result payload is preserved so the notifier can translate it
        # and the audit chain can read it.
        code = failure_code_of(result)
        if code is not None:
            status = failure_status(classify_typed_code(code))
            await self._fail_owned(
                claim, job_type, payload, typed_failure_error(code), status, result
            )
            return

        verifier = self._artifact_verifiers.get(job_type)
        publication = self._artifact_publications.get(job_type)
        if verifier is None:
            # No verification contract for this job type: historical
            # semantics — the handler result is the completed result.
            final_result = result
            if not await asyncio.to_thread(self._mark_completed, claim, final_result):
                self._log_rejected(job_id, claim, JobStatus.COMPLETED)
                return
        else:
            # Execution success ≠ job success: verify the artifact
            # independently before the row may become terminal-success.
            if not await asyncio.to_thread(self._mark_verifying, claim):
                self._log_rejected(job_id, claim, JobStatus.VERIFYING)
                return  # row was reclaimed/reset elsewhere; not ours anymore
            logger.info("job_verifying job_id=%s type=%s", job_id, job_type)
            outcome = await self._verify_safely(verifier, payload, result)
            published = False
            if outcome.ok and publication is not None:
                # Stage → verify → **publish atomically** → **re-probe** →
                # persist success (§4 order, task-181).  Publication is the
                # irreversible side effect: it runs only for the current
                # owner (fence re-checked right before it) and keeps the
                # previous artifact recoverable.
                result, outcome, published = await self._publish_and_reprobe(
                    claim, publication, verifier, payload, result
                )
            if outcome.ok:
                final_result = {**result, VERIFICATION_RESULT_KEY: outcome.block}
                if not await asyncio.to_thread(self._mark_completed, claim, final_result):
                    # Durable commit refused: we are no longer the owner.  No
                    # notification, no filesystem action — the current owner
                    # decides the fate of the destination.
                    self._log_rejected(job_id, claim, JobStatus.COMPLETED)
                    return
                if publication is not None and publication.finalize is not None and published:
                    await self._finalize_safely(publication, payload, result)
            else:
                if publication is not None and outcome.reason_code != "stale_execution":
                    # A refusal retracts this attempt (staged temp before
                    # publication; restore of the previous artifact after
                    # a refused re-probe).  A fenced-off execution retracts
                    # nothing: the destination is not its to touch.
                    await asyncio.to_thread(publication.retract, payload, result)
                reason = str(outcome.reason_code)
                status = failure_status(classify_verification_reason(reason))
                await self._fail_owned(
                    claim, job_type, payload, f"verification_failed:{reason}", status, None
                )
                return

        logger.info("job_completed job_id=%s type=%s attempt=%d", job_id, job_type, claim.attempt)
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

    async def _fail_owned(
        self,
        claim: ExecutionClaim,
        job_type: str,
        payload: dict[str, object],
        error: str,
        status: JobStatus,
        result: dict[str, object] | None,
    ) -> None:
        """Persist a classified failure for OUR execution, then announce it.

        The fenced CAS is the durable truth: if it is rejected (the row was
        taken over or is already terminal), nothing is announced — the
        current owner reports its own outcome.
        """
        if not await asyncio.to_thread(self._mark_failed, claim, error, status, result):
            self._log_rejected(claim.job_id, claim, status)
            return
        logger.info(
            "job_failed job_id=%s type=%s status=%s error=%s attempt=%d",
            claim.job_id,
            job_type,
            status.value,
            error,
            claim.attempt,
        )
        await self._notify_completion(
            JobCompletion(
                job_id=claim.job_id,
                job_type=job_type,
                status=status,
                result=result,
                error=error,
                payload=payload,
            )
        )

    @staticmethod
    def _log_rejected(job_id: str, claim: ExecutionClaim | None, target: JobStatus) -> None:
        logger.warning(
            "job_transition_rejected job_id=%s attempt=%s target=%s "
            "(stale execution: row reclaimed, superseded or terminal — no side effect)",
            job_id,
            claim.attempt if claim is not None else "unclaimed",
            target.value,
        )

    async def _publish_and_reprobe(
        self,
        claim: ExecutionClaim,
        publication: ArtifactPublication,
        verifier: ArtifactVerifier,
        payload: dict[str, object],
        result: dict[str, object],
    ) -> tuple[dict[str, object], VerificationOutcome, bool]:
        """Atomic publication + independent re-probe of the published bytes.

        Returns ``(result, outcome, published)``.  Side-effect fencing: the
        ownership of the row is re-read immediately before ``publish`` (the
        last check before the irreversible filesystem step); a stale
        execution gets ``stale_execution`` and publishes nothing.  The
        residual window between this check and the rename is the classic
        fencing limit of a plain filesystem (no token-aware storage); it is
        documented in JOB_LIFECYCLE.md §4 and bounded by the takeover rule
        (only startup recovery / expiry-gated reclaim can move a live row).
        """
        if not await asyncio.to_thread(self._owns_execution, claim, JobStatus.VERIFYING):
            return (
                result,
                VerificationOutcome(
                    ok=False,
                    reason_code="stale_execution",
                    summary={
                        "status": "failed",
                        "reason_code": "stale_execution",
                        "detail": "execution superseded before publication; nothing published",
                    },
                ),
                False,
            )
        try:
            patch = await asyncio.to_thread(publication.publish, payload, result)
        except Exception as exc:  # noqa: BLE001 - publication failure is a refusal
            logger.warning("artifact publish failed; keeping staged temp retracted: %s", exc)
            return (
                result,
                VerificationOutcome(
                    ok=False,
                    reason_code="publish_failed",
                    summary={
                        "status": "failed",
                        "reason_code": "publish_failed",
                        "detail": str(exc),
                    },
                ),
                False,
            )
        published = {**result, **patch}
        reprobed = await self._verify_safely(verifier, payload, published)
        if not reprobed.ok:
            reason = reprobed.reason_code or "reprobe_failed"
            return (
                published,
                VerificationOutcome(
                    ok=False,
                    reason_code="reprobe_failed",
                    summary={
                        "status": "failed",
                        "reason_code": "reprobe_failed",
                        "detail": f"published artifact failed re-probe ({reason})",
                    },
                ),
                True,
            )
        return published, reprobed, True

    async def _finalize_safely(
        self,
        publication: ArtifactPublication,
        payload: dict[str, object],
        result: dict[str, object],
    ) -> None:
        """Drop the publication backup after the durable commit (never fatal)."""
        assert publication.finalize is not None
        try:
            await asyncio.to_thread(publication.finalize, payload, result)
        except Exception:  # noqa: BLE001 - the job is already durably COMPLETED
            logger.warning("artifact publication finalize failed (backup left)", exc_info=True)

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

    def _reset_unfinished(self, stale_after: timedelta | None, exclude: set[str]) -> list[str]:
        """Takeover: orphaned in-flight rows → pending; pending rows re-listed.

        ``attempt`` is deliberately left as is — the next reservation
        increments it, which is what fences the previous owner out.
        """
        in_flight = (JobStatus.PROCESSING.value, JobStatus.VERIFYING.value)
        cutoff = (
            (datetime.now(timezone.utc) - stale_after).isoformat()
            if stale_after is not None
            else None
        )
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, status, started_at FROM nexus_job_queue
                WHERE status IN (?, ?, ?)
                ORDER BY created_at, id
                """,
                (JobStatus.PENDING.value, *in_flight),
            ).fetchall()
            selected: list[str] = []
            for row in rows:
                job_id = str(row["id"])
                if job_id in exclude:
                    continue
                if str(row["status"]) in in_flight:
                    started_at = row["started_at"]
                    if cutoff is not None and started_at is not None and started_at >= cutoff:
                        continue  # still within its window: a live peer may own it
                    connection.execute(
                        """
                        UPDATE nexus_job_queue
                        SET status = ?, started_at = NULL
                        WHERE id = ? AND status IN (?, ?)
                        """,
                        (JobStatus.PENDING.value, job_id, *in_flight),
                    )
                selected.append(job_id)
        return selected

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

    # ------------------------------------------------------------------
    # Fenced transitions.  Every method below is a compare-and-set on
    # (id, expected status[, attempt]) and returns whether it committed.
    # ``rowcount`` is the proof: 1 = this execution still owned the row.
    # ------------------------------------------------------------------
    def _mark_processing(self, job_id: str) -> ExecutionClaim | None:
        """Reservation: PENDING → PROCESSING, minting the fencing token.

        Source state is PENDING **only** — a PROCESSING/VERIFYING row is
        owned by a live execution and is never re-reserved (takeover goes
        through ``resume_pending`` explicitly).  Returns the claim (job id +
        the attempt value this reservation minted) or ``None`` if the CAS
        did not commit.
        """
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = ?, attempt = attempt + 1
                WHERE id = ? AND status = ?
                """,
                (JobStatus.PROCESSING.value, _now(), job_id, JobStatus.PENDING.value),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT attempt FROM nexus_job_queue WHERE id = ?", (job_id,)
            ).fetchone()
            return ExecutionClaim(job_id=job_id, attempt=int(row[0]))

    def _owns_execution(self, claim: ExecutionClaim, expected: JobStatus) -> bool:
        """Read-side fence: is the row still ours, in ``expected`` state?"""
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                "SELECT status, attempt FROM nexus_job_queue WHERE id = ?", (claim.job_id,)
            ).fetchone()
        return (
            row is not None
            and str(row["status"]) == expected.value
            and int(row["attempt"]) == claim.attempt
        )

    def _mark_verifying(self, claim: ExecutionClaim) -> bool:
        """PROCESSING → VERIFYING (fenced CAS). False if not ours any more."""
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?
                WHERE id = ? AND status = ? AND attempt = ?
                """,
                (
                    JobStatus.VERIFYING.value,
                    claim.job_id,
                    JobStatus.PROCESSING.value,
                    claim.attempt,
                ),
            )
            return cursor.rowcount == 1

    def _mark_pending(self, claim: ExecutionClaim) -> bool:
        """PROCESSING/VERIFYING → PENDING for OUR execution only (fenced CAS).

        A cancelled execution may reopen nothing but its own live row: a
        newer owner's row (different attempt) and any terminal row are left
        untouched.
        """
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, started_at = NULL
                WHERE id = ? AND status IN (?, ?) AND attempt = ?
                """,
                (
                    JobStatus.PENDING.value,
                    claim.job_id,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                    claim.attempt,
                ),
            )
            return cursor.rowcount == 1

    def _mark_completed(self, claim: ExecutionClaim, result: dict[str, object]) -> bool:
        """PROCESSING/VERIFYING → COMPLETED (fenced CAS) — the durable commit.

        ``True`` is the only licence to announce success.
        """
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, result_json = ?, error = NULL, finished_at = ?
                WHERE id = ? AND status IN (?, ?) AND attempt = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    _now(),
                    claim.job_id,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                    claim.attempt,
                ),
            )
            return cursor.rowcount == 1

    def _mark_failed(
        self,
        claim: ExecutionClaim,
        error: str,
        status: JobStatus = JobStatus.FAILED_TERMINAL,
        result: dict[str, object] | None = None,
    ) -> bool:
        """PROCESSING/VERIFYING → FAILED_* for OUR execution (fenced CAS).

        ``result`` is stored only for typed user failures (the durable
        ``{"success": False, "error_code": ...}`` dialect the notifier
        translates); execution crashes and verification refusals persist no
        result payload — the ``error`` text is the failure reason.
        """
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
        )
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, error = ?, result_json = ?, finished_at = ?
                WHERE id = ? AND status IN (?, ?) AND attempt = ?
                """,
                (
                    status.value,
                    error,
                    result_json,
                    _now(),
                    claim.job_id,
                    JobStatus.PROCESSING.value,
                    JobStatus.VERIFYING.value,
                    claim.attempt,
                ),
            )
            return cursor.rowcount == 1

    def _fail_unclaimed(self, job_id: str, error: str, status: JobStatus) -> bool:
        """PENDING → FAILED_* (claim-time structural failure; unowned CAS).

        No token exists before a reservation; the CAS on ``status =
        pending`` is what proves nobody else reserved the row meanwhile.
        """
        with self._db_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?, error = ?, result_json = NULL, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (status.value, error, _now(), job_id, JobStatus.PENDING.value),
            )
            return cursor.rowcount == 1

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
