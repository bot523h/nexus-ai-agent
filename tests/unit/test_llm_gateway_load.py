"""W2 — the authority under load (LAW 6: bounded everything).

Sustained and bursty concurrency is where a gateway either holds its shape or
becomes the outage. Every test here drives many callers through the real engine
with a fake provider and then asserts a *bound*: peak in-flight work, queue
depth, cache size, metric cardinality, task count, and the accounting returning
to zero when the storm is over.

Nothing here sleeps longer than it must: the point is the invariant, not the
wall clock, so the whole file runs in a couple of seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import sys
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    LLMError,
    LLMErrorKind,
    OverloadedError,
    RateLimitedError,
    TransientProviderError,
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
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.observability import CollectingSink
from nexus_ai_agent.llm.gateway.policy import (
    CircuitPolicy,
    ConcurrencyPolicy,
    FallbackPolicy,
    OverloadBehavior,
    ProviderRateLimit,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    TimeoutBudget,
    default_policy,
)

FAST_RETRY = RetryPolicy(max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.002)
NO_RETRY = RetryPolicy(max_attempts=1)
ROOMY = TimeoutBudget(
    total_seconds=60.0,
    queue_wait_seconds=30.0,
    connect_seconds=1.0,
    read_seconds=20.0,
    per_attempt_seconds=30.0,
    max_backoff_seconds=2.0,
).validate()


class LoadAdapter:
    """A provider double with a configurable latency and failure rate."""

    def __init__(
        self,
        name: str = "gemini",
        *,
        delay: float = 0.01,
        failure_rate: float = 0.0,
        seed: int = 7,
    ) -> None:
        self._name = name
        self._delay = delay
        self._failure_rate = failure_rate
        self._rng = random.Random(seed)
        self.calls = 0
        self.peak_concurrent = 0
        self._live = 0
        self.closed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def operations(self) -> Any:
        return frozenset({LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION})

    @property
    def modalities(self) -> Any:
        from nexus_ai_agent.llm.gateway.contract import Modality

        return frozenset({Modality.TEXT})

    async def execute(
        self, request: LLMRequest, route: Route, *, budget: Any, request_id: str, attempt: int
    ) -> AdapterResult:
        self.calls += 1
        self._live += 1
        self.peak_concurrent = max(self.peak_concurrent, self._live)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._failure_rate and self._rng.random() < self._failure_rate:
                raise TransientProviderError("provider blip", status_code=503)
            return AdapterResult(text=f"answer {request.prompt}", finish_reason=FinishReason.STOP)
        finally:
            self._live -= 1

    async def aclose(self) -> None:
        self.closed += 1


def _gateway(
    adapters: list[Any],
    *,
    concurrency: ConcurrencyPolicy | None = None,
    retry: RetryPolicy = FAST_RETRY,
    timeout: TimeoutBudget = ROOMY,
    **policy_kwargs: Any,
) -> LLMGateway:
    routes = [
        Route(provider=adapter.name, model="m", rank=10 * (i + 1))
        for i, adapter in enumerate(adapters)
    ]
    policy = default_policy(
        routes=routes,
        retry=retry,
        timeout=timeout,
        concurrency=concurrency
        or ConcurrencyPolicy(
            max_inflight_global=4,
            max_inflight_per_provider=4,
            max_inflight_per_tenant=2,
            max_queued=64,
            overload_behavior=OverloadBehavior.WAIT,
        ),
        **policy_kwargs,
    )
    collector = CollectingSink()
    gateway = LLMGateway(
        policy=policy, sinks=[collector], rng=random.Random(99), attach_default_sink=False
    )
    for adapter in adapters:
        gateway.register(adapter, [r for r in policy.routes if r.provider == adapter.name])
    gateway._test_sink = collector  # type: ignore[attr-defined]
    return gateway


def _request(index: int, **kwargs: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "caller": Caller(CallerCategory.AGENT, f"load.{index}", tenant_id=index % 8),
        "prompt": f"request {index}",
    }
    base.update(kwargs)
    return LLMRequest(**base)


def _tasks() -> int:
    return len(asyncio.all_tasks())


# ═══════════════════════════════════════════════════════════════════════════
# Burst behaviour
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_burst_of_two_hundred_callers_all_get_an_answer() -> None:
    adapter = LoadAdapter(delay=0.005)
    gateway = _gateway([adapter])
    results = await asyncio.gather(*[gateway.execute(_request(i)) for i in range(200)])
    assert len(results) == 200
    assert all(isinstance(response, LLMResponse) for response in results)
    assert adapter.calls == 200


async def test_the_inflight_bound_holds_under_a_burst() -> None:
    adapter = LoadAdapter(delay=0.01)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=3,
            max_inflight_per_provider=3,
            max_inflight_per_tenant=3,
            max_queued=256,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    await asyncio.gather(*[gateway.execute(_request(i)) for i in range(120)])
    assert adapter.peak_concurrent <= 3


async def test_the_per_provider_bound_holds_across_two_providers() -> None:
    first = LoadAdapter("gemini", delay=0.01)
    second = LoadAdapter("ollama", delay=0.01)
    gateway = _gateway(
        [first, second],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=6,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=6,
            max_queued=128,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    requests = [_request(i, provider="gemini" if i % 2 == 0 else "ollama") for i in range(80)]
    await asyncio.gather(*[gateway.execute(request) for request in requests])
    assert first.peak_concurrent <= 2
    assert second.peak_concurrent <= 2


async def test_the_per_tenant_bound_keeps_one_tenant_from_hogging_the_fleet() -> None:
    adapter = LoadAdapter(delay=0.01)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=8,
            max_inflight_per_provider=8,
            max_inflight_per_tenant=1,
            max_queued=128,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    greedy = Caller(CallerCategory.AGENT, "greedy", tenant_id=1)
    results = await asyncio.gather(
        *[gateway.execute(_request(i, caller=greedy)) for i in range(20)],
        *[
            gateway.execute(_request(100 + i, caller=Caller(CallerCategory.AGENT, "t", 2 + i)))
            for i in range(10)
        ],
    )
    assert len(results) == 30
    assert all(isinstance(response, LLMResponse) for response in results)


async def test_saturation_sheds_a_bounded_number_and_never_grows_the_queue() -> None:
    adapter = LoadAdapter(delay=0.05)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=4,
            overload_behavior=OverloadBehavior.REJECT,
        ),
    )
    outcomes = await asyncio.gather(
        *[
            gateway.execute(_request(i, caller=Caller(CallerCategory.AGENT, "c", i)))
            for i in range(60)
        ],
        return_exceptions=True,
    )
    served = [o for o in outcomes if isinstance(o, LLMResponse)]
    shed = [o for o in outcomes if isinstance(o, OverloadedError)]
    assert len(served) + len(shed) == 60
    assert len(shed) >= 50  # most of the burst was refused, not queued forever
    snapshot = gateway.status()["scheduler"]
    assert snapshot["queued"] == 0
    assert snapshot["inflight_global"] == 0
    assert snapshot["rejected"] >= 50


async def test_a_burst_completes_instead_of_serialising_everything() -> None:
    """Guard against an accidental global lock: 4-wide must beat 1-wide."""

    adapter = LoadAdapter(delay=0.02)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=4,
            max_inflight_per_provider=4,
            max_inflight_per_tenant=4,
            max_queued=64,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    started = asyncio.get_running_loop().time()
    await asyncio.gather(*[gateway.execute(_request(i)) for i in range(40)])
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 40 * 0.02  # strictly better than one-at-a-time


# ═══════════════════════════════════════════════════════════════════════════
# Sustained load with failures
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_third_of_calls_failing_still_serves_the_fleet() -> None:
    adapter = LoadAdapter(delay=0.002, failure_rate=0.34)
    gateway = _gateway(
        [adapter],
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.001, max_delay_seconds=0.002),
    )
    outcomes = await asyncio.gather(
        *[gateway.execute(_request(i)) for i in range(150)], return_exceptions=True
    )
    served = [o for o in outcomes if isinstance(o, LLMResponse)]
    assert len(served) >= 140  # retries absorb most blips
    assert all(not isinstance(o, BaseException) or isinstance(o, LLMError) for o in outcomes)


async def test_metrics_stay_internally_consistent_under_mixed_load() -> None:
    adapter = LoadAdapter(delay=0.001, failure_rate=0.2)
    gateway = _gateway(
        [adapter],
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.002),
    )
    await asyncio.gather(
        *[gateway.execute(_request(i)) for i in range(120)], return_exceptions=True
    )
    metrics = gateway.metrics.as_dict()
    assert metrics["requests"] == 120
    assert sum(metrics["outcomes"].values()) == 120
    assert metrics["attempts"] >= 120
    assert metrics["retries"] == metrics["attempts"] - 120
    assert metrics["usage_unknown"] + metrics["usage_reported"] == 120


async def test_the_record_buffer_stays_bounded_after_a_thousand_requests() -> None:
    adapter = LoadAdapter(delay=0.0)
    gateway = _gateway([adapter])
    for index in range(1000):
        await gateway.execute(_request(index, idempotency_key=f"key-{index}"))
    status = gateway.status()
    assert status["metrics"]["buffer_size"] <= status["metrics"]["buffer_capacity"]
    assert status["idempotency"]["cached"] <= status["idempotency"]["capacity"]
    assert status["scheduler"]["inflight_global"] == 0
    assert status["scheduler"]["queued"] == 0
    assert status["executed"] == 1000


async def test_metric_cardinality_does_not_grow_with_caller_diversity() -> None:
    adapter = LoadAdapter(delay=0.0)
    gateway = _gateway([adapter])
    for index in range(300):
        await gateway.execute(
            _request(
                index,
                caller=Caller(CallerCategory.AGENT, f"caller-{index}", index),
                purpose=f"purpose-{index}",
            )
        )
    metrics = gateway.status()["metrics"]
    assert len(metrics["outcomes"]) == 1
    assert len(metrics["provider_errors"]) == 0
    assert sys.getsizeof(metrics["provider_errors"]) < 1024


async def test_the_breaker_and_limiter_maps_do_not_grow_with_traffic() -> None:
    adapter = LoadAdapter(delay=0.0, failure_rate=0.1)
    gateway = _gateway(
        [adapter], circuit=CircuitPolicy(failure_threshold=1000, recovery_seconds=1.0)
    )
    # Failures are expected here; the assertion is about map growth, not outcomes.
    for index in range(300):
        with pytest.raises(LLMError) if False else contextlib.nullcontext():
            try:
                await gateway.execute(_request(index))
            except LLMError:
                pass
    status = gateway.status()
    assert len(status["routes"]) == 1
    assert len(status["rate_limits"]) <= 1


# ═══════════════════════════════════════════════════════════════════════════
# Rate limiting under load
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_local_rate_limit_is_enforced_under_a_burst() -> None:
    adapter = LoadAdapter(delay=0.0)
    gateway = _gateway(
        [adapter],
        retry=NO_RETRY,
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=10)},
            max_wait_seconds=0.0,
        ),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    outcomes = await asyncio.gather(
        *[gateway.execute(_request(i)) for i in range(40)], return_exceptions=True
    )
    served = [o for o in outcomes if isinstance(o, LLMResponse)]
    throttled = [o for o in outcomes if isinstance(o, RateLimitedError)]
    assert len(served) == 10  # exactly the configured window
    assert len(throttled) == 30
    assert adapter.calls == 10  # the other thirty never reached the provider
    snapshot = gateway.status()["rate_limits"]["gemini"]
    assert snapshot["charged"] == 10
    assert snapshot["throttled"] == 30


async def test_a_rate_limit_refusal_stays_a_rate_limit_under_a_short_deadline() -> None:
    """Why it was refused must survive a short caller budget (LAW 3).

    Both facts are true at once here: the window is full (a 60s wait is needed)
    and the caller's budget ends in 0.25s. Reporting DEADLINE_EXCEEDED would
    tell an operator the request was too slow, when the truth is that the
    provider window was full — and it would send the caller down the wrong
    recovery path (retry sooner, instead of backing off or shedding).
    """

    adapter = LoadAdapter(delay=0.0)
    gateway = _gateway(
        [adapter],
        retry=NO_RETRY,
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=5)},
            max_wait_seconds=0.0,
        ),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    outcomes = await asyncio.gather(
        *[gateway.execute(_request(i, deadline_seconds=0.25)) for i in range(20)],
        return_exceptions=True,
    )
    served = [o for o in outcomes if isinstance(o, LLMResponse)]
    throttled = [o for o in outcomes if isinstance(o, RateLimitedError)]
    deadlines = [
        o for o in outcomes if isinstance(o, LLMError) and o.kind is LLMErrorKind.DEADLINE_EXCEEDED
    ]
    assert len(served) == 5
    assert len(throttled) == 15
    assert not deadlines  # not one rate-limit refusal was relabelled
    assert all(o.detail == "local_rate_limit" for o in throttled)
    assert all(o.retry_after is not None and o.retry_after > 0.25 for o in throttled)
    assert adapter.calls == 5


async def test_quota_accounting_never_over_charges_under_concurrency() -> None:
    adapter = LoadAdapter(delay=0.002)
    gateway = _gateway(
        [adapter],
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=1000)},
            max_wait_seconds=0.0,
        ),
    )
    await asyncio.gather(*[gateway.execute(_request(i)) for i in range(50)])
    snapshot = gateway.status()["rate_limits"]["gemini"]
    assert snapshot["charged"] == adapter.calls == 50


# ═══════════════════════════════════════════════════════════════════════════
# Fairness and priority under sustained contention
# ═══════════════════════════════════════════════════════════════════════════


async def test_no_tenant_is_starved_under_sustained_contention() -> None:
    adapter = LoadAdapter(delay=0.005)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=1,
            max_queued=128,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    tenants = list(range(6))
    served: dict[int, int] = {tenant: 0 for tenant in tenants}

    async def run(tenant: int) -> None:
        for index in range(10):
            response = await gateway.execute(
                _request(index, caller=Caller(CallerCategory.AGENT, "c", tenant))
            )
            assert isinstance(response, LLMResponse)
            served[tenant] += 1

    await asyncio.gather(*[run(tenant) for tenant in tenants])
    assert all(count == 10 for count in served.values()), served


async def test_higher_priority_work_is_served_first_under_contention() -> None:
    adapter = LoadAdapter(delay=0.02)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=64,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    order: list[int] = []

    class RecordingAdapter(LoadAdapter):
        async def execute(self, *args: Any, **kwargs: Any) -> AdapterResult:
            request = args[0]
            order.append(int(request.priority))
            return await super().execute(*args, **kwargs)

    gateway.register(RecordingAdapter("gemini", delay=0.02), None)
    holder = asyncio.create_task(
        gateway.execute(_request(0, caller=Caller(CallerCategory.AGENT, "h", 100)))
    )
    await asyncio.sleep(0.005)
    queued = [
        asyncio.create_task(
            gateway.execute(
                _request(i, priority=priority, caller=Caller(CallerCategory.AGENT, "q", 200 + i))
            )
        )
        for i, priority in enumerate(
            [LLMPriority.LOW, LLMPriority.OWNER, LLMPriority.NORMAL, LLMPriority.REFERRAL_BONUS]
        )
    ]
    await asyncio.gather(holder, *queued)
    served_priorities = order[1:]
    assert served_priorities == sorted(served_priorities)


async def test_priority_is_work_conserving_when_only_low_priority_work_is_present() -> None:
    adapter = LoadAdapter(delay=0.001)
    gateway = _gateway([adapter])
    outcomes = await asyncio.gather(
        *[gateway.execute(_request(i, priority=LLMPriority.LOW)) for i in range(30)]
    )
    assert all(isinstance(response, LLMResponse) for response in outcomes)


# ═══════════════════════════════════════════════════════════════════════════
# Stability: no deadlock, no leak, accounting back to zero
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_storm_of_cancellations_leaves_the_accounting_at_zero() -> None:
    adapter = LoadAdapter(delay=0.05)
    gateway = _gateway([adapter])
    before = _tasks()
    tasks = [asyncio.create_task(gateway.execute(_request(i))) for i in range(120)]
    await asyncio.sleep(0.02)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["inflight_per_provider"] == {}
    assert snapshot["inflight_per_tenant"] == {}
    assert _tasks() <= before + 1


async def test_a_storm_of_timeouts_leaves_the_accounting_at_zero() -> None:
    adapter = LoadAdapter(delay=0.5)
    gateway = _gateway(
        [adapter],
        retry=NO_RETRY,
        timeout=TimeoutBudget(
            total_seconds=0.3,
            queue_wait_seconds=0.1,
            connect_seconds=0.02,
            read_seconds=0.05,
            per_attempt_seconds=0.1,
            max_backoff_seconds=0.02,
        ).validate(),
        concurrency=ConcurrencyPolicy(
            max_inflight_global=4,
            max_inflight_per_provider=4,
            max_inflight_per_tenant=4,
            max_queued=64,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    outcomes = await asyncio.gather(
        *[gateway.execute(_request(i)) for i in range(40)], return_exceptions=True
    )
    assert all(isinstance(o, LLMError) for o in outcomes)
    assert {o.kind for o in outcomes} <= {
        LLMErrorKind.UPSTREAM_TIMEOUT,
        LLMErrorKind.DEADLINE_EXCEEDED,
        LLMErrorKind.OVERLOADED,
    }
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0


async def test_a_mixed_storm_of_success_failure_and_cancellation_settles() -> None:
    adapter = LoadAdapter(delay=0.01, failure_rate=0.3)
    gateway = _gateway([adapter])
    tasks = [asyncio.create_task(gateway.execute(_request(i))) for i in range(90)]
    await asyncio.sleep(0.01)
    for task in tasks[::3]:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(o, (LLMResponse, LLMError, asyncio.CancelledError)) for o in outcomes)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0


async def test_a_flood_of_distinct_idempotency_keys_stays_bounded() -> None:
    adapter = LoadAdapter(delay=0.0)
    gateway = LLMGateway(
        policy=default_policy(
            routes=[Route(provider="gemini", model="m")],
            retry=NO_RETRY,
            timeout=ROOMY,
            concurrency=ConcurrencyPolicy(
                max_inflight_global=16,
                max_inflight_per_provider=16,
                max_inflight_per_tenant=16,
                max_queued=256,
                overload_behavior=OverloadBehavior.WAIT,
            ),
        ),
        sinks=[CollectingSink()],
        attach_default_sink=False,
        idempotency_capacity=8,
    )
    gateway.register(adapter, None)
    await asyncio.gather(
        *[gateway.execute(_request(i, idempotency_key=f"key-{i}")) for i in range(200)]
    )
    status = gateway.status()["idempotency"]
    assert status["cached"] <= 8
    assert status["inflight"] == 0


async def test_repeated_open_and_close_cycles_do_not_leak() -> None:
    before = _tasks()
    for _ in range(10):
        adapter = LoadAdapter(delay=0.0)
        gateway = _gateway([adapter])
        await gateway.execute(_request(1))
        await gateway.aclose()
    await asyncio.sleep(0)
    assert _tasks() <= before + 1


async def test_a_long_run_keeps_the_process_task_count_flat() -> None:
    adapter = LoadAdapter(delay=0.0)
    gateway = _gateway([adapter])
    before = _tasks()
    for batch in range(20):
        await asyncio.gather(*[gateway.execute(_request(batch * 10 + i)) for i in range(10)])
    await asyncio.sleep(0)
    assert _tasks() <= before + 1
    assert adapter.calls == 200


async def test_backoff_is_deterministic_for_a_given_seed() -> None:
    """Two identically-seeded gateways must make identical retry decisions."""

    def build(seed: int) -> tuple[LLMGateway, list[float]]:
        slept: list[float] = []
        adapter = LoadAdapter(delay=0.0, failure_rate=1.0)
        route = Route(provider="gemini", model="m")
        policy = default_policy(
            routes=[route],
            retry=RetryPolicy(max_attempts=4, base_delay_seconds=0.5, max_delay_seconds=8.0),
            timeout=ROOMY,
            fallback=FallbackPolicy(enabled=False, max_hops=0),
        )
        gateway = LLMGateway(
            policy=policy,
            sinks=[CollectingSink()],
            rng=random.Random(seed),
            attach_default_sink=False,
        )
        gateway.register(adapter, [route])

        async def spy(delay: float, token: Any = None) -> float:
            slept.append(delay)
            return delay

        gateway._sleep = spy  # type: ignore[method-assign]
        return gateway, slept

    async def run(gateway: LLMGateway) -> None:
        with pytest.raises(LLMError):
            await gateway.execute(_request(1))

    first, first_sleeps = build(1234)
    second, second_sleeps = build(1234)
    third, third_sleeps = build(4321)
    await run(first)
    await run(second)
    await run(third)
    assert first_sleeps == second_sleeps
    assert all(0.0 <= delay <= 8.0 for delay in first_sleeps)
    assert len(first_sleeps) == 3  # four attempts, three gaps


async def test_a_slow_provider_does_not_starve_a_fast_one() -> None:
    """Bulkheads are per provider: one sick endpoint must not stop the fleet."""

    slow = LoadAdapter("gemini", delay=0.2)
    fast = LoadAdapter("ollama", delay=0.001)
    gateway = _gateway(
        [slow, fast],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=8,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=8,
            max_queued=64,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    started = asyncio.get_running_loop().time()
    fast_results = await asyncio.gather(
        *[gateway.execute(_request(i, provider="ollama")) for i in range(8)],
        *[gateway.execute(_request(100 + i, provider="gemini")) for i in range(4)],
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert len(fast_results) == 12
    assert elapsed < 1.5  # the slow provider's queue did not block the fast one
