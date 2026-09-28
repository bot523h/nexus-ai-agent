"""W2 — centralised policy: budgets, route selection, privacy, refusals (LAW 4).

``plan()`` is a *pure* function: request + policy + a world snapshot in, a plan
out. Nothing in it reads a clock, touches a socket or mutates state, which is why
every decision the gateway makes can be asserted here without an event loop.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.llm.errors import LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    ContentPart,
    LLMOperation,
    LLMRequest,
    Modality,
)
from nexus_ai_agent.llm.gateway.policy import (
    ConcurrencyPolicy,
    FallbackPolicy,
    GatewayPolicy,
    PrivacyPolicy,
    ProviderRateLimit,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    RouteRule,
    RouteSnapshot,
    TimeoutBudget,
    WorldSnapshot,
    default_policy,
    plan,
)


def _caller(**kwargs: object) -> Caller:
    base: dict[str, object] = {"category": CallerCategory.AGENT, "name": "test.caller"}
    base.update(kwargs)
    return Caller(**base)  # type: ignore[arg-type]


def _world() -> WorldSnapshot:
    return WorldSnapshot()


GEMINI = Route(provider="gemini", model="gemini-2.0-flash", rank=10)
OLLAMA = Route(provider="ollama", model="llama3.2", rank=5)
FREE = Route(provider="openrouter", model="llama-3.3-70b:free", rank=90)
DEGRADED = Route(provider="local-degraded", model="fake", rank=900, degraded=True)
VISION = Route(
    provider="gemini",
    model="gemini-2.0-flash-vision",
    rank=12,
    modalities=frozenset({Modality.TEXT, Modality.IMAGE}),
)
EMBED = Route(
    provider="gemini",
    model="text-embedding-004",
    rank=11,
    operations=frozenset({LLMOperation.EMBEDDINGS}),
)


# ═══════════════════════════════════════════════════════════════════════════
# TimeoutBudget
# ═══════════════════════════════════════════════════════════════════════════


def test_default_budget_satisfies_its_own_invariants() -> None:
    budget = TimeoutBudget()
    assert budget.connect_seconds + budget.read_seconds <= budget.per_attempt_seconds
    assert budget.per_attempt_seconds <= budget.total_seconds
    assert budget.queue_wait_seconds <= budget.total_seconds
    assert budget.max_backoff_seconds <= budget.total_seconds
    assert budget.validate() is budget


def test_transport_must_time_out_before_the_gateway_cap() -> None:
    """Otherwise a slow provider surfaces as a gateway cancellation, and the
    failure cannot be classified as ``UPSTREAM_TIMEOUT`` — it becomes invisible
    to the retry policy."""

    with pytest.raises(ValueError, match="connect_seconds"):
        TimeoutBudget(connect_seconds=30.0, read_seconds=45.0, per_attempt_seconds=50.0).validate()


def test_one_attempt_can_never_outlive_the_whole_request() -> None:
    with pytest.raises(ValueError, match="per_attempt_seconds"):
        TimeoutBudget(per_attempt_seconds=90.0, total_seconds=60.0).validate()


def test_queue_wait_cannot_consume_more_than_everything() -> None:
    with pytest.raises(ValueError, match="queue_wait_seconds"):
        TimeoutBudget(queue_wait_seconds=120.0, total_seconds=60.0).validate()


def test_backoff_is_bounded_by_the_total_budget() -> None:
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        TimeoutBudget(max_backoff_seconds=120.0, total_seconds=60.0).validate()


@pytest.mark.parametrize(
    "field", ["total_seconds", "connect_seconds", "read_seconds", "queue_wait_seconds"]
)
def test_a_non_positive_budget_component_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        TimeoutBudget(**{field: 0.0}).validate()


def test_a_caller_deadline_tighter_than_the_transport_floor_does_not_crash_policy() -> None:
    """Regression: ``with_total`` used to raise ``ValueError`` here.

    A caller asking for a 50 ms budget is not a configuration error — it is a
    request that cannot be served. Policy evaluation must hand back a *valid*
    (if tiny) budget so the engine can answer with a typed DEADLINE_EXCEEDED.
    Leaking a ValueError out of ``plan()`` would surface to the caller as an
    infrastructure crash instead of an LLM failure.
    """

    for requested in (0.05, 0.001, 0.0, -5.0):
        budget = TimeoutBudget().with_total(requested)
        assert budget.connect_seconds + budget.read_seconds <= budget.per_attempt_seconds
        assert budget.per_attempt_seconds <= budget.total_seconds
        assert budget.queue_wait_seconds <= budget.total_seconds
        assert budget.max_backoff_seconds <= budget.total_seconds
        assert budget.connect_seconds > 0.0
        assert budget.read_seconds > 0.0
        assert budget.validate() is budget


def test_a_caller_can_shorten_but_never_lengthen_the_policy_budget() -> None:
    """The caller's deadline is a ceiling on its own request, not a policy override."""

    budget = TimeoutBudget().with_total(500.0)
    assert budget.total_seconds == TimeoutBudget().total_seconds
    assert budget.per_attempt_seconds == TimeoutBudget().per_attempt_seconds


