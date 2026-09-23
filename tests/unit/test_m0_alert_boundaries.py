"""M0 alert evaluator boundary tests — exact thresholds, epsilon, no-data.

Three deterministic evaluators from ``observability.slo``:

1. high failure rate (ratio > 0.2 over the window)
2. queue stuck (pending > 0 AND seconds-since-last-completion > 600)
3. readiness degraded (not ready AND seconds-not-ready > 60)

Each is pinned at: exact threshold (strict > means NO alert ON the boundary),
epsilon above (alert), epsilon below (no alert), no-data, startup, and
post-reset. Window/threshold values are asserted so a silent policy drift
fails a test rather than paging nobody.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.infrastructure.observability.metrics import reset_metrics_registry
from nexus_ai_agent.observability.metrics import (
    inc_jobs_created,
    inc_jobs_failed,
    snapshot,
)
from nexus_ai_agent.observability.slo import (
    ALERT_HIGH_FAILURE_RATE,
    ALERT_QUEUE_STUCK,
    ALERT_READINESS_DEGRADED,
    evaluate_failure_rate,
    should_alert_failure_rate,
    should_alert_queue_stuck,
    should_alert_readiness_degraded,
)

EPS = 1e-9


@pytest.fixture(autouse=True)
def _reset():
    reset_metrics_registry()
    yield
    reset_metrics_registry()


# -- policy pins (input/window/threshold/decision/unit) ----------------------


def test_policy_thresholds_windows_and_comparisons_are_pinned():
    assert ALERT_HIGH_FAILURE_RATE.threshold == 0.2
    assert ALERT_HIGH_FAILURE_RATE.window_seconds == 300
    assert ALERT_HIGH_FAILURE_RATE.comparison == "gt"
    assert ALERT_HIGH_FAILURE_RATE.severity == "critical"
    assert ALERT_HIGH_FAILURE_RATE.signal.value == "errors"

    assert ALERT_QUEUE_STUCK.threshold == 600.0
    assert ALERT_QUEUE_STUCK.window_seconds == 600
    assert ALERT_QUEUE_STUCK.comparison == "gt"
    assert ALERT_QUEUE_STUCK.severity == "warning"
    assert ALERT_QUEUE_STUCK.signal.value == "saturation"

    assert ALERT_READINESS_DEGRADED.threshold == 60.0
    assert ALERT_READINESS_DEGRADED.window_seconds == 60
    assert ALERT_READINESS_DEGRADED.comparison == "gt"
    assert ALERT_READINESS_DEGRADED.severity == "critical"


# -- evaluator 1: high failure rate ------------------------------------------


def test_failure_rate_exact_threshold_does_not_fire():
    # 1/5 == 0.2 exactly; decision is strict gt -> no alert ON the boundary
    assert evaluate_failure_rate(1, 5) == 0.2
    assert should_alert_failure_rate(1, 5, threshold=0.2) is False


def test_failure_rate_epsilon_above_fires_below_does_not():
    assert should_alert_failure_rate(1, 5, threshold=0.2 - EPS) is True
    assert should_alert_failure_rate(1, 5, threshold=0.2 + EPS) is False


def test_failure_rate_no_data_never_fires():
    assert evaluate_failure_rate(0, 0) == 0.0
    assert should_alert_failure_rate(0, 0) is False
    assert should_alert_failure_rate(0, 10) is False  # all succeeded


def test_failure_rate_startup_zero_jobs_no_fire():
    # startup: counters all zero -> rate 0 -> no alert
    snap = snapshot()
    assert all(value == 0 for value in snap.values()) or not snap
    assert should_alert_failure_rate(0, 0) is False


def test_failure_rate_post_reset_no_fire():
    inc_jobs_created("story")
    inc_jobs_failed("story", error_code="handler_exception")
    assert should_alert_failure_rate(1, 2) is True  # 50% > 20%
    reset_metrics_registry()
    snap = snapshot()
    assert not snap
    assert should_alert_failure_rate(0, 0) is False


# -- evaluator 2: queue stuck ------------------------------------------------


def test_queue_stuck_exact_threshold_does_not_fire():
    assert should_alert_queue_stuck(3, 600.0, threshold_seconds=600.0) is False  # strict gt
    assert should_alert_queue_stuck(3, 600.0 + EPS, threshold_seconds=600.0) is True
    assert should_alert_queue_stuck(3, 600.0 - EPS, threshold_seconds=600.0) is False


def test_queue_stuck_never_fires_with_empty_queue():
    # startup / drained: pending == 0 regardless of last-completion age
    assert should_alert_queue_stuck(0, 10_000.0) is False
    assert should_alert_queue_stuck(-1, 10_000.0) is False  # hostile input


def test_queue_stuck_boundary_pending_one():
    assert should_alert_queue_stuck(1, 601.0) is True
    assert should_alert_queue_stuck(1, 599.0) is False


# -- evaluator 3: readiness degraded -----------------------------------------


def test_readiness_exact_threshold_does_not_fire():
    assert should_alert_readiness_degraded(False, 60.0, threshold_seconds=60.0) is False
    assert should_alert_readiness_degraded(False, 60.0 + EPS, threshold_seconds=60.0) is True
    assert should_alert_readiness_degraded(False, 59.999, threshold_seconds=60.0) is False


def test_readiness_never_fires_while_ready():
    assert should_alert_readiness_degraded(True, 10_000.0) is False


def test_readiness_startup_just_went_not_ready_no_fire():
    assert should_alert_readiness_degraded(False, 0.0) is False
    assert should_alert_readiness_degraded(False, 1.0) is False


def test_readiness_post_recovery_reset_no_fire():
    # recovering flips is_ready True -> evaluator disarms immediately
    assert should_alert_readiness_degraded(False, 120.0) is True
    assert should_alert_readiness_degraded(True, 120.0) is False
