"""Deterministic-router tests.

The router must be *pure*: identical inputs always produce identical
decisions, and it must never grant authority or choose a more privileged
actor (it only chooses a reasoning level).  These tests pin the ordering,
the ceiling, the high-risk/budget guards and the honest "blocked" answer.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.cognition import (
    CognitionLevel,
    DeterministicRouter,
    IntentClass,
    RoutingRequest,
)


def _route(**kwargs: object):
    return DeterministicRouter().route(RoutingRequest(**kwargs))  # type: ignore[arg-type]


def test_recipe_wins_over_everything() -> None:
    decision = _route(
        intent_class=IntentClass.OPEN_ENDED,
        recipe_available=True,
        deterministic_available=True,
        local_model_available=True,
        cloud_model_available=True,
    )
    assert decision.level is CognitionLevel.L0_RECIPE
    assert decision.reason_code == "recipe_available"


def test_deterministic_wins_over_models() -> None:
    decision = _route(
        intent_class=IntentClass.PARAMETRIC,
        deterministic_available=True,
        local_model_available=True,
        cloud_model_available=True,
    )
    assert decision.level is CognitionLevel.L1_DETERMINISTIC


def test_open_ended_with_no_model_asks_a_human() -> None:
    decision = _route(intent_class=IntentClass.OPEN_ENDED)
    assert decision.level is CognitionLevel.L4_HUMAN
    assert decision.reason_code == "open_ended_no_model"
    assert decision.blocked is False


def test_open_ended_prefers_local_model_over_cloud() -> None:
    decision = _route(
        intent_class=IntentClass.OPEN_ENDED,
        local_model_available=True,
        cloud_model_available=True,
    )
    assert decision.level is CognitionLevel.L2_LOCAL_MODEL


def test_open_ended_uses_cloud_when_no_local_model() -> None:
    decision = _route(intent_class=IntentClass.OPEN_ENDED, cloud_model_available=True)
    assert decision.level is CognitionLevel.L3_CLOUD_MODEL


def test_high_risk_never_reaches_a_model() -> None:
    decision = _route(
        intent_class=IntentClass.PARAMETRIC,
        high_risk=True,
        local_model_available=True,
        cloud_model_available=True,
    )
    assert decision.level not in (CognitionLevel.L2_LOCAL_MODEL, CognitionLevel.L3_CLOUD_MODEL)


def test_budget_exhaustion_forbids_models() -> None:
    decision = _route(
        intent_class=IntentClass.PARAMETRIC,
        remaining_budget=0.0,
        local_model_available=True,
        cloud_model_available=True,
    )
    assert decision.level not in (CognitionLevel.L2_LOCAL_MODEL, CognitionLevel.L3_CLOUD_MODEL)


def test_ceiling_never_escalated() -> None:
    decision = _route(
        intent_class=IntentClass.PARAMETRIC,
        max_level=CognitionLevel.L1_DETERMINISTIC,
        cloud_model_available=True,
    )
    assert decision.level is not CognitionLevel.L3_CLOUD_MODEL
    assert decision.level is CognitionLevel.L4_HUMAN


def test_blocked_when_no_path_and_no_human() -> None:
    decision = _route(intent_class=IntentClass.OPEN_ENDED, human_available=False)
    assert decision.blocked is True
    assert decision.reason_code == "no_eligible_path"


def test_router_is_pure_and_repeatable() -> None:
    request = RoutingRequest(
        intent_class=IntentClass.PARAMETRIC,
        deterministic_available=True,
        local_model_available=True,
    )
    router = DeterministicRouter()
    first = router.route(request)
    for _ in range(50):
        assert router.route(request) == first


def test_router_decision_carries_a_reason_code_always() -> None:
    for intent in IntentClass:
        decision = _route(intent_class=intent)
        assert decision.reason_code


def test_router_never_returns_a_more_privileged_level_than_ceiling() -> None:
    # Exhaustive over a small space: the chosen automated level is always <= ceiling.
    rank = {
        CognitionLevel.L0_RECIPE: 0,
        CognitionLevel.L1_DETERMINISTIC: 1,
        CognitionLevel.L2_LOCAL_MODEL: 2,
        CognitionLevel.L3_CLOUD_MODEL: 3,
        CognitionLevel.L4_HUMAN: 4,
    }
    router = DeterministicRouter()
    for ceiling in CognitionLevel:
        for intent in IntentClass:
            for recipe in (False, True):
                for det in (False, True):
                    for local in (False, True):
                        for cloud in (False, True):
                            decision = router.route(
                                RoutingRequest(
                                    intent_class=intent,
                                    recipe_available=recipe,
                                    deterministic_available=det,
                                    local_model_available=local,
                                    cloud_model_available=cloud,
                                    max_level=ceiling,
                                )
                            )
                            if decision.level is not CognitionLevel.L4_HUMAN:
                                assert rank[decision.level] <= rank[ceiling]
