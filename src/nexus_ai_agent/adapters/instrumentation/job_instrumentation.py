"""Instrumented job queue — M0 runtime integration.

Subclass of :class:`InProcessJobQueue` (the queue adapter is fenced by the
Research-V2 claim-lease PR, so instrumentation happens here, at the two
composition roots: ``bot/app.py`` and ``cli.py`` ``jobs resume``).

What this adds, per event:

* **created** — ``correlation_id`` injected into the payload before the row is
  persisted (payload > ambient contextvar > fresh id), then
  ``jobs_created_total`` + ``job_created``. An idempotent hit is NOT counted.
* **claimed** — on the real ``pending -> processing`` transition only:
  ``jobs_claimed_total`` + ``job_claimed`` + a monotonic claim timer.
* **completed / failed** — after the durable state flip:
  ``jobs_completed_total`` / ``jobs_failed_total`` (bounded ``error_code``) +
  structured events + a ``job_duration_seconds`` histogram observation.
* **recovered** — at the two public resume entry points with entry-point
  reasons (``startup_recovery`` / ``operator_resume``); graceful shutdown
  drains are deliberately NOT counted (they release work, they do not
  recover it).

Correlation chain: the payload's ``correlation_id`` is restored into the
contextvar inside the wrapped handler for the duration of the call and the
previous value is restored afterwards. Terminal events read the id from the
persisted payload (handlers may clear the context before the terminal mark
runs), never from ambient state.

Saturation: gauges ``queue_depth`` / ``jobs_inflight`` are ``set()`` from a
``SELECT COUNT(*)`` after every state transition (source-of-truth sampling,
not inc/dec tracking), and :meth:`saturation_report` exposes depth, inflight,
oldest pending age and seconds since last completion against an **injected**
``now`` (hostile-clock safe: negative ages clamp to 0).

Every emission is fail-safe: observability must never break the queue. A
swallowed emission shows up as a missing metric in the Q-tests, never as a
failed job.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobCompletionHook,
    JobHandler,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.observability.correlation import (
    extract_from_payload,
    get_correlation_id,
    inject_into_payload,
    new_correlation_id,
)
from nexus_ai_agent.observability.diagnostics import (
    JobFailureContext,
    classify_failure_error,
    log_job_claimed,
    log_job_completed,
    log_job_created,
    log_job_failure,
    log_job_recovered,
)
from nexus_ai_agent.observability.metrics import (
    observe_job_duration,
    set_jobs_inflight,
    set_queue_depth,
)

logger = logging.getLogger(__name__)

__all__ = ["InstrumentedJobQueue", "SaturationReport", "classify_failure_error"]


@dataclass(frozen=True)
class SaturationReport:
    """Queue saturation sampled from SQLite at an injected point in time."""

    pending: int
    processing: int
    completed: int
    failed: int
    oldest_pending_age_seconds: float
    seconds_since_last_completion: float
    measured_at: str

    @property
    def depth(self) -> int:
        return self.pending

    @property
    def inflight(self) -> int:
        return self.processing


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a queue timestamp; naive values are treated as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(now: datetime, stamp: datetime | None) -> float:
    """Non-negative age of ``stamp`` relative to ``now`` (hostile-clock clamp)."""
    if stamp is None:
        return 0.0
    return max(0.0, (now - stamp).total_seconds())


class InstrumentedJobQueue(InProcessJobQueue):
    """``InProcessJobQueue`` with M0 metrics, correlation and diagnostics."""

    def __init__(
        self,
        db_path: Any,
        handlers: dict[str, JobHandler] | None = None,
        *,
        on_job_finished: JobCompletionHook | None = None,
    ) -> None:
        super().__init__(db_path, handlers, on_job_finished=on_job_finished)
        # Monotonic claim timestamps, keyed by job_id (immune to wall-clock jumps).
        self._claim_started: dict[str, float] = {}
        # Wrap constructor-provided handlers so consume-time correlation restore
        # applies no matter how the handler was registered.
        self._handlers = {
            job_type: self._wrap_handler(handler) for job_type, handler in self._handlers.items()
        }

    # -- fail-safe emission -------------------------------------------------

    @staticmethod
    def _emit(fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        """Run one observability emission; never let it break the runtime."""
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - observability is best-effort by contract
            logger.debug("m0 emission failed", exc_info=True)

    # -- handler wrapping: correlation restore on consume -------------------

    def _wrap_handler(self, handler: JobHandler) -> JobHandler:
        if getattr(handler, "_nexus_m0_wrapped", False):
            return handler

        async def wrapped(payload: dict[str, object]) -> dict[str, object]:
            ambient = get_correlation_id()
            from_payload = (
                extract_from_payload(payload) if isinstance(payload, dict) else None
            )
            bound = from_payload or ambient
            if bound:
                from nexus_ai_agent.observability.correlation import bind_correlation_id

                self._emit(bind_correlation_id, bound)
            try:
                return await handler(payload)
            finally:
                from nexus_ai_agent.observability.correlation import (
                    bind_correlation_id,
                    clear_correlation_id,
                )

                if ambient:
                    self._emit(bind_correlation_id, ambient)
                else:
                    self._emit(clear_correlation_id)

        wrapped._nexus_m0_wrapped = True  # type: ignore[attr-defined]
        return wrapped

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        super().register_handler(job_type, self._wrap_handler(handler))

    # -- created: correlation injection + counter + event -------------------

    def _key_exists(self, idempotency_key: str) -> bool:
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM nexus_job_queue WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    def _insert_or_get(
        self,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> str:
        # Inject correlation_id before the row is persisted. Priority:
        # payload (stable across idempotent re-enqueue) > ambient contextvar
        # (the Telegram update that enqueued this job) > fresh random id.
        # NOTE (documented M0 limitation): the existence check races a
        # concurrent same-key enqueue inside one process — the UNIQUE key still
        # prevents a second row; only the counter could over-count by one in
        # that pathological window.
        correlation_id: str | None = None
        try:
            payload = dict(payload)
            correlation_id = (
                extract_from_payload(payload) or get_correlation_id() or new_correlation_id()
            )
            payload = inject_into_payload(payload, correlation_id)
        except Exception:  # noqa: BLE001 - degrade the chain, never the queue
            correlation_id = None
        existed = self._key_exists(idempotency_key)
        job_id = super()._insert_or_get(job_type, idempotency_key, payload)
        if not existed:
            self._emit(log_job_created, job_id, job_type, correlation_id)
            self._emit(self._refresh_saturation_gauges)
        return job_id

    # -- claim: transition guard + counter + event + monotonic timer --------

    def _status_of(self, job_id: str) -> str | None:
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM nexus_job_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def _identity_of(self, job_id: str) -> tuple[str, str | None]:
        """Return ``(job_type, correlation_id from persisted payload)``."""
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                "SELECT job_type, payload_json FROM nexus_job_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return "unknown", None
        correlation_id: str | None = None
        try:
            payload = json.loads(str(row[1])) if row[1] else {}
            if isinstance(payload, dict):
                correlation_id = extract_from_payload(payload)
        except Exception:  # noqa: BLE001 - a broken payload must not break metrics
            correlation_id = None
        return str(row[0]), correlation_id

    def _mark_processing(self, job_id: str) -> None:
        prior = self._status_of(job_id)
        super()._mark_processing(job_id)
        if prior == JobStatus.PENDING.value:
            job_type, correlation_id = self._identity_of(job_id)
            self._claim_started[job_id] = time.monotonic()
            # Single emission point: log_job_claimed increments
            # jobs_claimed_total internally (mirrors log_job_failure).
            self._emit(log_job_claimed, job_id, job_type, correlation_id)
        elif prior == JobStatus.PROCESSING.value and job_id not in self._claim_started:
            # Re-mark of a row whose claim we did not witness (defensive).
            self._claim_started[job_id] = time.monotonic()
        self._emit(self._refresh_saturation_gauges)

    def _mark_pending(self, job_id: str) -> None:
        super()._mark_pending(job_id)
        # Cancellation returned the row to pending: inflight/depth shifted.
        self._claim_started.pop(job_id, None)
        self._emit(self._refresh_saturation_gauges)

    # -- terminal states ----------------------------------------------------

    def _elapsed_seconds(self, job_id: str) -> float:
        started = self._claim_started.pop(job_id, None)
        if started is None:
            return 0.0
        return max(0.0, time.monotonic() - started)

    def _mark_completed(self, job_id: str, result: dict[str, object]) -> None:
        super()._mark_completed(job_id, result)
        elapsed = self._elapsed_seconds(job_id)
        job_type, correlation_id = self._identity_of(job_id)
        self._emit(log_job_completed, job_id, job_type, elapsed * 1000.0, correlation_id)
        self._emit(observe_job_duration, job_type, elapsed)
        self._emit(self._refresh_saturation_gauges)

    def _mark_failed(self, job_id: str, error: str) -> None:
        super()._mark_failed(job_id, error)
        elapsed = self._elapsed_seconds(job_id)
        job_type, correlation_id = self._identity_of(job_id)
        failure_class = classify_failure_error(error)
        # inc_jobs_failed runs inside log_job_failure (single emission point).
        self._emit(
            log_job_failure,
            JobFailureContext(
                job_id=job_id,
                job_type=job_type,
                correlation_id=correlation_id,
                failure_class=failure_class,
                failure_message=error[:200],
                duration_ms=elapsed * 1000.0,
                retry_state="terminal",
                attempt=1,
            ),
        )
        self._emit(observe_job_duration, job_type, elapsed)
        self._emit(self._refresh_saturation_gauges)

    # -- recovery at the public resume entry points -------------------------

    def _emit_recovered(self, job_ids: list[str], reason: str) -> None:
        # Single emission point per row: log_job_recovered increments
        # jobs_recovered_total internally — do not also call inc_jobs_*.
        for job_id in job_ids:
            _, correlation_id = self._identity_of(job_id)
            self._emit(log_job_recovered, job_id, reason, correlation_id)
        self._emit(self._refresh_saturation_gauges)

    async def resume_pending(self) -> list[str]:
        """Process-startup recovery (bot post-init, webhook serve)."""
        job_ids = await super().resume_pending()
        self._emit_recovered(job_ids, reason="startup_recovery")
        return job_ids

    async def resume_pending_jobs(self) -> list[str]:
        """Operator recovery (``nexus jobs resume``)."""
        job_ids = await super().resume_pending_jobs()
        self._emit_recovered(job_ids, reason="operator_resume")
        return job_ids

    # -- saturation: gauges + report ----------------------------------------

    def _status_counts(self) -> dict[str, int]:
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM nexus_job_queue GROUP BY status"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def _refresh_saturation_gauges(self) -> None:
        try:
            counts = self._status_counts()
            set_queue_depth(counts.get(JobStatus.PENDING.value, 0))
            set_jobs_inflight(counts.get(JobStatus.PROCESSING.value, 0))
        except Exception:  # noqa: BLE001 - gauges are best-effort
            logger.debug("saturation gauge refresh failed", exc_info=True)

    def saturation_report(self, *, now: datetime | None = None) -> SaturationReport:
        """Sample saturation from the real queue at an injected ``now``."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        with self._db_lock, self._connection() as connection:
            counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM nexus_job_queue GROUP BY status"
                ).fetchall()
            }
            oldest_pending = connection.execute(
                "SELECT MIN(created_at) FROM nexus_job_queue WHERE status = ?",
                (JobStatus.PENDING.value,),
            ).fetchone()
            last_completed = connection.execute(
                "SELECT MAX(finished_at) FROM nexus_job_queue WHERE status = ?",
                (JobStatus.COMPLETED.value,),
            ).fetchone()
            oldest_unfinished = connection.execute(
                "SELECT MIN(created_at) FROM nexus_job_queue WHERE status IN (?, ?)",
                (JobStatus.PENDING.value, JobStatus.PROCESSING.value),
            ).fetchone()

        pending = counts.get(JobStatus.PENDING.value, 0)
        processing = counts.get(JobStatus.PROCESSING.value, 0)
        completed = counts.get(JobStatus.COMPLETED.value, 0)
        failed = counts.get(JobStatus.FAILED.value, 0)

        oldest_stamp = _parse_iso(str(oldest_pending[0])) if oldest_pending else None
        oldest_age = _age_seconds(now, oldest_stamp)

        last_stamp = _parse_iso(str(last_completed[0])) if last_completed else None
        if last_stamp is None and (pending or processing):
            # No completion ever while work waits: the oldest unfinished row
            # proves how long there has been no completion.
            last_stamp = _parse_iso(str(oldest_unfinished[0])) if oldest_unfinished else None
        seconds_since = _age_seconds(now, last_stamp) if last_stamp else 0.0

        return SaturationReport(
            pending=pending,
            processing=processing,
            completed=completed,
            failed=failed,
            oldest_pending_age_seconds=oldest_age,
            seconds_since_last_completion=seconds_since,
            measured_at=now.isoformat(),
        )