def test_a_shorter_deadline_scales_the_inner_layers_down() -> None:
    budget = TimeoutBudget().with_total(10.0)
    assert budget.total_seconds == 10.0
    assert budget.per_attempt_seconds == 10.0
    assert budget.connect_seconds < TimeoutBudget().connect_seconds + 1
    assert budget.max_backoff_seconds <= 10.0


# ═══════════════════════════════════════════════════════════════════════════
# Route selection
# ═══════════════════════════════════════════════════════════════════════════


def test_the_lowest_rank_capable_route_wins_by_default() -> None:
    policy = default_policy(routes=[GEMINI, OLLAMA])
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].key == "ollama/llama3.2"


def test_an_explicit_provider_pin_selects_that_route() -> None:
    policy = default_policy(routes=[GEMINI, OLLAMA])
    request = LLMRequest(caller=_caller(), prompt="hi", provider="gemini")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].key == "gemini/gemini-2.0-flash"


def test_an_explicit_model_pin_selects_that_route() -> None:
    policy = default_policy(routes=[GEMINI, EMBED])
    request = LLMRequest(
        caller=_caller(),
        prompt="hi",
        model="text-embedding-004",
        operation=LLMOperation.EMBEDDINGS,
        purpose="embeddings",
    )
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].model == "text-embedding-004"


def test_a_pin_to_a_route_that_cannot_serve_the_request_is_refused_not_substituted() -> None:
    """The silent-substitution bug this pins.

    Asking ``text-embedding-004`` for a chat completion used to fall through to
    the sibling Gemini route and answer from a *different model* than the caller
    named. A pin that cannot be honoured must produce a typed refusal naming the
    constraint, so the caller learns its request was impossible instead of
    receiving an answer it did not ask for.
    """

    policy = default_policy(routes=[GEMINI, EMBED])
    request = LLMRequest(caller=_caller(), prompt="hi", model="text-embedding-004")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes == ()
    assert result.refusal is not None
    assert result.refusal.kind is LLMErrorKind.POLICY_REFUSAL
    assert "cannot serve" in str(result.refusal)


def test_a_routing_rule_beats_rank_order() -> None:
    """Purpose-based routing is how a summarizer gets a long-context model
    without every caller hard-coding a provider."""

    rule = RouteRule(provider="gemini", model="gemini-2.0-flash", purpose="summarize")
    policy = default_policy(routes=[OLLAMA, GEMINI], rules=[rule])
    request = LLMRequest(caller=_caller(), prompt="hi", purpose="summarize")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].key == "gemini/gemini-2.0-flash"
    assert rule.matches(request) is True


def test_a_rule_for_another_purpose_does_not_match() -> None:
    rule = RouteRule(provider="gemini", model="gemini-2.0-flash", purpose="summarize")
    request = LLMRequest(caller=_caller(), prompt="hi", purpose="chat")
    assert rule.matches(request) is False


def test_a_degraded_route_is_never_the_first_choice() -> None:
    """LAW 8/11: a locally faked answer must not outrank a real provider."""

    policy = default_policy(routes=[DEGRADED, GEMINI])
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].key == "gemini/gemini-2.0-flash"


def test_a_degraded_route_is_used_when_it_is_the_only_one() -> None:
    policy = default_policy(routes=[DEGRADED])
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].degraded is True


