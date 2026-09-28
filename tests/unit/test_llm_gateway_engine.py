"""W2 — the gateway engine: retry, fallback, budget, breaker, quota, idempotency.

This is the behavioural heart of the authority. Every test here states a law and
then proves the engine keeps it:

* LAW 4 — policy is decided in exactly one place;
* LAW 5 — cancellation stops the chain immediately and leaves no orphan task;
* LAW 6 — in-flight work, the queue, the idempotency cache and the metrics are
  all bounded;
* LAW 7 — a retry has a reason, a bound, and respects the caller's deadline;
* LAW 8 — a fallback is policy-driven and always visible on the response;
* LAW 10 — every request produces one observation record;
* LAW 11 — usage is what the provider reported, or UNKNOWN.

Adapters here are fakes with injectable behaviour, so a test can name the exact
failure it wants on the exact attempt it wants.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    FALLBACK_ELIGIBLE_KINDS,
    AuthenticationError,
    ContentBlockedError,
    DeadlineExceededError,
    GatewayClosedError,
    GatewayInternalError,
    InvalidRequestError,
    LLMError,
    LLMErrorKind,
    MalformedResponseError,
    OverloadedError,
    PolicyRefusalError,
    QuotaExhaustedError,
    RateLimitedError,
    StructuredOutputInvalidError,
    TransientProviderError,
    UnsupportedCapabilityError,
)
from nexus_ai_agent.llm.gateway.adapters import AdapterResult
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    FinishReason,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    LLMResponse,
    Usage,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.engine import GatewayBuilder, LLMGateway
from nexus_ai_agent.llm.gateway.observability import CollectingSink
from nexus_ai_agent.llm.gateway.policy import (
    CircuitPolicy,
    ConcurrencyPolicy,
    FallbackPolicy,
    OverloadBehavior,
    PrivacyPolicy,
    ProviderRateLimit,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    TimeoutBudget,
    default_policy,
)

FAST_RETRY = RetryPolicy(max_attempts=3, base_delay_seconds=0.001, max_delay_seconds=0.005)
NO_RETRY = RetryPolicy(max_attempts=1)
CALLER = Caller(category=CallerCategory.AGENT, name="test.engine")
TEXT_OPS = frozenset({LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION})


class ScriptedAdapter:
    """An adapter that plays a script of outcomes, one per call.

    A script entry is either a string (answer), an ``Exception`` (raise it) or an
    :class:`AdapterResult`. Once the script is exhausted the last entry repeats,
    so "always fails" is ``[exc]``.
    """

    def __init__(
        self,
        name: str,
        script: list[Any],
        *,
        model: str = "m",
        operations: Any = TEXT_OPS,
        delay: float = 0.0,
        usage: Usage | None = None,
    ) -> None:
        self._name = name
        self._script = list(script)
        self._model = model
        self._operations = operations
        self._delay = delay
        self._usage = usage
        self.calls = 0
        self.seen_requests: list[LLMRequest] = []
        self.seen_attempts: list[int] = []
        self.cancelled_calls = 0
        self.closed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def operations(self) -> Any:
        return self._operations

    @property
    def modalities(self) -> Any:
        from nexus_ai_agent.llm.gateway.contract import Modality

        return frozenset({Modality.TEXT})

    async def execute(
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult:
        self.calls += 1
        self.seen_requests.append(request)
        self.seen_attempts.append(attempt)
        if self._delay:
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                self.cancelled_calls += 1
                raise
        outcome = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, AdapterResult):
            return outcome
        return AdapterResult(
            text=str(outcome),
            finish_reason=FinishReason.STOP,
            usage=self._usage if self._usage is not None else Usage(),
        )

    async def aclose(self) -> None:
        self.closed += 1


def _route(provider: str, model: str = "m", rank: int = 10, **kwargs: Any) -> Route:
    return Route(provider=provider, model=model, rank=rank, **kwargs)


class VirtualClock:
    """A clock that only moves when the gateway sleeps.

    Rate windows and circuit recovery are wall-clock phenomena; a virtual clock
    makes "waited for capacity" and "cooled down" testable in milliseconds.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _gateway(
    adapters: list[Any],
    *,
    routes: list[Route] | None = None,
    sink: CollectingSink | None = None,
    clock: Any = None,
    **policy_kwargs: Any,
) -> LLMGateway:
    resolved_routes = routes if routes is not None else [_route(a.name) for a in adapters]
    collector = sink if sink is not None else CollectingSink()
    retry = policy_kwargs.pop("retry", FAST_RETRY)
    policy = default_policy(routes=resolved_routes, retry=retry, **policy_kwargs)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    gateway = LLMGateway(
        policy=policy,
        sinks=[collector],
        rng=random.Random(1234),
        attach_default_sink=False,
        **kwargs,
    )
    for adapter in adapters:
        adapter_routes = [r for r in resolved_routes if r.provider == adapter.name]
        gateway.register(adapter, adapter_routes or None)
    gateway._test_sink = collector  # type: ignore[attr-defined]
    return gateway


def _virtual_sleep(gateway: LLMGateway, clock: VirtualClock) -> list[float]:
    """Replace the gateway's sleep so delays advance the virtual clock."""

    slept: list[float] = []

    async def sleep(delay: float, token: Any = None) -> float:
        slept.append(delay)
        clock.advance(delay)
        await asyncio.sleep(0)
        return delay

    gateway._sleep = sleep  # type: ignore[method-assign]
    return slept


def _sink(gateway: LLMGateway) -> CollectingSink:
    return gateway._test_sink  # type: ignore[attr-defined,no-any-return]


def _sink_records(gateway: LLMGateway) -> list[Any]:
    return list(gateway._sinks[0].records)  # type: ignore[attr-defined,no-any-return]


def _request(**kwargs: Any) -> LLMRequest:
    base: dict[str, Any] = {"caller": CALLER, "prompt": "hello"}
    base.update(kwargs)
    return LLMRequest(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The happy path
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_healthy_route_answers_on_the_first_attempt() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    response = await gateway.execute(_request())
    assert response.text == "answer"
    assert response.provider == "gemini"
    assert response.model == "m"
    assert response.policy is not None
    assert response.policy.attempts == 1
    assert response.policy.retries == 0
    assert response.degraded is False
    assert adapter.calls == 1


async def test_the_response_carries_identity_purpose_and_operation() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["answer"])])
    response = await gateway.execute(_request(purpose="summarize", operation=LLMOperation.CHAT))
    assert response.request_id
    assert response.purpose == "summarize"
    assert response.operation is LLMOperation.CHAT
    assert response.caller.label == "agent:test.engine"
    assert response.finish_reason is FinishReason.STOP


