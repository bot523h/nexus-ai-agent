"""Bounded creative director over a read-only causal memory.

The director never writes memory: it receives the capability proxy, measures
retention of supporting evidence, and greedily maximises expected causal
utility per unit cost under explicit action-count, cost and horizon budgets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from nexus_ai_agent.nagar.causal_memory.do_calculus import (
    CausalMemory,
    ReadOnlyCausalMemory,
    retention,
)

__all__ = [
    "BoundedDirector",
    "CreativeAction",
    "DirectorBudget",
    "DirectorPlan",
    "DirectorPlanningError",
]


class DirectorPlanningError(ValueError):
    """Raised when a plan request violates a budget or evidence invariant."""


@dataclass(frozen=True, slots=True)
class CreativeAction:
    action_id: str
    expected_utility: float
    cost: float
    evidence_id: str | None = None
    required_retention: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise DirectorPlanningError("action_id must be a non-empty string")
        if not math.isfinite(self.expected_utility) or self.expected_utility < 0.0:
            raise DirectorPlanningError("expected_utility must be finite and non-negative")
        if not math.isfinite(self.cost) or self.cost < 0.0:
            raise DirectorPlanningError("cost must be finite and non-negative")
        if self.evidence_id is not None and (
            not isinstance(self.evidence_id, str) or not self.evidence_id
        ):
            raise DirectorPlanningError("evidence_id must be a non-empty string or None")
        if not math.isfinite(self.required_retention) or not 0.0 <= self.required_retention <= 1.0:
            raise DirectorPlanningError("required_retention must lie inside [0, 1]")


@dataclass(frozen=True, slots=True)
class DirectorBudget:
    max_actions: int
    max_cost: float
    horizon_days: float

    def __post_init__(self) -> None:
        if isinstance(self.max_actions, bool) or not isinstance(self.max_actions, int):
            raise TypeError("max_actions must be an int")
        if self.max_actions <= 0:
            raise DirectorPlanningError("max_actions must be positive")
        if not math.isfinite(self.max_cost) or self.max_cost <= 0.0:
            raise DirectorPlanningError("max_cost must be finite and positive")
        if not math.isfinite(self.horizon_days) or self.horizon_days <= 0.0:
            raise DirectorPlanningError("horizon_days must be finite and positive")


@dataclass(frozen=True, slots=True)
class DirectorPlan:
    actions: tuple[CreativeAction, ...]
    skipped: tuple[str, ...]
    total_utility: float
    total_cost: float
    remaining_budget: float

    def __post_init__(self) -> None:
        for action in self.actions:
            if not isinstance(action, CreativeAction):
                raise TypeError("actions must contain CreativeAction values")
        for name in ("total_utility", "total_cost", "remaining_budget"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise DirectorPlanningError(f"{name} must be finite and non-negative")
        tolerance = 1e-9
        if (
            abs(sum(action.expected_utility for action in self.actions) - self.total_utility)
            > tolerance
        ):
            raise DirectorPlanningError("total_utility disagrees with the action sum")
        if abs(sum(action.cost for action in self.actions) - self.total_cost) > tolerance:
            raise DirectorPlanningError("total_cost disagrees with the action sum")


class BoundedDirector:
    """Deterministic utility-density planner with a hard budget."""

    def __init__(
        self,
        budget: DirectorBudget,
        *,
        stability: float = 1.0,
        alpha: float = 0.5,
        beta: float = 1.0,
    ) -> None:
        if not isinstance(budget, DirectorBudget):
            raise TypeError("budget must be a DirectorBudget")
        self._budget = budget
        self._stability = stability
        self._alpha = alpha
        self._beta = beta

    @property
    def budget(self) -> DirectorBudget:
        return self._budget

    def plan(
        self,
        memory: CausalMemory | ReadOnlyCausalMemory,
        actions: Sequence[CreativeAction],
        now_days: float,
    ) -> DirectorPlan:
        if isinstance(memory, CausalMemory):
            view = ReadOnlyCausalMemory(memory)
        elif isinstance(memory, ReadOnlyCausalMemory):
            view = memory
        else:
            raise TypeError("memory must be a CausalMemory or ReadOnlyCausalMemory")
        if not math.isfinite(now_days) or now_days < 0.0:
            raise DirectorPlanningError("now_days must be finite and non-negative")
        eligible: list[tuple[float, CreativeAction]] = []
        skipped: list[str] = []
        for action in actions:
            if not isinstance(action, CreativeAction):
                raise TypeError("actions must contain CreativeAction values")
            if action.cost > self._budget.max_cost:
                skipped.append(action.action_id)
                continue
            if action.evidence_id is not None:
                episode = view.get(action.evidence_id)
                if episode is None:
                    skipped.append(action.action_id)
                    continue
                elapsed = now_days - episode.observed_at_days
                if elapsed > self._budget.horizon_days:
                    skipped.append(action.action_id)
                    continue
                measured = retention(
                    episode,
                    now_days,
                    stability=self._stability,
                    alpha=self._alpha,
                    beta=self._beta,
                )
                if measured < action.required_retention:
                    skipped.append(action.action_id)
                    continue
            density = math.inf if action.cost == 0.0 else action.expected_utility / action.cost
            eligible.append((density, action))
        eligible.sort(key=lambda item: (-item[0], -item[1].expected_utility, item[1].action_id))
        selected: list[CreativeAction] = []
        total_cost = 0.0
        total_utility = 0.0
        for _density, action in eligible:
            if len(selected) >= self._budget.max_actions:
                skipped.append(action.action_id)
                continue
            if total_cost + action.cost > self._budget.max_cost + 1e-12:
                skipped.append(action.action_id)
                continue
            selected.append(action)
            total_cost += action.cost
            total_utility += action.expected_utility
        return DirectorPlan(
            actions=tuple(selected),
            skipped=tuple(skipped),
            total_utility=total_utility,
            total_cost=total_cost,
            remaining_budget=max(0.0, self._budget.max_cost - total_cost),
        )