def test_fallback_candidates_follow_in_rank_order_up_to_max_hops() -> None:
    policy = default_policy(
        routes=[GEMINI, OLLAMA, FREE],
        fallback=FallbackPolicy(enabled=True, max_hops=2),
    )
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert [route.key for route in result.routes] == [
        "ollama/llama3.2",
        "gemini/gemini-2.0-flash",
        "openrouter/llama-3.3-70b:free",
    ]


def test_max_hops_of_zero_means_no_alternatives_are_planned() -> None:
    policy = default_policy(routes=[GEMINI, OLLAMA], fallback=FallbackPolicy(max_hops=0))
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert len(result.routes) == 1


def test_a_caller_veto_removes_every_alternative() -> None:
    """``allow_fallback=False`` makes a pin a hard constraint.

    A summarizer or a memory extractor must not silently receive a different
    provider's (or a locally faked) answer: the caller asked for *this* model,
    and a fabricated substitute written into a database is worse than a typed
    failure the caller can act on.
    """

    policy = default_policy(
        routes=[GEMINI, DEGRADED],
        fallback=FallbackPolicy(enabled=True, max_hops=2, allow_degraded_routes=True),
    )
    pinned = LLMRequest(caller=_caller(), prompt="hi", provider="gemini", allow_fallback=False)
    result = plan(pinned, policy, _world(), now=0.0)
    assert [route.key for route in result.routes] == ["gemini/gemini-2.0-flash"]

    open_ = LLMRequest(caller=_caller(), prompt="hi", provider="gemini")
    assert len(plan(open_, policy, _world(), now=0.0).routes) == 2


def test_degraded_routes_are_excluded_from_fallback_unless_policy_allows_them() -> None:
    strict = default_policy(
        routes=[GEMINI, DEGRADED],
        fallback=FallbackPolicy(enabled=True, max_hops=2, allow_degraded_routes=False),
    )
    request = LLMRequest(caller=_caller(), prompt="hi")
    assert [route.key for route in plan(request, strict, _world(), now=0.0).routes] == [
        "gemini/gemini-2.0-flash"
    ]

    permissive = default_policy(
        routes=[GEMINI, DEGRADED],
        fallback=FallbackPolicy(enabled=True, max_hops=2, allow_degraded_routes=True),
    )
    assert len(plan(request, permissive, _world(), now=0.0).routes) == 2


# ── capability matching ─────────────────────────────────────────────────


def test_an_embeddings_request_is_not_planned_onto_a_chat_route() -> None:
    policy = default_policy(routes=[GEMINI, EMBED])
    request = LLMRequest(
        caller=_caller(), prompt="hi", operation=LLMOperation.EMBEDDINGS, purpose="embeddings"
    )
    result = plan(request, policy, _world(), now=0.0)
    assert [route.key for route in result.routes] == ["gemini/text-embedding-004"]


def test_an_image_request_is_refused_by_a_text_only_fleet() -> None:
    """Refused *before* any bytes are sent: a typed UNSUPPORTED_CAPABILITY beats
    a provider 400 that costs quota and says nothing actionable."""

    policy = default_policy(routes=[GEMINI, OLLAMA])
    request = LLMRequest(
        caller=_caller(),
        prompt="describe",
        parts=(ContentPart(mime_type="image/png", data=b"1234"),),
    )
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes == ()
    assert result.refusal is not None
    assert result.refusal.kind is LLMErrorKind.POLICY_REFUSAL
    assert "image" in str(result.refusal)


def test_a_vision_route_serves_an_image_request() -> None:
    policy = default_policy(routes=[GEMINI, VISION])
    request = LLMRequest(
        caller=_caller(),
        prompt="describe",
        parts=(ContentPart(mime_type="image/png", data=b"1234"),),
    )
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes[0].key == VISION.key
    assert Modality.IMAGE in result.routes[0].modalities


def test_a_text_route_is_not_a_fallback_for_an_image_request() -> None:
    """Dropping the image and answering the text would be a silent lie."""

    policy = default_policy(routes=[VISION, GEMINI])
    request = LLMRequest(
        caller=_caller(),
        prompt="describe",
        parts=(ContentPart(mime_type="image/png", data=b"1234"),),
    )
    keys = [route.key for route in plan(request, policy, _world(), now=0.0).routes]
    assert keys == [VISION.key]


