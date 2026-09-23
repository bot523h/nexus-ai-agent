"""Diagnostic visibility for job failures — M0.

Audit finding:
  - metrics snapshot maybe cannot observe real bot
  - job failures may lack notable logs

This module provides structured failure logging with:
  - event name
  - job/task identity non-sensitive (job_type, job_id prefix)
  - correlation id
  - failure class/type
  - duration
  - retry/terminal state

But never:
  - secret
  - raw user text
  - token
  - API key
  - sensitive URL

Design: uses existing log_lifecycle_event and redaction, plus metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from nexus_ai_agent.infrastructure.observability.structured import (
    log_lifecycle_event,
)
from nexus_ai_agent.observability.correlation import get_correlation_id
from nexus_ai_agent.observability.metrics import inc_jobs_failed

log = logging.getLogger("nexus_ai_agent.jobs.diagnostics")

# Event names — canonical, low-cardinality
EVENT_JOB_FAILED: str = "job_failed"
EVENT_JOB_COMPLETED: str = "job_completed"
EVENT_JOB_RECOVERED: str = "job_recovered"
EVENT_JOB_CLAIMED: str = "job_claimed"
EVENT_JOB_CREATED: str = "job_created"


@dataclass(frozen=True)
class JobFailureContext:
    """Non-sensitive failure context for logging."""

    job_id: str  # full id, but logged truncated (random hex, safe)
    job_type: str
    correlation_id: str | None
    failure_class: str
    failure_message: str  # truncated, redacted
    duration_ms: float
    retry_state: str  # "terminal" or "retryable" or "recovered"
    attempt: int = 1


def _truncate_job_id(job_id: str) -> str:
    """Truncate job_id to 8 chars for logs — enough for correlation."""
    return job_id[:8] if len(job_id) > 8 else job_id


def _safe_failure_message(exc: BaseException | str) -> str:
    """Return safe failure message: type + truncated, no raw payload."""
    if isinstance(exc, str):
        return exc[:200]
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def log_job_failure(ctx: JobFailureContext) -> None:
    """Log a job failure with structured fields, redacted, and metrics."""
    # Metrics — low-cardinality only
    try:
        inc_jobs_failed(ctx.job_type, error_code=ctx.failure_class.lower())
    except Exception:
        pass

    # Structured event — never raises
    try:
        log_lifecycle_event(
            logging.ERROR,
            EVENT_JOB_FAILED,
            job_id=_truncate_job_id(ctx.job_id),
            job_type=ctx.job_type,
            correlation_id=ctx.correlation_id or "none",
            failure_class=ctx.failure_class,
            failure_message=ctx.failure_message[:200],
            duration_ms=round(ctx.duration_ms, 1),
            retry_state=ctx.retry_state,
            attempt=ctx.attempt,
        )
    except Exception:
        pass

    # Also standard logger with correlation_id
    try:
        cid = ctx.correlation_id or get_correlation_id() or "none"
        log.error(
            "job_failed job_id=%s job_type=%s correlation_id=%s "
            "failure_class=%s duration_ms=%.1f retry_state=%s",
            _truncate_job_id(ctx.job_id),
            ctx.job_type,
            cid,
            ctx.failure_class,
            ctx.duration_ms,
            ctx.retry_state,
        )
    except Exception:
        pass


def log_job_completed(
    job_id: str,
    job_type: str,
    duration_ms: float,
    correlation_id: str | None = None,
) -> None:
    try:
        from nexus_ai_agent.observability.metrics import inc_jobs_completed

        inc_jobs_completed(job_type)
    except Exception:
        pass

    try:
        log_lifecycle_event(
            logging.INFO,
            EVENT_JOB_COMPLETED,
            job_id=_truncate_job_id(job_id),
            job_type=job_type,
            correlation_id=correlation_id or get_correlation_id() or "none",
            duration_ms=round(duration_ms, 1),
        )
    except Exception:
        pass


def log_job_created(job_id: str, job_type: str, correlation_id: str | None = None) -> None:
    try:
        from nexus_ai_agent.observability.metrics import inc_jobs_created

        inc_jobs_created(job_type)
    except Exception:
        pass

    try:
        log_lifecycle_event(
            logging.INFO,
            EVENT_JOB_CREATED,
            job_id=_truncate_job_id(job_id),
            job_type=job_type,
            correlation_id=correlation_id or get_correlation_id() or "none",
        )
    except Exception:
        pass


def log_job_claimed(job_id: str, job_type: str, correlation_id: str | None = None) -> None:
    try:
        from nexus_ai_agent.observability.metrics import inc_jobs_claimed

        inc_jobs_claimed(job_type)
    except Exception:
        pass

    try:
        log_lifecycle_event(
            logging.INFO,
            EVENT_JOB_CLAIMED,
            job_id=_truncate_job_id(job_id),
            job_type=job_type,
            correlation_id=correlation_id or get_correlation_id() or "none",
        )
    except Exception:
        pass


def log_job_recovered(job_id: str, reason: str, correlation_id: str | None = None) -> None:
    try:
        from nexus_ai_agent.observability.metrics import inc_jobs_recovered

        inc_jobs_recovered(reason)
    except Exception:
        pass

    try:
        log_lifecycle_event(
            logging.WARNING,
            EVENT_JOB_RECOVERED,
            job_id=_truncate_job_id(job_id),
            reason=reason,
            correlation_id=correlation_id or get_correlation_id() or "none",
        )
    except Exception:
        pass


# Helper to measure duration
class JobTimer:
    """Simple timer for job duration — hostile-clock safe (monotonic)."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start


__all__ = [
    "EVENT_JOB_CLAIMED",
    "EVENT_JOB_COMPLETED",
    "EVENT_JOB_CREATED",
    "EVENT_JOB_FAILED",
    "EVENT_JOB_RECOVERED",
    "JobFailureContext",
    "JobTimer",
    "log_job_claimed",
    "log_job_completed",
    "log_job_created",
    "log_job_failure",
    "log_job_recovered",
]