async def test_the_text_helper_returns_just_the_text() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["answer"])])
    assert await gateway.text("hello", caller=CALLER) == "answer"


async def test_the_convenience_generate_passes_every_field_through() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    response = await gateway.generate(
        "hello",
        caller=CALLER,
        system="be brief",
        purpose="chat",
        provider="gemini",
        priority=LLMPriority.OWNER,
        allow_fallback=False,
    )
    assert response.text == "answer"
    sent = adapter.seen_requests[0]
    assert sent.system == "be brief"
    assert sent.provider == "gemini"
    assert sent.priority is LLMPriority.OWNER
    assert sent.allow_fallback is False


async def test_provider_reported_usage_reaches_the_caller() -> None:
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=10, output_tokens=4, total_tokens=14)
    gateway = _gateway([ScriptedAdapter("gemini", ["answer"], usage=usage)])
    response = await gateway.execute(_request())
    assert response.usage.source is UsageSource.PROVIDER
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 4


async def test_a_free_tier_model_costs_exactly_zero_and_says_so() -> None:
    """LAW 11: a real price of zero is a fact; a missing price is not a zero."""

    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=500)
    gateway = _gateway(
        [ScriptedAdapter("gemini", ["answer"], usage=usage)],
        routes=[_route("gemini", model="gemini-2.0-flash")],
    )
    response = await gateway.execute(_request())
    assert response.usage.estimated_cost_usd == 0.0


async def test_an_unpriced_model_leaves_the_cost_unknown() -> None:
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=500)
    gateway = _gateway(
        [ScriptedAdapter("gemini", ["answer"], usage=usage)],
        routes=[_route("gemini", model="some-unpriced-model")],
    )
    response = await gateway.execute(_request())
    assert response.usage.estimated_cost_usd is None


async def test_usage_the_provider_did_not_report_is_never_invented() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["answer"], usage=Usage())])
    response = await gateway.execute(_request())
    assert response.usage.source is UsageSource.UNKNOWN
    assert response.usage.input_tokens is None
    assert response.usage.estimated_cost_usd is None


# ═══════════════════════════════════════════════════════════════════════════
# Retry (LAW 7)
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_transient_failure_is_retried_on_the_same_route_and_recovers() -> None:
    adapter = ScriptedAdapter(
        "gemini",
        [
            TransientProviderError("boom", status_code=503),
            TransientProviderError("boom"),
            "recovered",
        ],
    )
    gateway = _gateway([adapter])
    response = await gateway.execute(_request())
    assert response.text == "recovered"
    assert response.policy is not None
    assert response.policy.attempts == 3
    assert response.policy.retries == 2
    assert response.degraded is False
    assert adapter.calls == 3


async def test_the_attempt_number_is_passed_to_the_adapter() -> None:
    """An adapter must be able to tell which attempt it is serving."""

    adapter = ScriptedAdapter(
        "gemini", [TransientProviderError("x"), TransientProviderError("x"), "ok"]
    )
    gateway = _gateway([adapter])
    await gateway.execute(_request())
    assert adapter.seen_attempts == [1, 2, 3]


@pytest.mark.parametrize(
    "error",
    [
        InvalidRequestError("bad request", status_code=400),
        AuthenticationError("bad key", status_code=401),
        ContentBlockedError("safety"),
        MalformedResponseError("no candidates"),
        StructuredOutputInvalidError("not json"),
        UnsupportedCapabilityError("no such operation"),
    ],
)
async def test_a_non_retryable_failure_costs_exactly_one_attempt(error: LLMError) -> None:
    """Retrying a 400 or a safety block burns the caller's deadline for nothing."""

    adapter = ScriptedAdapter("gemini", [error])
    gateway = _gateway([adapter])
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.kind is error.kind
    assert adapter.calls == 1


async def test_retry_exhaustion_raises_the_last_typed_error() -> None:
    adapter = ScriptedAdapter("gemini", [TransientProviderError("boom", status_code=503)])
    gateway = _gateway([adapter], fallback=FallbackPolicy(enabled=False, max_hops=0))
    with pytest.raises(TransientProviderError) as excinfo:
        await gateway.execute(_request())
    assert adapter.calls == 3
    assert excinfo.value.attempt == 3
    assert excinfo.value.request_id


async def test_a_provider_retry_after_is_honoured_as_the_backoff_floor() -> None:
    """RFC 9110: when the provider says when to come back, that is the answer."""

    slept: list[float] = []
    adapter = ScriptedAdapter(
        "gemini", [RateLimitedError("slow down", status_code=429, retry_after=0.05), "ok"]
    )
    gateway = _gateway([adapter])
    real_sleep = asyncio.sleep

    async def spy(delay: float, token: Any = None) -> float:
        slept.append(delay)
        await real_sleep(min(delay, 0.05))
        return delay

    gateway._sleep = spy  # type: ignore[method-assign]
    response = await gateway.execute(_request())
    assert response.text == "ok"
    assert any(delay >= 0.05 for delay in slept)


async def test_a_retry_after_beyond_policy_is_declined_not_served() -> None:
    """A provider asking for an hour must not hold the caller's deadline hostage."""

    clock = VirtualClock()
    adapter = ScriptedAdapter(
        "gemini", [RateLimitedError("come back tomorrow", status_code=429, retry_after=3600.0)]
    )
    gateway = _gateway([adapter], fallback=FallbackPolicy(enabled=False, max_hops=0), clock=clock)
    slept = _virtual_sleep(gateway, clock)
    with pytest.raises(RateLimitedError):
        await gateway.execute(_request())
    assert adapter.calls == 1  # declined immediately, no sleeping, no second attempt
    assert not slept


async def test_a_max_attempts_of_one_disables_retry_entirely() -> None:
    adapter = ScriptedAdapter("gemini", [TransientProviderError("boom")])
    gateway = _gateway(
        [adapter], retry=NO_RETRY, fallback=FallbackPolicy(enabled=False, max_hops=0)
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert adapter.calls == 1


async def test_backoff_between_attempts_is_bounded_by_policy() -> None:
    slept: list[float] = []
    adapter = ScriptedAdapter(
        "gemini",
        [
            TransientProviderError("a"),
            TransientProviderError("b"),
            TransientProviderError("c"),
        ],
    )
    gateway = _gateway([adapter], fallback=FallbackPolicy(enabled=False, max_hops=0))
    real_sleep = asyncio.sleep

    async def spy(delay: float, token: Any = None) -> float:
        slept.append(delay)
        await real_sleep(0)
        return delay

    gateway._sleep = spy  # type: ignore[method-assign]
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert len(slept) == 2  # three attempts, two gaps
    assert all(0.0 <= delay <= FAST_RETRY.max_delay_seconds for delay in slept)


# ═══════════════════════════════════════════════════════════════════════════
# Fallback (LAW 8)
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_exhausted_primary_falls_back_and_says_so() -> None:
    primary = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503)])
    backup = ScriptedAdapter("ollama", ["local answer"])
    gateway = _gateway(
        [primary, backup], routes=[_route("gemini", rank=10), _route("ollama", rank=20)]
    )
    response = await gateway.execute(_request())
    assert response.text == "local answer"
    assert response.provider == "ollama"
    assert response.degraded is True
    assert response.policy is not None
    assert response.policy.fallback_used is True
    assert response.policy.fallback_from == "gemini/m"
    assert response.policy.attempts == 4  # 3 on the primary, 1 on the backup


