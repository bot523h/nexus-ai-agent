"""Deterministic router — choose *how* to answer, never *what is allowed*.

The router answers one question: given an intent class and the resources the
caller already has, which reasoning level should be tried?  It is a pure
function of its inputs — no model call, no randomness, no learning, no clock
read.  Two identical requests always yield an identical decision.

Levels (cheapest-first):

* ``L0_RECIPE``       — a validated, versioned recipe exists (future work).
* ``L1_DETERMINISTIC``— a deterministic rule/capability path can answer.
* ``L2_LOCAL_MODEL``  — a local model provider is configured.
* ``L3_CLOUD_MODEL``  — a strong cloud provider is configured.
* ``L4_HUMAN``        — ask the human to clarify.

Hard rules (each is a test):

* the router **never** grants authority and **never** selects a *more*
  privileged actor — it only picks a reasoning level;
* the router **never** escalates past the caller's configured ceiling;
* a request that truly needs cognition (open-ended) and has no provider
  configured routes to ``L4_HUMAN``, not to a fabricated answer;
* risk or budget exhaustion can only *lower* the level, never raise it;
* the decision always carries a stable ``reason_code``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CognitionLevel(str, Enum):
    """Ordered, cheapest-first reasoning levels."""

    L0_RECIPE = "L0_recipe"
    L1_DETERMINISTIC = "L1_deterministic"
    L2_LOCAL_MODEL = "L2_local_model"
    L3_CLOUD_MODEL = "L3_cloud_model"
    L4_HUMAN = "L4_human"


#: Numeric rank so escalation can be compared without relying on enum order.
_LEVEL_RANK: dict[CognitionLevel, int] = {
    CognitionLevel.L0_RECIPE: 0,
    CognitionLevel.L1_DETERMINISTIC: 1,
    CognitionLevel.L2_LOCAL_MODEL: 2,
    CognitionLevel.L3_CLOUD_MODEL: 3,
    CognitionLevel.L4_HUMAN: 4,
}


class IntentClass(str, Enum):
    """How much semantic depth an intent needs."""

    KNOWN_OPERATION = "known_operation"  # maps to a registered capability
    PARAMETRIC = "parametric"  # known shape, needs slot filling
    OPEN_ENDED = "open_ended"  # genuinely ambiguous / creative


class RoutingRequest(BaseModel):
    """The router's entire input — all caller-owned, all data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_class: IntentClass
    #: Whether a deterministic rule/capability can answer this intent.
    deterministic_available: bool = False
    #: Whether a validated recipe exists (future; default false today).
    recipe_available: bool = False
    #: Whether a *local* model provider is configured by the composition root.
    local_model_available: bool = False
    #: Whether a *cloud* model provider is configured.
    cloud_model_available: bool = False
    #: Caller's ceiling: the router may never route above this.
    max_level: CognitionLevel = CognitionLevel.L3_CLOUD_MODEL
    #: Whether the caller can ask a human for clarification.
    human_available: bool = True
    #: A risk flag (e.g. security-sensitive): forces the cheapest safe level.
    high_risk: bool = False
    #: Remaining budget in the caller's own units; 0 means exhausted.
    remaining_budget: float = Field(default=1.0, ge=0.0)


class RoutingDecision(BaseModel):
    """A deterministic, observable routing decision.

    ``blocked`` is the honest terminal answer when nothing within the
    caller's ceiling can answer and no human is available: the caller must
    stop, not guess.  ``blocked`` is never a model level.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: CognitionLevel
    reason_code: str = Field(min_length=1, max_length=64)
    explanation: str = Field(default="", max_length=500)
    blocked: bool = False


def _rank(level: CognitionLevel) -> int:
    return _LEVEL_RANK[level]


class DeterministicRouter:
    """Pure, deterministic level selection.  No model, no state, no clock."""

    def route(self, request: RoutingRequest) -> RoutingDecision:
        # The ceiling bounds *automated* reasoning (L0..L3).  L4 (ask a human)
        # is the safest possible answer and is never blocked by the ceiling.
        ceiling = _rank(request.max_level)
        budget_exhausted = request.remaining_budget <= 0.0
        # A high-risk intent must never reach a model; budget exhaustion also
        # forbids spending on a model.  Both only *lower* what is eligible.
        model_forbidden = request.high_risk or budget_exhausted
        if budget_exhausted:
            ceiling = min(ceiling, _rank(CognitionLevel.L1_DETERMINISTIC))

        def allowed(level: CognitionLevel) -> bool:
            if level is CognitionLevel.L4_HUMAN:
                return request.human_available
            if _rank(level) > ceiling:
                return False
            if model_forbidden and level in (
                CognitionLevel.L2_LOCAL_MODEL,
                CognitionLevel.L3_CLOUD_MODEL,
            ):
                return False
            return True

        def human_or_blocked(reason: str, explanation: str) -> RoutingDecision:
            if request.human_available:
                return RoutingDecision(
                    level=CognitionLevel.L4_HUMAN,
                    reason_code=reason,
                    explanation=explanation,
                )
            return RoutingDecision(
                level=CognitionLevel.L4_HUMAN,
                reason_code="no_eligible_path",
                explanation="no reasoning level is available and human clarification is disabled",
                blocked=True,
            )

        # L0 — a validated recipe is always the cheapest correct answer.
        if request.recipe_available and allowed(CognitionLevel.L0_RECIPE):
            return RoutingDecision(
                level=CognitionLevel.L0_RECIPE,
                reason_code="recipe_available",
                explanation="a validated recipe covers this intent",
            )

        # L1 — deterministic rules/capability path.
        if request.deterministic_available and allowed(CognitionLevel.L1_DETERMINISTIC):
            return RoutingDecision(
                level=CognitionLevel.L1_DETERMINISTIC,
                reason_code="deterministic_available",
                explanation="a deterministic rule/capability path can answer",
            )

        # Open-ended intents need a model; without an eligible one, ask the human.
        if request.intent_class is IntentClass.OPEN_ENDED:
            if request.local_model_available and allowed(CognitionLevel.L2_LOCAL_MODEL):
                return RoutingDecision(
                    level=CognitionLevel.L2_LOCAL_MODEL,
                    reason_code="open_ended_local_model",
                    explanation="open-ended intent answered by the configured local model",
                )
            if request.cloud_model_available and allowed(CognitionLevel.L3_CLOUD_MODEL):
                return RoutingDecision(
                    level=CognitionLevel.L3_CLOUD_MODEL,
                    reason_code="open_ended_cloud_model",
                    explanation="open-ended intent answered by the configured cloud model",
                )
            return human_or_blocked(
                "open_ended_no_model",
                "open-ended intent with no eligible model provider; clarify with a human",
            )

        # Known/parametric intents: prefer the cheapest eligible provider.
        if request.local_model_available and allowed(CognitionLevel.L2_LOCAL_MODEL):
            return RoutingDecision(
                level=CognitionLevel.L2_LOCAL_MODEL,
                reason_code="local_model_available",
                explanation="no deterministic path, local model configured",
            )
        if request.cloud_model_available and allowed(CognitionLevel.L3_CLOUD_MODEL):
            return RoutingDecision(
                level=CognitionLevel.L3_CLOUD_MODEL,
                reason_code="cloud_model_available",
                explanation="no deterministic path, cloud model configured",
            )
        return human_or_blocked(
            "no_provider_human",
            "no deterministic path and no eligible model provider; clarify with a human",
        )


__all__ = [
    "CognitionLevel",
    "DeterministicRouter",
    "IntentClass",
    "RoutingDecision",
    "RoutingRequest",
]
