"""Multi-window multi-burn-rate SLO calculus.

Each window reports the exact error rate, burn rate and remaining error-budget
fraction.  State precedence is fail-closed: any exhausted budget freezes the
release, then critical page burn, then warning burn, then healthy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "BurnWindowResult",
    "SLOBurnEvaluator",
    "SLOConfigError",
    "SLOReport",
    "SLOState",
    "SLOWindow",
    "burn_rate",
    "error_budget_remaining",
]

_TOLERANCE = 1e-12


class SLOConfigError(ValueError):
    """Raised when SLO configuration or observations are invalid."""


@dataclass(frozen=True, slots=True)
class SLOWindow:
    label: str
    duration_seconds: float
    warning_burn: float = 2.0
    critical_burn: float = 6.0

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label:
            raise SLOConfigError("label must be a non-empty string")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0.0:
            raise SLOConfigError("duration_seconds must be finite and positive")
        if not math.isfinite(self.warning_burn) or self.warning_burn <= 0.0:
            raise SLOConfigError("warning_burn must be finite and positive")
        if not math.isfinite(self.critical_burn) or self.critical_burn <= 0.0:
            raise SLOConfigError("critical_burn must be finite and positive")
        if self.critical_burn < self.warning_burn:
            raise SLOConfigError("critical_burn must not be below warning_burn")


class SLOState(Enum):
    HEALTHY = "HEALTHY"
    WARNING_BURN = "WARNING_BURN"
    CRITICAL_PAGE_BURN = "CRITICAL_PAGE_BURN"
    BUDGET_EXHAUSTED_FREEZE = "BUDGET_EXHAUSTED_FREEZE"


def burn_rate(failures: int | float, total: int | float, target: float) -> float:
    """Error-budget burn rate ``(failures/total) / (1 - target)``."""

    if not math.isfinite(target) or not 0.0 < target < 1.0:
        raise SLOConfigError("target must lie inside (0, 1)")
    if not math.isfinite(float(total)) or total <= 0:
        raise SLOConfigError("total must be positive")
    if not math.isfinite(float(failures)) or failures < 0 or failures > total:
        raise SLOConfigError("failures must lie inside [0, total]")
    return (failures / total) / (1.0 - target)


def error_budget_remaining(failures: int | float, total: int | float, target: float) -> float:
    """Fraction of the allowed error budget left, clamped to ``[0, 1]``."""

    if not math.isfinite(target) or not 0.0 < target < 1.0:
        raise SLOConfigError("target must lie inside (0, 1)")
    if not math.isfinite(float(total)) or total <= 0:
        raise SLOConfigError("total must be positive")
    if not math.isfinite(float(failures)) or failures < 0 or failures > total:
        raise SLOConfigError("failures must lie inside [0, total]")
    allowed = (1.0 - target) * total
    if allowed <= 0.0:  # pragma: no cover - target < 1 guarantees positive allowed
        return 0.0
    return max(0.0, min(1.0, 1.0 - failures / allowed))


@dataclass(frozen=True, slots=True)
class BurnWindowResult:
    label: str
    successes: int
    failures: int
    total: int
    error_rate: float
    burn_rate: float
    remaining_budget_fraction: float

    def __post_init__(self) -> None:
        for name in ("successes", "failures", "total"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SLOConfigError(f"{name} must be a non-negative int")
        if self.successes + self.failures != self.total:
            raise SLOConfigError("successes + failures must equal total")
        for name in ("error_rate", "burn_rate", "remaining_budget_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise SLOConfigError(f"{name} must be finite and non-negative")
        if self.error_rate > 1.0 or self.remaining_budget_fraction > 1.0:
            raise SLOConfigError("rates must not exceed 1")


@dataclass(frozen=True, slots=True)
class SLOReport:
    state: SLOState
    windows: tuple[BurnWindowResult, ...]
    overall_remaining: float
    target: float

    def __post_init__(self) -> None:
        if not isinstance(self.state, SLOState):
            raise SLOConfigError("state must be an SLOState")
        if any(not isinstance(window, BurnWindowResult) for window in self.windows):
            raise TypeError("windows must contain BurnWindowResult values")
        if not self.windows:
            raise SLOConfigError("at least one window is required")
        if not math.isfinite(self.overall_remaining) or not 0.0 <= self.overall_remaining <= 1.0:
            raise SLOConfigError("overall_remaining must lie inside [0, 1]")
        if not math.isfinite(self.target) or not 0.0 < self.target < 1.0:
            raise SLOConfigError("target must lie inside (0, 1)")


class SLOBurnEvaluator:
    """Evaluate counters for every configured window and pick the worst state."""

    def __init__(self, target: float, windows: Sequence[SLOWindow]) -> None:
        if not math.isfinite(target) or not 0.0 < target < 1.0:
            raise SLOConfigError("target must lie inside (0, 1)")
        if not windows:
            raise SLOConfigError("at least one window is required")
        if len({window.label for window in windows}) != len(windows):
            raise SLOConfigError("window labels must be unique")
        self._target = float(target)
        self._windows = tuple(windows)

    @property
    def target(self) -> float:
        return self._target

    @property
    def windows(self) -> tuple[SLOWindow, ...]:
        return self._windows

    def evaluate(self, counts: Mapping[str, tuple[int, int]]) -> SLOReport:
        measurements: list[tuple[SLOWindow, int, int, int, float, float]] = []
        for window in self._windows:
            if window.label not in counts:
                raise SLOConfigError(f"missing counts for window {window.label!r}")
            pair = counts[window.label]
            if len(pair) != 2:
                raise SLOConfigError("counts must be (successes, total)")
            successes, total = pair
            for name, value in (("successes", successes), ("total", total)):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise SLOConfigError(f"{name} must be a non-negative int")
            if successes > total:
                raise SLOConfigError("successes must not exceed total")
            failures = total - successes
            if total == 0:
                rate = 0.0
                burn = 0.0
            else:
                rate = failures / total
                burn = rate / (1.0 - self._target)
            measurements.append((window, successes, failures, total, rate, burn))
        # The broadest window supplies the period denominator; the worst observed
        # failure count is compared against it so short spikes still consume budget.
        global_total = max((item[3] for item in measurements), default=0)
        global_failures = max((item[2] for item in measurements), default=0)
        allowed = (1.0 - self._target) * global_total
        results: list[BurnWindowResult] = []
        for window, successes, failures, total, rate, burn in measurements:
            if allowed <= 0.0:
                remaining = 1.0
            else:
                remaining = max(0.0, min(1.0, 1.0 - failures / allowed))
            results.append(
                BurnWindowResult(
                    label=window.label,
                    successes=successes,
                    failures=failures,
                    total=total,
                    error_rate=rate,
                    burn_rate=burn,
                    remaining_budget_fraction=remaining,
                )
            )
        exhausted = global_total > 0 and allowed > 0.0 and global_failures >= allowed - _TOLERANCE
        critical = any(
            result.burn_rate >= window.critical_burn
            for window, result in zip(self._windows, results, strict=True)
        )
        warning = any(
            result.burn_rate >= window.warning_burn
            for window, result in zip(self._windows, results, strict=True)
        )
        if exhausted:
            state = SLOState.BUDGET_EXHAUSTED_FREEZE
        elif critical:
            state = SLOState.CRITICAL_PAGE_BURN
        elif warning:
            state = SLOState.WARNING_BURN
        else:
            state = SLOState.HEALTHY
        overall = min(result.remaining_budget_fraction for result in results)

        return SLOReport(
            state=state,
            windows=tuple(results),
            overall_remaining=overall,
            target=self._target,
        )