async def test_a_degraded_route_appends_nothing_the_caller_did_not_ask_for() -> None:
    """The disclaimer is a *surface* decision; the gateway reports the fact."""

    primary = ScriptedAdapter("gemini", [QuotaExhaustedError("drained")])
    degraded = ScriptedAdapter("local-degraded", ["[FAKE] answer"])
    gateway = _gateway(
        [primary, degraded],
        routes=[_route("gemini", rank=10), _route("local-degraded", rank=900, degraded=True)],
        fallback=FallbackPolicy(enabled=True, max_hops=1, allow_degraded_routes=True),
    )
    response = await gateway.execute(_request())
    assert response.text == "[FAKE] answer"
    assert response.degraded is True


async def test_degraded_routes_are_skipped_when_policy_forbids_them() -> None:
    primary = ScriptedAdapter("gemini", [QuotaExhaustedError("drained")])
    degraded = ScriptedAdapter("local-degraded", ["[FAKE]"])
    gateway = _gateway(
        [primary, degraded],
        routes=[_route("gemini", rank=10), _route("local-degraded", rank=900, degraded=True)],
        fallback=FallbackPolicy(enabled=True, max_hops=2, allow_degraded_routes=False),
    )
    with pytest.raises(QuotaExhaustedError):
        await gateway.execute(_request())
    assert degraded.calls == 0


async def test_a_caller_veto_on_fallback_yields_the_typed_failure() -> None:
    primary = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503)])
    backup = ScriptedAdapter("ollama", ["should not be used"])
    gateway = _gateway(
        [primary, backup], routes=[_route("gemini", rank=10), _route("ollama", rank=20)]
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request(provider="gemini", allow_fallback=False))
    assert backup.calls == 0


async def test_content_blocked_never_hops_to_another_provider() -> None:
    """LAW 8: a moderation decision is not a routing problem.

    Falling back would send a blocked prompt to a provider with weaker filters —
    the same request, a different answer, and no record of why.
    """

    primary = ScriptedAdapter("gemini", [ContentBlockedError("safety")])
    backup = ScriptedAdapter("ollama", ["laundered answer"])
    gateway = _gateway(
        [primary, backup], routes=[_route("gemini", rank=10), _route("ollama", rank=20)]
    )
    with pytest.raises(ContentBlockedError):
        await gateway.execute(_request())
    assert backup.calls == 0


async def test_a_misconfigured_fallback_policy_cannot_launder_a_content_block() -> None:
    """Typed truth outranks configuration (LAW 3 before LAW 8).

    ``FallbackPolicy.eligible_kinds`` is deployment configuration: a mistaken or
    hostile value can add ``CONTENT_BLOCKED`` to it. The engine still refuses,
    because an error's own ``fallback_eligible`` is a typed fact about what the
    failure *means* and configuration cannot redefine meaning. Without that
    check a moderation decision would be re-asked of a provider with weaker
    filters — the same prompt, a different answer, and no record of why.
    """

    primary = ScriptedAdapter("gemini", [ContentBlockedError("safety")])
    backup = ScriptedAdapter("ollama", ["laundered answer"])
    gateway = _gateway(
        [primary, backup],
        routes=[_route("gemini", rank=10), _route("ollama", rank=20)],
        fallback=FallbackPolicy(
            enabled=True,
            max_hops=2,
            eligible_kinds=FALLBACK_ELIGIBLE_KINDS | {LLMErrorKind.CONTENT_BLOCKED},
        ),
    )
    with pytest.raises(ContentBlockedError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.provider == "gemini"  # the route that blocked it
    assert backup.calls == 0


async def test_an_authentication_failure_hops_only_when_policy_says_it_may() -> None:
    """A bad key on one provider is a legitimate reason to try another."""

    primary = ScriptedAdapter("gemini", [AuthenticationError("bad key", status_code=401)])
    backup = ScriptedAdapter("ollama", ["local answer"])
    gateway = _gateway(
        [primary, backup], routes=[_route("gemini", rank=10), _route("ollama", rank=20)]
    )
    response = await gateway.execute(_request())
    assert response.provider == "ollama"
    assert primary.calls == 1  # not retried, but fallen back from


async def test_max_hops_bounds_the_number_of_providers_tried() -> None:
    failing = [
        ScriptedAdapter("a", [TransientProviderError("down")]),
        ScriptedAdapter("b", [TransientProviderError("down")]),
        ScriptedAdapter("c", [TransientProviderError("down")]),
    ]
    gateway = _gateway(
        failing,
        routes=[_route("a", rank=1), _route("b", rank=2), _route("c", rank=3)],
        fallback=FallbackPolicy(enabled=True, max_hops=1),
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert sum(adapter.calls for adapter in failing) <= 6  # two routes, three attempts each


async def test_a_route_without_an_adapter_is_reported_not_silently_skipped() -> None:
    gateway = _gateway(
        [ScriptedAdapter("gemini", ["ok"])],
        routes=[_route("missing-provider", rank=1), _route("gemini", rank=20)],
    )
    response = await gateway.execute(_request())
    assert response.provider == "gemini"
    records = list(_sink(gateway).records)
    assert records[-1].error_kind in {None, "internal_gateway_failure"} or response.degraded


# ═══════════════════════════════════════════════════════════════════════════
# Timeout budget (LAW 7: retry must not destroy the caller's deadline)
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_slow_attempt_is_cut_off_by_the_per_attempt_cap() -> None:
    adapter = ScriptedAdapter("gemini", ["late"], delay=5.0)
    gateway = _gateway(
        [adapter],
        timeout=TimeoutBudget(
            total_seconds=1.0,
            queue_wait_seconds=0.2,
            connect_seconds=0.05,
            read_seconds=0.1,
            per_attempt_seconds=0.2,
            max_backoff_seconds=0.05,
        ).validate(),
        retry=NO_RETRY,
    )
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.kind in {LLMErrorKind.UPSTREAM_TIMEOUT, LLMErrorKind.DEADLINE_EXCEEDED}
    assert adapter.cancelled_calls == 1  # the in-flight call was actually cancelled


async def test_a_caller_deadline_shorter_than_one_attempt_fails_typed_not_with_a_value_error() -> (
    None
):
    """Regression: policy evaluation used to raise ``ValueError`` here."""

    adapter = ScriptedAdapter("gemini", ["answer"], delay=0.2)
    gateway = _gateway([adapter], retry=NO_RETRY)
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request(deadline_seconds=0.01))
    assert excinfo.value.kind is LLMErrorKind.DEADLINE_EXCEEDED


async def test_a_zero_deadline_is_a_typed_deadline_failure() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["answer"])])
    with pytest.raises(DeadlineExceededError):
        await gateway.execute(_request(deadline_seconds=0.0))


