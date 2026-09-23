"""SLO signals and alert policies — M0 tests."""

from nexus_ai_agent.observability.slo import (
    ALERT_HIGH_FAILURE_RATE,
    ALERT_QUEUE_STUCK,
    ALERT_READINESS_DEGRADED,
    ALL_ALERT_POLICIES,
    GOLDEN_SIGNALS,
    QueueSaturation,
    SignalType,
    evaluate_failure_rate,
    measure_saturation_from_counts,
    should_alert_failure_rate,
    should_alert_queue_stuck,
    should_alert_readiness_degraded,
)


def test_golden_signals_defined():
    assert SignalType.LATENCY in GOLDEN_SIGNALS
    assert SignalType.TRAFFIC in GOLDEN_SIGNALS
    assert SignalType.ERRORS in GOLDEN_SIGNALS
    assert SignalType.SATURATION in GOLDEN_SIGNALS
    for _sig, contract in GOLDEN_SIGNALS.items():
        assert "sli" in contract
        assert "description" in contract
        assert "source" in contract
        assert "metric" in contract


def test_alert_policies_exist():
    assert len(ALL_ALERT_POLICIES) == 3
    names = {p.name for p in ALL_ALERT_POLICIES}
    assert "high_failure_rate" in names
    assert "queue_stuck_no_progress" in names
    assert "readiness_degraded" in names


def test_high_failure_rate_policy():
    policy = ALERT_HIGH_FAILURE_RATE
    assert policy.signal == SignalType.ERRORS
    assert policy.threshold == 0.2
    assert policy.comparison == "gt"
    # Evaluate
    assert policy.evaluate(0.3) is True
    assert policy.evaluate(0.1) is False
    assert policy.evaluate(0.2) is False  # gt, not gte


def test_queue_stuck_policy():
    policy = ALERT_QUEUE_STUCK
    assert policy.signal == SignalType.SATURATION
    assert policy.evaluate(700) is True
    assert policy.evaluate(100) is False


def test_readiness_degraded_policy():
    policy = ALERT_READINESS_DEGRADED
    assert policy.signal == SignalType.ERRORS
    assert policy.evaluate(70) is True
    assert policy.evaluate(10) is False


def test_evaluate_failure_rate():
    assert evaluate_failure_rate(0, 0) == 0.0
    assert evaluate_failure_rate(0, 10) == 0.0
    assert evaluate_failure_rate(5, 10) == 0.5
    assert evaluate_failure_rate(2, 10) == 0.2


def test_should_alert_failure_rate():
    assert should_alert_failure_rate(3, 10, threshold=0.2) is True  # 0.3 > 0.2
    assert should_alert_failure_rate(1, 10, threshold=0.2) is False  # 0.1
    assert should_alert_failure_rate(2, 10, threshold=0.2) is False  # exactly 0.2 not >


def test_should_alert_queue_stuck():
    # No pending => no alert
    assert should_alert_queue_stuck(0, 1000) is False
    # Pending and stuck long
    assert should_alert_queue_stuck(5, 700, threshold_seconds=600) is True
    # Pending but recent progress
    assert should_alert_queue_stuck(5, 100, threshold_seconds=600) is False


def test_should_alert_readiness_degraded():
    assert should_alert_readiness_degraded(True, 100) is False  # ready
    assert should_alert_readiness_degraded(False, 70, threshold_seconds=60) is True
    assert should_alert_readiness_degraded(False, 10, threshold_seconds=60) is False


def test_queue_saturation_measurement():
    sat = measure_saturation_from_counts(pending=5, processing=2, completed=10, failed=1)
    assert sat.depth == 5
    assert sat.inflight == 2
    assert sat.total == 18
    assert 0 <= sat.utilization <= 1
    assert sat.utilization == 2 / 7


def test_saturation_utilization_edge():
    sat = QueueSaturation(pending=0, processing=0, completed=0, failed=0)
    assert sat.utilization == 0.0
    assert sat.depth == 0
    assert sat.inflight == 0


def test_latency_sli_contract():
    latency = GOLDEN_SIGNALS[SignalType.LATENCY]
    assert "job_duration" in latency["sli"]
    assert "duration" in latency["description"].lower() or "time" in latency["description"].lower()


def test_traffic_sli_contract():
    traffic = GOLDEN_SIGNALS[SignalType.TRAFFIC]
    assert "jobs_created" in traffic["sli"] or "created" in traffic["sli"].lower()


def test_errors_sli_contract():
    errors = GOLDEN_SIGNALS[SignalType.ERRORS]
    assert "failed" in errors["sli"].lower()


def test_saturation_sli_contract():
    sat = GOLDEN_SIGNALS[SignalType.SATURATION]
    assert (
        "queue_depth" in sat["sli"]
        or "depth" in sat["sli"].lower()
        or "pending" in sat["sli"].lower()
    )
