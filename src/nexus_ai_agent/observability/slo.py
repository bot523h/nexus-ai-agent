"""SLO signals — M0 golden signals and alert policies.

Golden signals:
  LATENCY: job duration
  TRAFFIC: jobs created / updates observed
  ERRORS: failed jobs / failed effects
  SATURATION: queue depth / inflight / worker utilization

Three alert policies:
  - high failure rate
  - queue stuck / no progress
  - readiness degraded

No real alert delivery needed for M0; metric + threshold + testable evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SignalType(str, Enum):
    LATENCY = "latency"
    TRAFFIC = "traffic"
    ERRORS = "errors"
    SATURATION = "saturation"


@dataclass(frozen=True)
class SliMeasurement:
    """Single SLI measurement — low-cardinality, testable."""

    signal: SignalType
    name: str
    value: float
    # Optional labels, bounded
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertPolicy:
    """Alert policy: metric + threshold + evaluator."""

    name: str
    description: str
    signal: SignalType
    metric_name: str
    threshold: float
    comparison: str  # "gt", "lt", "gte", "lte"
    window_seconds: int
    severity: str  # "warning", "critical"

    def evaluate(self, value: float) -> bool:
        """Return True if alert should fire."""
        if self.comparison == "gt":
            return value > self.threshold
        if self.comparison == "gte":
            return value >= self.threshold
        if self.comparison == "lt":
            return value < self.threshold
        if self.comparison == "lte":
            return value <= self.threshold
        raise ValueError(f"unknown comparison {self.comparison!r}")


# --- Golden signals contracts ---

GOLDEN_SIGNALS: dict[SignalType, dict[str, str]] = {
    SignalType.LATENCY: {
        "sli": "job_duration_seconds",
        "description": "Time from claim to terminal (completed/failed)",
        "source": "JobTimer elapsed, or queue started_at/finished_at",
        "metric": "histogram of job duration; M0 uses counter+avg via logs",
        "unit": "seconds",
    },
    SignalType.TRAFFIC: {
        "sli": "jobs_created_total + updates_observed_total",
        "description": "Rate of jobs created and Telegram updates observed",
        "source": "jobs_created_total metric + bot handlers counter",
        "metric": "jobs_created_total, nexus_touch_total, etc.",
        "unit": "jobs/sec or updates/sec",
    },
    SignalType.ERRORS: {
        "sli": "jobs_failed_total / jobs_created_total",
        "description": "Failure rate of jobs and effects",
        "source": "jobs_failed_total metric, lifecycle failure events",
        "metric": "jobs_failed_total, error_code label",
        "unit": "ratio 0..1",
    },
    SignalType.SATURATION: {
        "sli": "queue_depth, inflight, worker_utilization",
        "description": "Queue depth (pending), inflight (processing), utilization",
        "source": "SELECT COUNT(*) WHERE status=pending/processing; or _tasks dict",
        "metric": "queue_depth, inflight_jobs, worker_utilization",
        "unit": "count or ratio",
    },
}


# --- Three initial alert policies ---

ALERT_HIGH_FAILURE_RATE = AlertPolicy(
    name="high_failure_rate",
    description="Job failure rate > 20% over 5 minutes",
    signal=SignalType.ERRORS,
    metric_name="jobs_failed_total / jobs_created_total",
    threshold=0.2,
    comparison="gt",
    window_seconds=300,
    severity="critical",
)

ALERT_QUEUE_STUCK = AlertPolicy(
    name="queue_stuck_no_progress",
    description="No job completed in last 10 minutes while pending > 0, or queue depth growing",
    signal=SignalType.SATURATION,
    metric_name="queue_depth + time_since_last_completion",
    threshold=600.0,  # 10 minutes no progress
    comparison="gt",
    window_seconds=600,
    severity="warning",
)

ALERT_READINESS_DEGRADED = AlertPolicy(
    name="readiness_degraded",
    description="Readiness probe failing for > 60 seconds",
    signal=SignalType.ERRORS,
    metric_name="readiness_status",
    threshold=60.0,
    comparison="gt",
    window_seconds=60,
    severity="critical",
)

ALL_ALERT_POLICIES: tuple[AlertPolicy, ...] = (
    ALERT_HIGH_FAILURE_RATE,
    ALERT_QUEUE_STUCK,
    ALERT_READINESS_DEGRADED,
)


# --- Evaluators for M0 (testable, pure) ---


def evaluate_failure_rate(failed: int, total: int) -> float:
    """Return failure rate 0..1, safe for total=0."""
    if total <= 0:
        return 0.0
    return failed / total


def should_alert_failure_rate(failed: int, total: int, threshold: float = 0.2) -> bool:
    rate = evaluate_failure_rate(failed, total)
    return rate > threshold


def should_alert_queue_stuck(
    pending_count: int,
    seconds_since_last_completion: float,
    threshold_seconds: float = 600.0,
) -> bool:
    """Alert if pending >0 and no completion for threshold."""
    if pending_count <= 0:
        return False
    return seconds_since_last_completion > threshold_seconds


def should_alert_readiness_degraded(
    is_ready: bool,
    seconds_not_ready: float,
    threshold_seconds: float = 60.0,
) -> bool:
    if is_ready:
        return False
    return seconds_not_ready > threshold_seconds


# --- Saturation measurement helpers (from queue state) ---


@dataclass(frozen=True)
class QueueSaturation:
    pending: int
    processing: int
    completed: int
    failed: int

    @property
    def depth(self) -> int:
        return self.pending

    @property
    def inflight(self) -> int:
        return self.processing

    @property
    def total(self) -> int:
        return self.pending + self.processing + self.completed + self.failed

    @property
    def utilization(self) -> float:
        """Worker utilization: processing / (pending+processing) if any, else 0."""
        denom = self.pending + self.processing
        if denom == 0:
            return 0.0
        return self.processing / denom


def measure_saturation_from_counts(
    pending: int, processing: int, completed: int = 0, failed: int = 0
) -> QueueSaturation:
    return QueueSaturation(
        pending=pending, processing=processing, completed=completed, failed=failed
    )


__all__ = [
    "ALERT_HIGH_FAILURE_RATE",
    "ALERT_QUEUE_STUCK",
    "ALERT_READINESS_DEGRADED",
    "ALL_ALERT_POLICIES",
    "GOLDEN_SIGNALS",
    "AlertPolicy",
    "QueueSaturation",
    "SignalType",
    "SliMeasurement",
    "evaluate_failure_rate",
    "measure_saturation_from_counts",
    "should_alert_failure_rate",
    "should_alert_queue_stuck",
    "should_alert_readiness_degraded",
]