async def test_retries_cannot_outlive_the_callers_total_deadline() -> None:
    """The whole point of a *budget*: attempts + backoff fit inside it."""

    adapter = ScriptedAdapter("gemini", ["late"], delay=0.5)
    gateway = _gateway(
        [adapter],
        timeout=TimeoutBudget(
            total_seconds=0.4,
            queue_wait_seconds=0.1,
            connect_seconds=0.05,
            read_seconds=0.15,
            per_attempt_seconds=0.2,
            max_backoff_seconds=0.05,
        ).validate(),
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=0.05, max_delay_seconds=0.1),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    elapsed = asyncio.get_running_loop().time() - started
    assert excinfo.value.kind in {LLMErrorKind.DEADLINE_EXCEEDED, LLMErrorKind.UPSTREAM_TIMEOUT}
    assert elapsed < 1.5  # not 5 attempts x 0.5s
    assert adapter.calls < 5


class TopJitter:
    """``rng`` stub pinned to the top of the jitter range.

    Full jitter draws ``uniform(0, cap)``; a test that must observe a *specific*
    backoff length cannot leave the draw to chance, so these tests pin it.
    """

    def random(self) -> float:
        return 1.0

    def uniform(self, low: float, high: float) -> float:
        return high


async def test_a_backoff_that_would_outlive_the_budget_is_declined_not_slept() -> None:
    """LAW 7: the chain stops *before* a sleep that cannot fit in the budget.

    Sleeping it out overruns the caller's deadline: the caller gets its error
    after the moment it stopped caring, while the gateway holds a concurrency
    slot and a rate-limit charge for a request nobody is waiting for. The
    assertion is on the *virtual* clock, so it is exact rather than a race —
    the refused backoff must never be slept, not even partly.
    """

    clock = VirtualClock()
    adapter = ScriptedAdapter("gemini", [TransientProviderError("blip", status_code=503)])
    gateway = _gateway(
        [adapter],
        clock=clock,
        timeout=TimeoutBudget(
            total_seconds=1.0,
            queue_wait_seconds=0.2,
            connect_seconds=0.05,
            read_seconds=0.2,
            per_attempt_seconds=0.3,
            max_backoff_seconds=0.9,
        ).validate(),
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=0.9, max_delay_seconds=0.9),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    gateway._rng = TopJitter()  # type: ignore[assignment]
    slept = _virtual_sleep(gateway, clock)
    started = clock.now

    with pytest.raises(DeadlineExceededError) as excinfo:
        await gateway.execute(_request(deadline_seconds=1.0))

    assert clock.now - started <= 1.0  # never slept past the caller's budget
    assert slept == [0.9]  # the second backoff was declined, not shortened
    assert adapter.calls == 2  # two attempts, then the truth
    assert excinfo.value.kind is LLMErrorKind.DEADLINE_EXCEEDED


async def test_an_expired_deadline_before_any_provider_call_is_not_charged_quota() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    with pytest.raises(DeadlineExceededError):
        await gateway.execute(_request(deadline_seconds=0.0))
    assert adapter.calls == 0


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════


async def test_repeated_health_failures_open_the_circuit_and_shed_the_route() -> None:
    """An open breaker keeps the route in the plan but refuses to spend quota.

    The route is *not* removed at planning time: a half-open probe is the only
    way it can ever recover. What changes is that the engine declines to call
    the adapter, and says so with ``detail="all_circuits_open"``.
    """

    adapter = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503)])
    backup = ScriptedAdapter("ollama", ["local"])
    gateway = _gateway(
        [adapter, backup],
        routes=[_route("gemini", rank=10), _route("ollama", rank=20)],
        retry=NO_RETRY,
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=2, recovery_seconds=60.0),
    )
    for _ in range(2):
        with pytest.raises(TransientProviderError):
            await gateway.execute(_request(provider="gemini"))
    before = adapter.calls
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request(provider="gemini"))
    assert excinfo.value.detail == "all_circuits_open"
    assert adapter.calls == before  # no round trip: the breaker shed the load


async def test_an_open_circuit_diverts_traffic_to_the_healthy_route() -> None:
    """The point of a breaker: shed one route, keep serving from the next."""

    adapter = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503)])
    backup = ScriptedAdapter("ollama", ["local answer"])
    gateway = _gateway(
        [adapter, backup],
        routes=[_route("gemini", rank=10), _route("ollama", rank=20)],
        retry=NO_RETRY,
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request(provider="gemini", allow_fallback=False))
    response = await gateway.execute(_request())
    assert response.provider == "ollama"
    assert adapter.calls == 1  # the open circuit kept us away from the sick provider


