"""M0 observability contract: canonical job metrics.

This module defines canonical metric names, label dimensions,
cardinality policy, source events, and lifecycle. It wraps the
infrastructure registry but provides typed, M0-compliant facade.

Metrics:
  1. jobs_created_total   — job persisted via enqueue
  2. jobs_claimed_total   — job moved pending->processing
  3. jobs_completed_total — job reached completed
  4. jobs_failed_total    — job reached failed
  5. jobs_recovered_total — job recovered from expired lease

For each metric:
  - canonical name: as above, plus nexus_ alias for compat
  - label dimensions: job_type, error_code, reason, outcome (bounded)
  - cardinality policy: no telegram_id, URL, API key, raw text, UUID
  - source event: which code path increments it
  - lifecycle: counter, never reset except restart/test, monotonic
  - test: deterministic unit test proving increment and guard

High-cardinality guard: any label >64 chars, containing ://, or @ is rejected.
job_type for canonical metrics normalized to bounded set; unknown->"unknown".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from nexus_ai_agent.infrastructure.observability.metrics import (
    BOUNDED_JOB_TYPES,
    CANONICAL_JOB_METRICS,
    get_metrics_registry,
)

# Canonical names — primary (without prefix) is source of truth
JOBS_CREATED_TOTAL: Final[str] = "jobs_created_total"
JOBS_CLAIMED_TOTAL: Final[str] = "jobs_claimed_total"
JOBS_COMPLETED_TOTAL: Final[str] = "jobs_completed_total"
JOBS_FAILED_TOTAL: Final[str] = "jobs_failed_total"
JOBS_RECOVERED_TOTAL: Final[str] = "jobs_recovered_total"

ALL_JOB_METRICS: Final[tuple[str, ...]] = (
    JOBS_CREATED_TOTAL,
    JOBS_CLAIMED_TOTAL,
    JOBS_COMPLETED_TOTAL,
    JOBS_FAILED_TOTAL,
    JOBS_RECOVERED_TOTAL,
)

# Histogram + gauge names (M0 runtime integration; buckets frozen at M0).
JOBS_DURATION_SECONDS: Final[str] = "job_duration_seconds"
QUEUE_DEPTH: Final[str] = "queue_depth"
JOBS_INFLIGHT: Final[str] = "jobs_inflight"

# Bounded label values
BOUNDED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "handler_not_found",
        "handler_exception",
        "payload_invalid",
        "timeout",
        "cancelled",
        "unknown",
        "validation_error",
        "external_api_error",
    }
)

BOUNDED_REASONS: Final[frozenset[str]] = frozenset(
    {
        "expired_lease",
        "startup_recovery",
        "orphaned_processing",
        "pending_requeue",
        "operator_resume",
        "unknown",
    }
)

BOUNDED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "success",
        "failure",
        "recovered",
    }
)


@dataclass(frozen=True)
class MetricContract:
    """Documentation contract for a single metric."""

    name: str
    description: str
    labels: tuple[str, ...]
    cardinality_policy: str
    source_event: str
    lifecycle: str


METRIC_CONTRACTS: Final[dict[str, MetricContract]] = {
    JOBS_CREATED_TOTAL: MetricContract(
        name=JOBS_CREATED_TOTAL,
        description="Number of jobs persisted via enqueue (durable creation)",
        labels=("job_type",),
        cardinality_policy=(
            "job_type from BOUNDED_JOB_TYPES; unknown->'unknown'; "
            "no telegram_id/URL/UUID/raw text; max 64 chars"
        ),
        source_event=(
            "InProcessJobQueue.enqueue / _insert_or_get — after INSERT; "
            "idempotent hit NOT counted as created"
        ),
        lifecycle=(
            "Counter, monotonic, process-local, reset only on restart/test. "
            "Incremented once per new job row."
        ),
    ),
    JOBS_CLAIMED_TOTAL: MetricContract(
        name=JOBS_CLAIMED_TOTAL,
        description="Number of jobs claimed (pending -> processing)",
        labels=("job_type",),
        cardinality_policy="Same as created; bounded job_type only",
        source_event=("InProcessJobQueue._mark_processing — when job moves to processing"),
        lifecycle=(
            "Counter, monotonic. Each claim that transitions state increments. "
            "Duplicate guards prevent double-counting."
        ),
    ),
    JOBS_COMPLETED_TOTAL: MetricContract(
        name=JOBS_COMPLETED_TOTAL,
        description="Number of jobs that reached completed",
        labels=("job_type",),
        cardinality_policy="Bounded job_type only",
        source_event="InProcessJobQueue._mark_completed — after handler returns",
        lifecycle="Counter, monotonic, terminal state only.",
    ),
    JOBS_FAILED_TOTAL: MetricContract(
        name=JOBS_FAILED_TOTAL,
        description="Number of jobs that reached failed",
        labels=("job_type", "error_code"),
        cardinality_policy=(
            "job_type bounded, error_code bounded; unknown->'unknown'; "
            "no raw exception text as label"
        ),
        source_event="InProcessJobQueue._mark_failed — when handler raises",
        lifecycle="Counter, monotonic, terminal, paired with diagnostics event.",
    ),
    JOBS_RECOVERED_TOTAL: MetricContract(
        name=JOBS_RECOVERED_TOTAL,
        description="Number of jobs recovered from expired lease",
        labels=("reason",),
        cardinality_policy="reason bounded; unknown->'unknown'",
        source_event=(
            "InProcessJobQueue._reset_unfinished / resume_pending — "
            "when pending+processing reset to pending"
        ),
        lifecycle="Counter, monotonic, incremented per recovered row.",
    ),
}


def _normalize_job_type(job_type: str) -> str:
    jt = job_type.strip()
    if not jt:
        return "unknown"
    if jt in BOUNDED_JOB_TYPES:
        return jt
    return "unknown"


def _normalize_error_code(code: str) -> str:
    c = code.strip().lower()
    if c in BOUNDED_ERROR_CODES:
        return c
    return "unknown"


def _normalize_reason(reason: str) -> str:
    r = reason.strip().lower()
    if r in BOUNDED_REASONS:
        return r
    return "unknown"


def inc_jobs_created(job_type: str) -> None:
    get_metrics_registry().increment(
        JOBS_CREATED_TOTAL, labels={"job_type": _normalize_job_type(job_type)}
    )


def inc_jobs_claimed(job_type: str) -> None:
    get_metrics_registry().increment(
        JOBS_CLAIMED_TOTAL, labels={"job_type": _normalize_job_type(job_type)}
    )


def inc_jobs_completed(job_type: str) -> None:
    get_metrics_registry().increment(
        JOBS_COMPLETED_TOTAL, labels={"job_type": _normalize_job_type(job_type)}
    )


def inc_jobs_failed(job_type: str, error_code: str = "unknown") -> None:
    get_metrics_registry().increment(
        JOBS_FAILED_TOTAL,
        labels={
            "job_type": _normalize_job_type(job_type),
            "error_code": _normalize_error_code(error_code),
        },
    )


def inc_jobs_recovered(reason: str = "unknown") -> None:
    get_metrics_registry().increment(
        JOBS_RECOVERED_TOTAL, labels={"reason": _normalize_reason(reason)}
    )


def set_queue_depth(value: int) -> None:
    """Gauge: current pending rows — set() from a SELECT COUNT, never tracked."""
    if value < 0:
        raise ValueError(f"queue_depth must be non-negative, got {value}")
    get_metrics_registry().set_gauge("queue_depth", float(value))


def set_jobs_inflight(value: int) -> None:
    """Gauge: current processing rows — set() from a SELECT COUNT, never tracked."""
    if value < 0:
        raise ValueError(f"jobs_inflight must be non-negative, got {value}")
    get_metrics_registry().set_gauge("jobs_inflight", float(value))


def observe_job_duration(job_type: str, seconds: float) -> None:
    """Histogram: one claim-to-terminal duration observation (frozen M0 buckets)."""
    get_metrics_registry().observe(
        JOBS_DURATION_SECONDS, seconds, labels={"job_type": _normalize_job_type(job_type)}
    )


def snapshot() -> dict[str, float]:
    return get_metrics_registry().snapshot()


def reset_for_tests() -> None:
    """Test-only reset."""
    from nexus_ai_agent.infrastructure.observability.metrics import (
        reset_metrics_registry,
    )

    reset_metrics_registry()


__all__ = [
    "ALL_JOB_METRICS",
    "BOUNDED_ERROR_CODES",
    "BOUNDED_OUTCOMES",
    "BOUNDED_REASONS",
    "CANONICAL_JOB_METRICS",
    "JOBS_CLAIMED_TOTAL",
    "JOBS_COMPLETED_TOTAL",
    "JOBS_CREATED_TOTAL",
    "JOBS_DURATION_SECONDS",
    "JOBS_FAILED_TOTAL",
    "JOBS_INFLIGHT",
    "JOBS_RECOVERED_TOTAL",
    "METRIC_CONTRACTS",
    "MetricContract",
    "QUEUE_DEPTH",
    "inc_jobs_claimed",
    "inc_jobs_completed",
    "inc_jobs_created",
    "inc_jobs_failed",
    "inc_jobs_recovered",
    "observe_job_duration",
    "reset_for_tests",
    "set_jobs_inflight",
    "set_queue_depth",
    "snapshot",
]
