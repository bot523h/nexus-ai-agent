"""LLM gateway control theory: PID concurrency and Wald's SPRT breaker.

The concurrency limit is a clipped PID controller with a filtered derivative
and back-calculation anti-windup, so provider outages cannot accumulate an
unbounded integral.  The circuit breaker is a sequential probability ratio
test between healthy and degraded failure rates, with an exact Bayesian
posterior failure mean reported alongside every decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CircuitDecision",
    "CircuitState",
    "ConcurrencyGovernor",
    "GovernorStep",
    "PIDConfig",
    "SPRTConfig",
    "SPRTCircuitBreaker",
    "SPRTError",
]


class SPRTError(ValueError):
    """Raised when control or statistical inputs violate their invariants."""


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise SPRTError(f"{label} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class PIDConfig:
    kp: float
    ki: float
    kd: float
    tau_d: float = 0.0
    k_aw: float = 0.0

    def __post_init__(self) -> None:
        for name in ("kp", "ki", "kd", "tau_d", "k_aw"):
            value = _finite(getattr(self, name), name)
            if value < 0.0:
                raise SPRTError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class GovernorStep:
    limit: float
    raw_limit: float
    error: float
    proportional: float
    integral: float
    derivative: float
    anti_windup: float
    saturated: bool

    def __post_init__(self) -> None:
        for name in (
            "limit",
            "raw_limit",
            "error",
            "proportional",
            "integral",
            "derivative",
            "anti_windup",
        ):
            if not math.isfinite(getattr(self, name)):
                raise SPRTError(f"{name} must be finite")
        if not isinstance(self.saturated, bool):
            raise TypeError("saturated must be a bool")


class ConcurrencyGovernor:
    """Discrete-time PID regulator for the active concurrency limit."""

    def __init__(
        self,
        config: PIDConfig,
        *,
        target_latency_ms: float,
        minimum: float = 1.0,
        maximum: float = 100.0,
        base: float = 10.0,
    ) -> None:
        if not isinstance(config, PIDConfig):
            raise TypeError("config must be a PIDConfig")
        self._config = config
        self._target = _finite(target_latency_ms, "target_latency_ms")
        self._minimum = _finite(minimum, "minimum")
        self._maximum = _finite(maximum, "maximum")
        self._base = _finite(base, "base")
        if self._target <= 0.0:
            raise SPRTError("target_latency_ms must be positive")
        if self._minimum <= 0.0 or self._maximum <= self._minimum:
            raise SPRTError("limits must satisfy 0 < minimum < maximum")
        if not self._minimum <= self._base <= self._maximum:
            raise SPRTError("base must lie inside [minimum, maximum]")
        self._integral = 0.0
        self._previous_error = 0.0
        self._derivative = 0.0

    @property
    def integral(self) -> float:
        return self._integral

    def update(self, latency_ms: float, dt_seconds: float) -> GovernorStep:
        latency = _finite(latency_ms, "latency_ms")
        dt = _finite(dt_seconds, "dt_seconds")
        if latency <= 0.0:
            raise SPRTError("latency_ms must be positive")
        if dt <= 0.0:
            raise SPRTError("dt_seconds must be positive")
        config = self._config
        error = (self._target - latency) / self._target
        proportional = config.kp * error
        derivative = (
            config.kd / (dt + config.tau_d) * (error - self._previous_error)
            + config.tau_d / (dt + config.tau_d) * self._derivative
        )
        raw = self._base + proportional + self._integral + derivative
        limit = max(self._minimum, min(self._maximum, raw))
        anti_windup = config.k_aw * (limit - raw) * dt
        self._integral += config.ki * error * dt + anti_windup
        self._integral = max(-self._maximum, min(self._maximum, self._integral))
        self._previous_error = error
        self._derivative = derivative
        return GovernorStep(
            limit=limit,
            raw_limit=raw,
            error=error,
            proportional=proportional,
            integral=self._integral,
            derivative=derivative,
            anti_windup=anti_windup,
            saturated=limit != raw,
        )

    def reset(self) -> None:
        self._integral = 0.0
        self._previous_error = 0.0
        self._derivative = 0.0


@dataclass(frozen=True, slots=True)
class SPRTConfig:
    p0: float = 0.01
    p1: float = 0.20
    alpha: float = 0.05
    beta: float = 0.10

    def __post_init__(self) -> None:
        for name in ("p0", "p1", "alpha", "beta"):
            value = _finite(getattr(self, name), name)
            if not 0.0 < value < 1.0:
                raise SPRTError(f"{name} must lie inside (0, 1)")
        if self.p0 >= self.p1:
            raise SPRTError("p0 must be smaller than p1")
        if self.alpha + self.beta >= 1.0:
            raise SPRTError("alpha + beta must be smaller than 1")

    @property
    def upper_threshold(self) -> float:
        return math.log((1.0 - self.beta) / self.alpha)

    @property
    def lower_threshold(self) -> float:
        return math.log(self.beta / (1.0 - self.alpha))


class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class CircuitDecision:
    state: CircuitState
    allow_request: bool
    log_likelihood_ratio: float
    posterior_failure_mean: float
    tripped: bool
    recovered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, CircuitState):
            raise SPRTError("state must be a CircuitState")
        for name in ("log_likelihood_ratio", "posterior_failure_mean"):
            if not math.isfinite(getattr(self, name)):
                raise SPRTError(f"{name} must be finite")
        if not 0.0 <= self.posterior_failure_mean <= 1.0:
            raise SPRTError("posterior_failure_mean must lie inside [0, 1]")
        for name in ("allow_request", "tripped", "recovered"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")


class SPRTCircuitBreaker:
    """Sequential probability ratio test with Bayesian posterior reporting."""

    def __init__(
        self,
        config: SPRTConfig,
        *,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ) -> None:
        if not isinstance(config, SPRTConfig):
            raise TypeError("config must be a SPRTConfig")
        for label, value in (("prior_alpha", prior_alpha), ("prior_beta", prior_beta)):
            number = _finite(value, label)
            if number <= 0.0:
                raise SPRTError(f"{label} must be positive")
        self._config = config
        self._prior_alpha = float(prior_alpha)
        self._prior_beta = float(prior_beta)
        self._state = CircuitState.CLOSED
        self._ratio = 0.0
        self._failures = 0
        self._successes = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def log_likelihood_ratio(self) -> float:
        return self._ratio

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def success_count(self) -> int:
        return self._successes

    def _decision(self, tripped: bool, recovered: bool) -> CircuitDecision:
        total = self._prior_alpha + self._prior_beta + self._failures + self._successes
        posterior = (self._prior_alpha + self._failures) / total
        return CircuitDecision(
            state=self._state,
            allow_request=self._state is not CircuitState.OPEN,
            log_likelihood_ratio=self._ratio,
            posterior_failure_mean=posterior,
            tripped=tripped,
            recovered=recovered,
        )

    def record(self, success: bool) -> CircuitDecision:
        if not isinstance(success, bool):
            raise TypeError("success must be a bool")
        if self._state is CircuitState.OPEN:
            return self._decision(tripped=False, recovered=False)
        config = self._config
        if success:
            self._successes += 1
            self._ratio += math.log((1.0 - config.p1) / (1.0 - config.p0))
        else:
            self._failures += 1
            self._ratio += math.log(config.p1 / config.p0)
        tripped = False
        recovered = False
        if self._ratio >= config.upper_threshold:
            self._state = CircuitState.OPEN
            tripped = True
        elif self._state is CircuitState.HALF_OPEN and self._ratio <= config.lower_threshold:
            self._state = CircuitState.CLOSED
            recovered = True
        return self._decision(tripped=tripped, recovered=recovered)

    def enter_half_open(self) -> CircuitDecision:
        if self._state is not CircuitState.OPEN:
            raise SPRTError("half-open requires an OPEN circuit")
        self._state = CircuitState.HALF_OPEN
        self._ratio = 0.0
        return self._decision(tripped=False, recovered=False)

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._ratio = 0.0
        self._failures = 0
        self._successes = 0