async def test_a_single_route_fleet_with_an_open_breaker_refuses_without_a_call() -> None:
    """With fallback disabled the caller asked for exactly one route: honour that."""

    adapter = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503)])
    gateway = _gateway(
        [adapter],
        retry=NO_RETRY,
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    with pytest.raises(TransientProviderError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.detail == "all_circuits_open"
    assert adapter.calls == 1


async def test_a_half_open_probe_lets_the_route_recover() -> None:
    clock = VirtualClock()
    adapter = ScriptedAdapter("gemini", [TransientProviderError("down", status_code=503), "back"])
    gateway = _gateway(
        [adapter],
        retry=NO_RETRY,
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=30.0),
        clock=clock,
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    clock.advance(31.0)  # cool-down elapsed → the route is probed again
    response = await gateway.execute(_request())
    assert response.text == "back"
    assert adapter.calls == 2


async def test_an_open_circuit_reports_the_fleet_state_when_nothing_else_is_left() -> None:
    adapter = ScriptedAdapter("gemini", [TransientProviderError("down")])
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    with pytest.raises(TransientProviderError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.detail == "all_circuits_open"


async def test_a_content_block_does_not_open_the_circuit() -> None:
    """Our caller's content is not evidence about the provider's health."""

    adapter = ScriptedAdapter("gemini", [ContentBlockedError("safety")])
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    for _ in range(3):
        with pytest.raises(ContentBlockedError):
            await gateway.execute(_request())
    assert adapter.calls == 3  # still being asked: the circuit never opened


async def test_a_malformed_request_does_not_open_the_circuit_either() -> None:
    adapter = ScriptedAdapter("gemini", [InvalidRequestError("bad", status_code=400)])
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    for _ in range(3):
        with pytest.raises(InvalidRequestError):
            await gateway.execute(_request())
    assert adapter.calls == 3


# ═══════════════════════════════════════════════════════════════════════════
# Local rate limiting / quota
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_local_minute_window_serves_a_burst_then_throttles_typed() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway(
        [adapter],
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=2)},
            max_wait_seconds=0.001,
        ),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    await gateway.execute(_request(prompt="a"))
    await gateway.execute(_request(prompt="b"))
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request(prompt="c"))
    assert excinfo.value.kind is LLMErrorKind.RATE_LIMITED
    assert excinfo.value.retryable is True
    assert adapter.calls == 2  # the third never reached the provider


async def test_a_drained_daily_budget_is_exhaustion_not_a_throttle() -> None:
    """Waiting cannot lift a daily cap, so the kind must not invite a retry."""

    clock = VirtualClock()
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway(
        [adapter],
        clock=clock,
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_day=1)},
            max_wait_seconds=3600.0,
        ),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    _virtual_sleep(gateway, clock)
    await gateway.execute(_request(prompt="a"))
    with pytest.raises(QuotaExhaustedError) as excinfo:
        await gateway.execute(_request(prompt="b"))
    assert excinfo.value.detail == "daily"
    assert excinfo.value.retryable is False
    clock.advance(60.0)  # a minute window would have rolled over; a day has not
    with pytest.raises(QuotaExhaustedError):
        await gateway.execute(_request(prompt="c"))


async def test_a_throttled_call_waits_for_capacity_when_policy_allows_it() -> None:
    clock = VirtualClock()
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway(
        [adapter],
        clock=clock,
        timeout=TimeoutBudget(
            total_seconds=600.0,
            queue_wait_seconds=30.0,
            connect_seconds=5.0,
            read_seconds=45.0,
            per_attempt_seconds=300.0,
            max_backoff_seconds=120.0,
        ).validate(),
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=2)},
            max_wait_seconds=120.0,
        ),
    )
    slept = _virtual_sleep(gateway, clock)
    await gateway.execute(_request(prompt="a"))
    await gateway.execute(_request(prompt="b"))
    response = await gateway.execute(_request(prompt="c"))
    assert response.text == "ok"
    assert response.policy is not None
    assert response.policy.rate_limited_locally is True
    assert response.timings.rate_waited_seconds > 0.0
    assert slept  # the gateway actually waited instead of failing the caller
    assert adapter.calls == 3


async def test_every_provider_attempt_charges_exactly_one_unit_of_quota() -> None:
    """Quota is charged per real provider call — not per request, not per hop.

    Charging per *request* would under-count a retry storm and let the gateway
    walk straight through a provider's RPM limit; charging on refusal would
    burn budget for a call that never happened.
    """

    adapter = ScriptedAdapter("gemini", [TransientProviderError("blip"), "ok"])
    gateway = _gateway(
        [adapter],
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=5)},
            max_wait_seconds=0.001,
        ),
    )
    response = await gateway.execute(_request())
    assert response.text == "ok"
    assert adapter.calls == 2
    snapshot = gateway.status()["rate_limits"]["gemini"]
    assert snapshot["charged"] == 2
    assert snapshot["minute_used"] == 2


async def test_a_request_refused_by_policy_charges_no_quota() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway(
        [adapter],
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=5)},
            max_wait_seconds=0.001,
        ),
    )
    with pytest.raises(PolicyRefusalError):
        await gateway.execute(_request(provider="nope"))
    assert gateway.status()["rate_limits"] == {}
    assert adapter.calls == 0


async def test_a_retry_that_needs_more_wait_than_the_budget_allows_is_a_deadline_failure() -> None:
    """The limiter says "come back in 60s"; the caller said "I have 60s total".

    Serving that retry would overrun the caller's deadline, so the honest answer
    is DEADLINE_EXCEEDED — and the provider is not asked again.
    """

    adapter = ScriptedAdapter("gemini", [TransientProviderError("blip"), "ok"])
    gateway = _gateway(
        [adapter],
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=1)},
            max_wait_seconds=5.0,
        ),
    )
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.kind in {LLMErrorKind.DEADLINE_EXCEEDED, LLMErrorKind.RATE_LIMITED}
    assert adapter.calls == 1  # the second attempt was never sent


# ═══════════════════════════════════════════════════════════════════════════
# Concurrency and shedding (LAW 6)
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_inflight_bound_is_respected_under_a_burst() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"], delay=0.05)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=16,
            overload_behavior=OverloadBehavior.WAIT,
        ),
        timeout=TimeoutBudget(
            total_seconds=10.0,
            queue_wait_seconds=5.0,
            connect_seconds=1.0,
            read_seconds=4.0,
            per_attempt_seconds=5.0,
            max_backoff_seconds=1.0,
        ).validate(),
    )
    peak = 0
    live = 0
    original = adapter.execute

    async def tracked(*args: Any, **kwargs: Any) -> AdapterResult:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        try:
            return await original(*args, **kwargs)
        finally:
            live -= 1

    adapter.execute = tracked  # type: ignore[method-assign]
    results = await asyncio.gather(*[gateway.execute(_request(prompt=str(i))) for i in range(8)])
    assert len(results) == 8
    assert peak <= 2