# ── privacy policy ──────────────────────────────────────────────────────


def test_strict_privacy_removes_free_endpoints_that_may_train_on_prompts() -> None:
    """``docs/architecture/LLM_PROVIDERS.md``: strict privacy excludes ``:free``."""

    strict = default_policy(routes=[FREE, GEMINI], privacy=PrivacyPolicy(strict=True))
    request = LLMRequest(caller=_caller(), prompt="hi")
    keys = [route.key for route in plan(request, strict, _world(), now=0.0).routes]
    assert "openrouter/llama-3.3-70b:free" not in keys
    assert "gemini/gemini-2.0-flash" in keys


def test_strict_privacy_refuses_when_every_route_is_forbidden() -> None:
    strict = default_policy(routes=[FREE], privacy=PrivacyPolicy(strict=True))
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, strict, _world(), now=0.0)
    assert result.routes == ()
    assert result.refusal is not None
    assert "privacy" in str(result.refusal)


def test_non_strict_privacy_keeps_free_endpoints() -> None:
    policy = default_policy(routes=[FREE], privacy=PrivacyPolicy(strict=False))
    request = LLMRequest(caller=_caller(), prompt="hi")
    assert len(plan(request, policy, _world(), now=0.0).routes) == 1


def test_an_explicitly_forbidden_provider_is_removed_even_without_strict_mode() -> None:
    policy = default_policy(
        routes=[GEMINI, OLLAMA],
        privacy=PrivacyPolicy(strict=False, forbidden_providers=frozenset({"ollama"})),
    )
    request = LLMRequest(caller=_caller(), prompt="hi")
    keys = [route.key for route in plan(request, policy, _world(), now=0.0).routes]
    assert keys == ["gemini/gemini-2.0-flash"]


def test_privacy_forbids_is_a_pure_route_predicate() -> None:
    privacy = PrivacyPolicy(strict=True)
    assert privacy.forbids(FREE) is True
    assert privacy.forbids(GEMINI) is False


# ── refusals ────────────────────────────────────────────────────────────


def test_a_fleet_with_no_routes_refuses_with_an_actionable_reason() -> None:
    policy = default_policy(routes=[])
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert result.routes == ()
    assert result.refusal is not None
    assert result.refusal.kind is LLMErrorKind.POLICY_REFUSAL
    assert "no LLM route is registered" in str(result.refusal)


def test_a_pin_that_matches_nothing_says_what_is_registered() -> None:
    policy = default_policy(routes=[GEMINI])
    request = LLMRequest(caller=_caller(), prompt="hi", provider="anthropic")
    result = plan(request, policy, _world(), now=0.0)
    assert result.refusal is not None
    message = str(result.refusal)
    assert "anthropic" in message
    assert "gemini/gemini-2.0-flash" in message


def test_an_open_circuit_stays_in_the_plan_so_the_route_can_recover() -> None:
    """Filtering open circuits out of the plan would make recovery impossible.

    The engine owns the half-open probe: it asks the breaker per attempt and
    fails fast when it is open. If ``plan()`` removed the route instead, a
    tripped provider could never be probed again and the fleet would shrink
    permanently.
    """

    policy = default_policy(routes=[GEMINI])
    world = WorldSnapshot(routes={GEMINI.key: RouteSnapshot(circuit_open=True)})
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, world, now=0.0)
    assert [route.key for route in result.routes] == [GEMINI.key]
    assert result.refusal is None


def test_healthy_routes_are_ordered_before_cooling_ones() -> None:
    """Same rank, different health: the healthy route is tried first."""

    healthy = Route(provider="a", model="m", rank=10)
    cooling = Route(provider="b", model="m", rank=10)
    policy = default_policy(
        routes=[cooling, healthy],
        fallback=FallbackPolicy(enabled=True, max_hops=2),
    )
    world = WorldSnapshot(routes={cooling.key: RouteSnapshot(circuit_open=True)})
    request = LLMRequest(caller=_caller(), prompt="hi")
    keys = [route.key for route in plan(request, policy, world, now=0.0).routes]
    assert keys == [healthy.key, cooling.key]


