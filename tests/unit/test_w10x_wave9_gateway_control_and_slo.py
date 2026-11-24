"""Wave 9: PID concurrency governor, SPRT breaker and SLO burn calculus."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.llm.gateway.control_theory_governor import (
    CircuitDecision,
    CircuitState,
    ConcurrencyGovernor,
    GovernorStep,
    PIDConfig,
    SPRTCircuitBreaker,
    SPRTConfig,
    SPRTError,
)
from nexus_ai_agent.observability.slo_burn_calculus import (
    BurnWindowResult,
    SLOBurnEvaluator,
    SLOConfigError,
    SLOReport,
    SLOState,
    SLOWindow,
    burn_rate,
    error_budget_remaining,
)


def test_pid_config_and_governor_validation() -> None:
    PIDConfig(1.0, 0.1, 0.01)
    for bad in (-1.0, math.nan):
        with pytest.raises(SPRTError):
            PIDConfig(bad, 0.1, 0.0)
        with pytest.raises(SPRTError):
            PIDConfig(1.0, bad, 0.0)
        with pytest.raises(SPRTError):
            PIDConfig(1.0, 0.1, bad)
        with pytest.raises(SPRTError):
            PIDConfig(1.0, 0.1, 0.0, tau_d=bad)
        with pytest.raises(SPRTError):
            PIDConfig(1.0, 0.1, 0.0, k_aw=bad)
    with pytest.raises(TypeError):
        PIDConfig(True, 0.1, 0.0)  # type: ignore[arg-type]

    config = PIDConfig(1.0, 0.1, 0.0)
    with pytest.raises(TypeError):
        ConcurrencyGovernor("config", target_latency_ms=100.0)  # type: ignore[arg-type]
    with pytest.raises(SPRTError):
        ConcurrencyGovernor(config, target_latency_ms=0.0)
    with pytest.raises(SPRTError):
        ConcurrencyGovernor(config, target_latency_ms=100.0, minimum=0.0)
    with pytest.raises(SPRTError):
        ConcurrencyGovernor(config, target_latency_ms=100.0, minimum=10.0, maximum=5.0)
    with pytest.raises(SPRTError):
        ConcurrencyGovernor(config, target_latency_ms=100.0, base=0.0)
    with pytest.raises(SPRTError):
        ConcurrencyGovernor(config, target_latency_ms=100.0, base=200.0)


def test_pid_controls_concurrency_and_anti_windup() -> None:
    governor = ConcurrencyGovernor(
        PIDConfig(kp=10.0, ki=1.0, kd=1.0, tau_d=0.5, k_aw=1.0),
        target_latency_ms=100.0,
        minimum=1.0,
        maximum=50.0,
        base=10.0,
    )
    fast = governor.update(50.0, 1.0)
    assert fast.limit > 10.0
    assert fast.error > 0.0
    assert fast.saturated is False
    slow = governor.update(1000.0, 1.0)
    assert slow.limit == 1.0
    assert slow.saturated is True
    for _ in range(50):
        step = governor.update(5000.0, 1.0)
    assert abs(step.integral) <= 50.0
    recovered = governor.update(10.0, 1.0)
    assert recovered.limit > 1.0
    governor.reset()
    assert governor.integral == 0.0
    assert governor.update(50.0, 1.0).limit > 0.0
    assert GovernorStep(1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, False).limit == 1.0


def test_pid_input_validation() -> None:
    governor = ConcurrencyGovernor(PIDConfig(1.0, 0.1, 0.1), target_latency_ms=100.0)
    with pytest.raises(SPRTError):
        governor.update(0.0, 1.0)
    with pytest.raises(SPRTError):
        governor.update(100.0, 0.0)
    with pytest.raises(SPRTError):
        governor.update(math.nan, 1.0)
    with pytest.raises(SPRTError):
        governor.update(100.0, math.inf)
    with pytest.raises(TypeError):
        governor.update(True, 1.0)  # type: ignore[arg-type]
    with pytest.raises(SPRTError):
        GovernorStep(math.nan, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, False)
    with pytest.raises(TypeError):
        GovernorStep(1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)  # type: ignore[arg-type]


def test_sprt_config_and_thresholds() -> None:
    config = SPRTConfig()
    assert config.upper_threshold == pytest.approx(math.log(0.9 / 0.05))
    assert config.lower_threshold == pytest.approx(math.log(0.10 / 0.95))
    for kwargs in (
        {"p0": 0.0},
        {"p0": 0.5, "p1": 0.4},
        {"alpha": 0.0},
        {"beta": 1.0},
        {"alpha": 0.6, "beta": 0.5},
    ):
        with pytest.raises(SPRTError):
            SPRTConfig(**kwargs)
    with pytest.raises(TypeError):
        SPRTConfig(p0=True)  # type: ignore[arg-type]


def test_sprt_trip_and_recovery() -> None:
    breaker = SPRTCircuitBreaker(SPRTConfig())
    healthy = breaker.record(True)
    assert healthy.state is CircuitState.CLOSED
    assert healthy.allow_request is True
    assert healthy.log_likelihood_ratio < 0.0
    breaker.record(False)
    tripped = breaker.record(False)
    assert tripped.tripped is True
    assert tripped.state is CircuitState.OPEN
    assert tripped.allow_request is False
    assert tripped.posterior_failure_mean == pytest.approx(3.0 / 5.0)
    frozen = breaker.record(False)
    assert frozen.tripped is False
    assert breaker.failure_count == 2
    assert breaker.success_count == 1
    assert breaker.log_likelihood_ratio > 0.0
    with pytest.raises(SPRTError):
        SPRTCircuitBreaker(SPRTConfig()).enter_half_open()
    probing = breaker.enter_half_open()
    assert probing.state is CircuitState.HALF_OPEN
    decisions = [breaker.record(True) for _ in range(100)]
    assert any(decision.recovered for decision in decisions)
    assert breaker.state is CircuitState.CLOSED
    breaker.reset()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.log_likelihood_ratio == 0.0
    assert breaker.failure_count == 0
    assert breaker.success_count == 0


def test_sprt_validation_and_decision() -> None:
    with pytest.raises(TypeError):
        SPRTCircuitBreaker("config")  # type: ignore[arg-type]
    with pytest.raises(SPRTError):
        SPRTCircuitBreaker(SPRTConfig(), prior_alpha=0.0)
    with pytest.raises(SPRTError):
        SPRTCircuitBreaker(SPRTConfig(), prior_beta=-1.0)
    breaker = SPRTCircuitBreaker(SPRTConfig())
    with pytest.raises(TypeError):
        breaker.record(1)  # type: ignore[arg-type]
    CircuitDecision(CircuitState.CLOSED, True, 0.0, 0.5, False, False)
    with pytest.raises(SPRTError):
        CircuitDecision("CLOSED", True, 0.0, 0.5, False, False)  # type: ignore[arg-type]
    with pytest.raises(SPRTError):
        CircuitDecision(CircuitState.CLOSED, True, math.nan, 0.5, False, False)
    with pytest.raises(SPRTError):
        CircuitDecision(CircuitState.CLOSED, True, 0.0, 1.5, False, False)
    with pytest.raises(TypeError):
        CircuitDecision(CircuitState.CLOSED, 1, 0.0, 0.5, False, False)  # type: ignore[arg-type]


# ---------------------------------------------------------------- SLO calculus


def test_burn_rate_and_budget_helpers() -> None:
    assert burn_rate(0, 100, 0.99) == 0.0
    assert burn_rate(1, 100, 0.99) == pytest.approx(1.0)
    assert burn_rate(2, 100, 0.99) == pytest.approx(2.0)
    assert error_budget_remaining(0, 100, 0.99) == 1.0
    assert error_budget_remaining(1, 100, 0.99) == pytest.approx(0.0)
    assert error_budget_remaining(0, 100, 0.9) == 1.0
    for args in ((0, 0, 0.99), (-1, 10, 0.99), (11, 10, 0.99), (0, 10, 1.0), (0, 10, 0.0)):
        with pytest.raises(SLOConfigError):
            burn_rate(*args)
        with pytest.raises(SLOConfigError):
            error_budget_remaining(*args)


def test_slo_window_validation() -> None:
    SLOWindow("5m", 300.0)
    with pytest.raises(SLOConfigError):
        SLOWindow("", 300.0)
    with pytest.raises(SLOConfigError):
        SLOWindow("5m", 0.0)
    with pytest.raises(SLOConfigError):
        SLOWindow("5m", 300.0, warning_burn=0.0)
    with pytest.raises(SLOConfigError):
        SLOWindow("5m", 300.0, critical_burn=0.0)
    with pytest.raises(SLOConfigError):
        SLOWindow("5m", 300.0, warning_burn=5.0, critical_burn=1.0)


def test_slo_states() -> None:
    windows = (SLOWindow("5m", 300.0), SLOWindow("1h", 3600.0))
    evaluator = SLOBurnEvaluator(0.999, windows)
    assert evaluator.target == 0.999
    assert evaluator.windows == windows
    healthy = evaluator.evaluate({"5m": (100000, 100000), "1h": (100000, 100000)})
    assert healthy.state is SLOState.HEALTHY
    assert healthy.overall_remaining == 1.0
    warning = evaluator.evaluate({"5m": (9970, 10000), "1h": (99999, 100000)})
    assert warning.state is SLOState.WARNING_BURN
    critical = evaluator.evaluate({"5m": (9920, 10000), "1h": (100000, 100000)})
    assert critical.state is SLOState.CRITICAL_PAGE_BURN
    exhausted = evaluator.evaluate({"5m": (900, 1000), "1h": (100000, 100000)})
    assert exhausted.state is SLOState.BUDGET_EXHAUSTED_FREEZE
    empty = evaluator.evaluate({"5m": (0, 0), "1h": (0, 0)})
    assert empty.state is SLOState.HEALTHY
    assert empty.windows[0].burn_rate == 0.0
    multi = evaluator.evaluate({"5m": (9920, 10000), "1h": (999500, 1000000)})
    assert multi.state is SLOState.CRITICAL_PAGE_BURN


def test_slo_evaluator_validation() -> None:
    window = SLOWindow("5m", 300.0)
    with pytest.raises(SLOConfigError):
        SLOBurnEvaluator(1.0, (window,))
    with pytest.raises(SLOConfigError):
        SLOBurnEvaluator(0.99, ())
    with pytest.raises(SLOConfigError):
        SLOBurnEvaluator(0.99, (window, window))
    evaluator = SLOBurnEvaluator(0.99, (window,))
    with pytest.raises(SLOConfigError):
        evaluator.evaluate({})
    with pytest.raises(SLOConfigError):
        evaluator.evaluate({"5m": (1,)})
    with pytest.raises(SLOConfigError):
        evaluator.evaluate({"5m": (-1, 10)})
    with pytest.raises(SLOConfigError):
        evaluator.evaluate({"5m": (11, 10)})
    with pytest.raises(SLOConfigError):
        evaluator.evaluate({"5m": (True, 10)})  # type: ignore[dict-item]


def test_burn_result_and_report_validation() -> None:
    result = BurnWindowResult("5m", 99, 1, 100, 0.01, 1.0, 0.0)
    assert result.failures == 1
    with pytest.raises(SLOConfigError):
        BurnWindowResult("5m", 99, 2, 100, 0.01, 1.0, 0.0)
    with pytest.raises(SLOConfigError):
        BurnWindowResult("5m", -1, 1, 0, 0.01, 1.0, 0.0)
    with pytest.raises(SLOConfigError):
        BurnWindowResult("5m", 99, 1, 100, -0.01, 1.0, 0.0)
    with pytest.raises(SLOConfigError):
        BurnWindowResult("5m", 99, 1, 100, 1.1, 1.0, 0.0)
    SLOReport(SLOState.HEALTHY, (result,), 0.0, 0.99)
    with pytest.raises(SLOConfigError):
        SLOReport("HEALTHY", (result,), 0.0, 0.99)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SLOReport(SLOState.HEALTHY, ("x",), 0.0, 0.99)  # type: ignore[arg-type]
    with pytest.raises(SLOConfigError):
        SLOReport(SLOState.HEALTHY, (), 0.0, 0.99)
    with pytest.raises(SLOConfigError):
        SLOReport(SLOState.HEALTHY, (result,), 1.5, 0.99)
    with pytest.raises(SLOConfigError):
        SLOReport(SLOState.HEALTHY, (result,), 0.0, 1.0)