async def test_saturation_sheds_with_a_typed_overload_instead_of_growing() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"], delay=0.5)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=1,
            overload_behavior=OverloadBehavior.REJECT,
        ),
    )
    tasks = [
        asyncio.create_task(
            gateway.execute(
                _request(prompt=str(i), caller=Caller(CallerCategory.AGENT, "t", tenant_id=i))
            )
        )
        for i in range(6)
    ]
    await asyncio.sleep(0.02)
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    overloaded = [o for o in outcomes if isinstance(o, OverloadedError)]
    assert len(overloaded) >= 3
    assert all(o.kind is LLMErrorKind.OVERLOADED for o in overloaded)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["rejected"] >= 3
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_one_tenant_cannot_consume_the_whole_fleet() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"], delay=0.05)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=4,
            max_inflight_per_provider=4,
            max_inflight_per_tenant=1,
            max_queued=8,
            overload_behavior=OverloadBehavior.WAIT,
        ),
        timeout=TimeoutBudget(
            total_seconds=10.0,
            queue_wait_seconds=5.0,
            connect_seconds=1.0,
            read_seconds=4.0,
            per_attempt_seconds=5.0,
            max_backoff_seconds=1.0,
        ).validate(),
    )
    greedy = Caller(CallerCategory.AGENT, "greedy", tenant_id=1)
    other = Caller(CallerCategory.AGENT, "other", tenant_id=2)
    results = await asyncio.gather(
        *[gateway.execute(_request(prompt=str(i), caller=greedy)) for i in range(3)],
        gateway.execute(_request(prompt="x", caller=other)),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, LLMResponse)]
    assert len(successes) == 4


# ═══════════════════════════════════════════════════════════════════════════
# Refusals before any provider call
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_fleet_with_no_routes_refuses_typed_and_calls_nobody() -> None:
    """An unconfigured deployment fails with a reason, not with an AttributeError."""

    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = LLMGateway(
        policy=default_policy(routes=[]),
        sinks=[CollectingSink()],
        attach_default_sink=False,
    )
    with pytest.raises(PolicyRefusalError) as excinfo:
        await gateway.execute(_request())
    assert "no LLM route" in str(excinfo.value)
    assert adapter.calls == 0
    assert list(_sink_records(gateway))


async def test_strict_privacy_refuses_before_any_egress() -> None:
    adapter = ScriptedAdapter("openrouter", ["ok"])
    gateway = _gateway(
        [adapter],
        routes=[_route("openrouter", model="llama-3.3-70b:free")],
        privacy=PrivacyPolicy(strict=True),
    )
    with pytest.raises(PolicyRefusalError) as excinfo:
        await gateway.execute(_request())
    assert "privacy" in str(excinfo.value)
    assert adapter.calls == 0


async def test_a_route_context_gate_refuses_an_oversized_payload_before_sending_it() -> None:
    """Gemini reports "too long" and "malformed" with the same 400, so the gate is
    a declared character budget rather than a guess from a provider message."""

    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway(
        [adapter],
        routes=[_route("gemini", context_window_chars=100)],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request(prompt="x" * 5000))
    assert excinfo.value.kind is LLMErrorKind.CONTEXT_LIMIT
    assert adapter.calls == 0


async def test_a_payload_inside_the_context_gate_is_served() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter], routes=[_route("gemini", context_window_chars=1000)])
    response = await gateway.execute(_request(prompt="x" * 100))
    assert response.text == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# Structured output
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_validated_answer_is_returned_parsed() -> None:
    adapter = ScriptedAdapter("gemini", ['{"a": 1}'])
    gateway = _gateway([adapter])
    from nexus_ai_agent.llm.gateway.facade import json_object_validator

    response = await gateway.execute(
        _request(output_validator=json_object_validator(required_keys=("a",)))
    )
    assert response.structured == {"a": 1}
    assert response.text == '{"a": 1}'


async def test_an_answer_that_violates_the_callers_contract_is_typed_and_not_retried() -> None:
    """Validation is the caller's contract; a violation is not a provider outage."""

    from nexus_ai_agent.llm.gateway.facade import json_object_validator

    adapter = ScriptedAdapter("gemini", ["not json at all"])
    gateway = _gateway([adapter])
    with pytest.raises(StructuredOutputInvalidError) as excinfo:
        await gateway.execute(_request(output_validator=json_object_validator()))
    assert excinfo.value.retryable is False
    assert adapter.calls == 1


async def test_a_fenced_json_answer_is_accepted() -> None:
    from nexus_ai_agent.llm.gateway.facade import json_object_validator

    adapter = ScriptedAdapter("gemini", ['```json\n{"a": 2}\n```'])
    gateway = _gateway([adapter])
    response = await gateway.execute(_request(output_validator=json_object_validator()))
    assert response.structured == {"a": 2}


async def test_a_missing_required_key_is_a_contract_violation() -> None:
    from nexus_ai_agent.llm.gateway.facade import json_object_validator

    adapter = ScriptedAdapter("gemini", ['{"b": 1}'])
    gateway = _gateway([adapter])
    with pytest.raises(StructuredOutputInvalidError):
        await gateway.execute(
            _request(output_validator=json_object_validator(required_keys=("a",)))
        )


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency
# ═══════════════════════════════════════════════════════════════════════════


async def test_two_concurrent_calls_with_one_key_share_a_single_execution() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"], delay=0.05)
    gateway = _gateway([adapter])
    first, second = await asyncio.gather(
        gateway.execute(_request(idempotency_key="k1")),
        gateway.execute(_request(idempotency_key="k1")),
    )
    assert adapter.calls == 1
    assert first.request_id == second.request_id
    assert first.text == second.text == "answer"


async def test_a_repeated_key_inside_the_window_is_served_from_cache() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    await gateway.execute(_request(idempotency_key="k2"))
    response = await gateway.execute(_request(idempotency_key="k2"))
    assert adapter.calls == 1
    assert response.policy is not None
    assert response.policy.idempotency_hit is True


async def test_different_keys_are_executed_separately() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    await gateway.execute(_request(idempotency_key="a"))
    await gateway.execute(_request(idempotency_key="b"))
    assert adapter.calls == 2


async def test_no_key_means_no_deduplication() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = _gateway([adapter])
    await gateway.execute(_request())
    await gateway.execute(_request())
    assert adapter.calls == 2


async def test_a_failed_keyed_request_is_not_cached_as_a_success() -> None:
    """Caching a failure would turn one outage into a stuck caller."""

    adapter = ScriptedAdapter("gemini", [TransientProviderError("down"), "recovered"])
    gateway = _gateway(
        [adapter], retry=NO_RETRY, fallback=FallbackPolicy(enabled=False, max_hops=0)
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request(idempotency_key="k3"))
    response = await gateway.execute(_request(idempotency_key="k3"))
    assert response.text == "recovered"


async def test_the_idempotency_cache_is_bounded() -> None:
    adapter = ScriptedAdapter("gemini", ["answer"])
    gateway = LLMGateway(
        policy=default_policy(routes=[_route("gemini")], retry=FAST_RETRY),
        sinks=[CollectingSink()],
        attach_default_sink=False,
        idempotency_capacity=4,
    )
    gateway.register(adapter, None)
    for index in range(20):
        await gateway.execute(_request(prompt=str(index), idempotency_key=f"key-{index}"))
    status = gateway.status()["idempotency"]
    assert status["cached"] <= 4
    assert status["capacity"] == 4