# ── budget plumbing ─────────────────────────────────────────────────────


def test_the_plan_carries_the_policy_budget_when_no_deadline_is_given() -> None:
    policy = default_policy(routes=[GEMINI])
    request = LLMRequest(caller=_caller(), prompt="hi")
    result = plan(request, policy, _world(), now=0.0)
    assert result.budget.total_seconds == policy.timeout.total_seconds


def test_a_caller_deadline_tightens_the_planned_budget() -> None:
    """LAW: retry must not silently destroy the caller's overall timeout."""

    policy = default_policy(routes=[GEMINI])
    request = LLMRequest(caller=_caller(), prompt="hi", deadline_seconds=8.0)
    result = plan(request, policy, _world(), now=0.0)
    assert result.budget.total_seconds == 8.0
    assert result.budget.per_attempt_seconds <= 8.0
    assert result.budget.max_backoff_seconds <= 8.0


def test_the_plan_carries_the_idempotency_key_through() -> None:
    policy = default_policy(routes=[GEMINI])
    request = LLMRequest(caller=_caller(), prompt="hi", idempotency_key="abc123")
    assert plan(request, policy, _world(), now=0.0).idempotency_key == "abc123"


# ═══════════════════════════════════════════════════════════════════════════
# Policy construction and defaults
# ═══════════════════════════════════════════════════════════════════════════


def test_default_policy_is_valid_and_versioned() -> None:
    policy = default_policy(routes=[GEMINI])
    assert isinstance(policy, GatewayPolicy)
    assert policy.version == "1"
    assert policy.timeout.validate() is policy.timeout
    assert policy.retry.max_attempts == 3
    assert policy.fallback.enabled is True


def test_default_policy_overrides_one_layer_without_rebuilding_the_rest() -> None:
    policy = default_policy(routes=[GEMINI], retry=RetryPolicy(max_attempts=1))
    assert policy.retry.max_attempts == 1
    assert policy.timeout.total_seconds == TimeoutBudget().total_seconds
    assert policy.concurrency.max_inflight_global == ConcurrencyPolicy().max_inflight_global


def test_default_concurrency_is_bounded_and_rejects_on_saturation() -> None:
    """LAW 6: bounded everything. An unbounded queue is a memory leak with a
    latency tail, and shedding load is the honest response to saturation."""

    policy = ConcurrencyPolicy()
    assert policy.max_inflight_global >= 1
    assert policy.max_inflight_per_provider >= 1
    assert policy.max_queued >= 0
    assert policy.overload_behavior.value == "reject"


def test_rate_limit_policy_falls_back_to_the_global_axes() -> None:
    policy = RateLimitPolicy(
        requests_per_minute=60,
        per_provider={"gemini": ProviderRateLimit(requests_per_minute=15, requests_per_day=1500)},
    )
    assert policy.for_provider("gemini").requests_per_minute == 15
    assert policy.for_provider("gemini").requests_per_day == 1500
    assert policy.for_provider("ollama").requests_per_minute == 60


def test_policy_rejects_an_unknown_override_instead_of_ignoring_it() -> None:
    """A typo in a policy override must fail loudly, not silently weaken policy."""

    with pytest.raises(TypeError):
        default_policy(routes=[GEMINI], retries=RetryPolicy())


def test_context_window_gate_is_off_unless_a_route_declares_one() -> None:
    assert GEMINI.context_window_chars is None
    gated = Route(provider="gemini", model="m", context_window_chars=100)
    assert gated.context_window_chars == 100


def test_route_key_is_provider_slash_model() -> None:
    assert GEMINI.key == "gemini/gemini-2.0-flash"


def test_route_serves_only_matching_operation_and_modalities() -> None:
    text_request = LLMRequest(caller=_caller(), prompt="hi")
    image_request = LLMRequest(
        caller=_caller(), prompt="hi", parts=(ContentPart("image/png", b"x"),)
    )
    assert GEMINI.serves(text_request) is True
    assert GEMINI.serves(image_request) is False
    assert VISION.serves(image_request) is True
    assert EMBED.serves(text_request) is False