# ═══════════════════════════════════════════════════════════════════════════
# Observability (LAW 10)
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_request_produces_exactly_one_record() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    response = await gateway.execute(_request())
    records = list(_sink(gateway).records)
    assert len(records) == 1
    assert records[0].request_id == response.request_id


async def test_a_failed_request_is_recorded_too() -> None:
    adapter = ScriptedAdapter("gemini", [InvalidRequestError("bad", status_code=400)])
    gateway = _gateway([adapter])
    with pytest.raises(InvalidRequestError):
        await gateway.execute(_request())
    records = list(_sink(gateway).records)
    assert len(records) == 1
    assert records[0].error_kind == "invalid_request"
    assert records[0].status_code == 400


async def test_the_record_names_the_caller_purpose_provider_and_outcome() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    await gateway.execute(
        _request(
            caller=Caller(CallerCategory.SUMMARIZER, "features.summarizer", tenant_id=9),
            purpose="summarize",
        )
    )
    payload = list(_sink(gateway).records)[0].as_dict()
    assert payload["caller"] == "summarizer:features.summarizer"
    assert payload["tenant_id"] == 9
    assert payload["purpose"] == "summarize"
    assert payload["provider"] == "gemini"
    assert payload["model"] == "m"
    assert payload["outcome"] == "success"


async def test_the_record_carries_timings_retries_and_attempt_history() -> None:
    adapter = ScriptedAdapter("gemini", [TransientProviderError("blip"), "ok"])
    gateway = _gateway([adapter])
    await gateway.execute(_request())
    record = list(_sink(gateway).records)[0]
    assert record.attempts_count == 2
    assert record.retries == 1
    payload = record.as_dict()
    assert payload["total_seconds"] >= 0.0
    assert payload["attempts"] == 2  # the count
    history = payload["attempt_outcomes"]
    assert len(history) == 2
    assert history[0]["outcome"] == "error"
    assert history[0]["error_kind"] == "transient_provider_failure"
    assert history[1]["outcome"] == "success"


async def test_the_record_never_carries_the_prompt_the_answer_or_a_credential() -> None:
    adapter = ScriptedAdapter("gemini", ["the model's answer text"])
    gateway = _gateway([adapter])
    await gateway.execute(
        _request(prompt="the user's secret prompt text", system="a secret system prompt"),
    )
    record = list(_sink(gateway).records)[0]
    blob = str(record.as_dict()) + str(record.otel_attributes())
    assert "secret prompt" not in blob
    assert "model's answer" not in blob
    assert "api_key" not in blob


async def test_metrics_count_requests_outcomes_and_retries() -> None:
    adapter = ScriptedAdapter("gemini", [TransientProviderError("blip"), "ok"])
    gateway = _gateway([adapter])
    await gateway.execute(_request())
    with pytest.raises(LLMError):
        await gateway.execute(_request(provider="nope"))
    metrics = gateway.metrics.as_dict()
    assert metrics["requests"] == 2
    # attempt 1 failed + attempt 2 succeeded; the pinned-to-a-missing-provider
    # request never reached an adapter, so it contributes zero attempts.
    assert metrics["attempts"] == 2
    assert metrics["retries"] == 1
    assert metrics["outcomes"]["success"] == 1
    # A pinned request with no matching route is refused by policy before any
    # adapter is touched, so it is recorded as a refusal, not an error.
    assert metrics["outcomes"]["policy_refusal"] == 1


async def test_recent_returns_the_newest_records_first_and_is_bounded() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    for index in range(5):
        await gateway.execute(_request(prompt=str(index)))
    recent = gateway.recent(limit=3)
    assert len(recent) == 3


async def test_status_is_json_serialisable_and_secret_free() -> None:
    import json

    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    await gateway.execute(_request())
    status = gateway.status()
    blob = json.dumps(status, default=str)
    assert "routes" in status and "scheduler" in status and "metrics" in status
    assert status["executed"] == 1
    assert "api_key" not in blob
    assert "x-goog-api-key" not in blob


# ═══════════════════════════════════════════════════════════════════════════
# Adapter misbehaviour (defensive boundaries)
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_adapter_raising_a_bare_exception_becomes_a_gateway_internal_error() -> None:
    """Never re-raised raw: the caller's ``except LLMError`` must always work."""

    adapter = ScriptedAdapter("gemini", [RuntimeError("adapter bug")])
    gateway = _gateway([adapter], fallback=FallbackPolicy(enabled=False, max_hops=0))
    with pytest.raises(GatewayInternalError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.retryable is False


async def test_an_adapter_returning_a_non_result_is_a_gateway_internal_error() -> None:
    class BadAdapter(ScriptedAdapter):
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return "just a string"

    gateway = _gateway(
        [BadAdapter("gemini", ["unused"])], fallback=FallbackPolicy(enabled=False, max_hops=0)
    )
    with pytest.raises(GatewayInternalError):
        await gateway.execute(_request())


async def test_an_adapter_returning_an_empty_answer_is_not_a_silent_success() -> None:
    gateway = _gateway(
        [ScriptedAdapter("gemini", [AdapterResult(text="", finish_reason=FinishReason.STOP)])],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    response = await gateway.execute(_request())
    # An empty answer with a STOP reason is what the provider said; the gateway
    # reports it faithfully rather than substituting text.
    assert response.text == ""


async def test_a_failing_sink_never_breaks_the_request() -> None:
    """Observability is a passenger, not a participant (LAW 10)."""

    class ExplodingSink:
        def emit(self, record: Any) -> None:
            raise RuntimeError("sink is broken")

    adapter = ScriptedAdapter("gemini", ["ok"])
    collector = CollectingSink()
    policy = default_policy(routes=[_route("gemini")], retry=FAST_RETRY)
    gateway = LLMGateway(
        policy=policy, sinks=[ExplodingSink(), collector], attach_default_sink=False
    )
    gateway.register(adapter, None)
    response = await gateway.execute(_request())
    assert response.text == "ok"
    assert len(list(collector.records)) == 1


async def test_adding_the_same_sink_twice_does_not_duplicate_records() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    collector = CollectingSink()
    gateway = _gateway([adapter], sink=collector)
    gateway.add_sink(collector)
    await gateway.execute(_request())
    assert len(list(collector.records)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_closed_gateway_refuses_new_work_typed() -> None:
    """Refused at the door: no plan, no route attribution, no record.

    A closed authority must decline *before* it does any work. Asserting only
    the error type would still pass if the refusal came from deeper inside (the
    retry loop, or the closed scheduler), which would mean the request had
    already been planned, admitted and observed after shutdown — work spent on
    a request the authority had already refused.
    """

    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    await gateway.aclose()
    assert gateway.closed is True
    assert adapter.closed == 1
    before = len(_sink_records(gateway))
    with pytest.raises(GatewayClosedError) as excinfo:
        await gateway.execute(_request())
    error = excinfo.value
    assert error.kind is LLMErrorKind.GATEWAY_CLOSED
    # No route was selected, so the refusal carries no route attribution.
    assert error.provider is None
    assert error.model is None
    assert error.attempt is None
    assert adapter.calls == 0
    assert len(_sink_records(gateway)) == before  # nothing was planned or observed


async def test_closing_twice_is_safe_and_does_not_re_close_adapters() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    gateway = _gateway([adapter])
    await gateway.aclose()
    await gateway.aclose()
    assert adapter.closed == 1


async def test_closing_wakes_every_waiter_instead_of_hanging_them() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"], delay=1.0)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=8,
            overload_behavior=OverloadBehavior.WAIT,
        ),
        timeout=TimeoutBudget(
            total_seconds=30.0,
            queue_wait_seconds=20.0,
            connect_seconds=1.0,
            read_seconds=10.0,
            per_attempt_seconds=15.0,
            max_backoff_seconds=1.0,
        ).validate(),
    )
    inflight = asyncio.create_task(
        gateway.execute(
            _request(prompt="slow", caller=Caller(CallerCategory.AGENT, "a", tenant_id=1))
        )
    )
    await asyncio.sleep(0.02)
    waiting = [
        asyncio.create_task(
            gateway.execute(
                _request(prompt=str(i), caller=Caller(CallerCategory.AGENT, "w", tenant_id=100 + i))
            )
        )
        for i in range(3)
    ]
    await asyncio.sleep(0.02)
    await gateway.aclose()
    outcomes = await asyncio.wait_for(asyncio.gather(*waiting, return_exceptions=True), timeout=3.0)
    assert all(
        isinstance(outcome, (GatewayClosedError, asyncio.CancelledError, LLMError))
        for outcome in outcomes
    )
    inflight.cancel()
    await asyncio.gather(inflight, return_exceptions=True)


async def test_inflight_work_is_awaited_before_the_gateway_reports_closed() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"], delay=0.05)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.01)
    await gateway.aclose()
    # The request either completed or failed typed; it was never dropped silently.
    try:
        response = await task
        assert isinstance(response, LLMResponse)
    except LLMError as exc:
        assert exc.kind in {LLMErrorKind.GATEWAY_CLOSED, LLMErrorKind.CANCELLED}


# ═══════════════════════════════════════════════════════════════════════════
# Embeddings through the same authority
# ═══════════════════════════════════════════════════════════════════════════


async def test_embeddings_go_through_the_same_policy_and_observability() -> None:
    from nexus_ai_agent.llm.gateway.contract import Modality

    adapter = ScriptedAdapter(
        "gemini",
        [AdapterResult(embedding=(0.1, 0.2, 0.3), finish_reason=FinishReason.STOP)],
        operations=frozenset({LLMOperation.EMBEDDINGS}),
    )
    gateway = _gateway(
        [adapter],
        routes=[
            Route(
                provider="gemini",
                model="text-embedding-004",
                operations=frozenset({LLMOperation.EMBEDDINGS}),
                modalities=frozenset({Modality.TEXT}),
                rank=10,
            )
        ],
    )
    vector = await gateway.embed("some text", caller=CALLER)
    assert vector == (0.1, 0.2, 0.3)
    assert len(list(_sink(gateway).records)) == 1


async def test_an_embedding_route_that_returns_no_vector_is_a_gateway_internal_error() -> None:
    from nexus_ai_agent.llm.gateway.contract import Modality

    adapter = ScriptedAdapter(
        "gemini",
        [AdapterResult(text="no vector here")],
        operations=frozenset({LLMOperation.EMBEDDINGS}),
    )
    gateway = _gateway(
        [adapter],
        routes=[
            Route(
                provider="gemini",
                model="emb",
                operations=frozenset({LLMOperation.EMBEDDINGS}),
                modalities=frozenset({Modality.TEXT}),
            )
        ],
    )
    with pytest.raises(GatewayInternalError):
        await gateway.embed("text", caller=CALLER)


async def test_embed_rejects_unknown_keyword_arguments_loudly() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["ok"])])
    with pytest.raises(TypeError):
        await gateway.embed("text", caller=CALLER, temperature=0.5)


# ═══════════════════════════════════════════════════════════════════════════
# Builder
# ═══════════════════════════════════════════════════════════════════════════


def test_the_builder_composes_a_working_gateway() -> None:
    adapter = ScriptedAdapter("gemini", ["ok"])
    collector = CollectingSink()
    gateway = (
        GatewayBuilder()
        .with_adapter(adapter, [_route("gemini")])
        .with_sink(collector)
        .with_rng(random.Random(0))
        .with_policy(retry=NO_RETRY)
        .build()
    )
    assert isinstance(gateway, LLMGateway)
    assert gateway.policy.retry.max_attempts == 1
    assert gateway.adapter("gemini") is adapter
    assert gateway.adapter("nope") is None


async def test_a_built_gateway_executes() -> None:
    adapter = ScriptedAdapter("gemini", ["built answer"])
    gateway = GatewayBuilder().with_adapter(adapter, [_route("gemini")]).build()
    assert await gateway.text("hi", caller=CALLER) == "built answer"
    await gateway.aclose()


def test_an_adapter_is_visible_in_the_operator_status() -> None:
    gateway = LLMGateway(
        policy=default_policy(routes=[_route("gemini")]), attach_default_sink=False
    )
    gateway.register(ScriptedAdapter("gemini", ["x"]), None)
    assert set(gateway.status()["adapters"]) == {"gemini"}


async def test_a_route_whose_adapter_was_never_registered_fails_typed() -> None:
    """Misconfiguration surfaces as a typed gateway error, not an AttributeError."""

    gateway = LLMGateway(
        policy=default_policy(routes=[_route("gemini")], retry=FAST_RETRY),
        sinks=[CollectingSink()],
        attach_default_sink=False,
    )
    with pytest.raises(GatewayInternalError) as excinfo:
        await gateway.execute(_request())
    assert "no registered adapter" in str(excinfo.value)


async def test_a_request_for_an_unregistered_provider_refuses_typed() -> None:
    gateway = _gateway([ScriptedAdapter("gemini", ["ok"])])
    with pytest.raises(PolicyRefusalError):
        await gateway.execute(_request(provider="anthropic"))
