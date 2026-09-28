"""W2 — cancellation is sacred (LAW 5).

A caller that walks away must stop the work: no further attempts, no further
backoff sleeps, no orphaned task holding a provider connection, no leaked
concurrency slot, and exactly one honest record saying "cancelled".

The tests distinguish two different things that both look like cancellation:

* the caller's **task** is cancelled (``task.cancel()``) — ``CancelledError``
  must propagate untouched, because the event loop is telling *us* to stop; and
* the caller **withdraws the request** through ``LLMRequest.cancellation`` while
  its own task is still alive — that is a typed :class:`CancelledByCallerError`,
  because raising ``CancelledError`` there would falsely mark the caller's task
  as cancelled.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    CancelledByCallerError,
    GatewayClosedError,
    LLMError,
    LLMErrorKind,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway.adapters import AdapterResult
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    FinishReason,
    LLMOperation,
    LLMRequest,
    LLMResponse,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.observability import (
    OUTCOME_CANCELLED,
    CollectingSink,
)
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

CALLER = Caller(category=CallerCategory.AGENT, name="test.cancellation")
FAST_RETRY = RetryPolicy(max_attempts=5, base_delay_seconds=0.05, max_delay_seconds=0.2)
GENEROUS = TimeoutBudget(
    total_seconds=30.0,
    queue_wait_seconds=10.0,
    connect_seconds=1.0,
    read_seconds=10.0,
    per_attempt_seconds=15.0,
    max_backoff_seconds=5.0,
).validate()


class SlowAdapter:
    """An adapter that reports how its in-flight call was interrupted."""

    def __init__(
        self, name: str = "gemini", delay: float = 0.5, script: list[Any] | None = None
    ) -> None:
        self._name = name
        self._delay = delay
        self._script = list(script or ["answer"])
        self.calls = 0
        self.interrupted = 0
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
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult:
        self.calls += 1
        if self._delay:
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                self.interrupted += 1
                raise
        outcome = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return AdapterResult(text=str(outcome), finish_reason=FinishReason.STOP)

    async def aclose(self) -> None:
        self.closed += 1


class MaxJitter:
    """``rng`` stub returning the *top* of the jitter range.

    Full jitter is ``uniform(0, cap)``; a test that must observe a long backoff
    cannot leave the draw to chance, so these tests pin the draw to the maximum.
    """

    def random(self) -> float:
        return 1.0

    def uniform(self, low: float, high: float) -> float:
        return high


def _gateway(
    adapters: list[Any],
    *,
    sink: CollectingSink | None = None,
    rng: Any = None,
    **policy_kwargs: Any,
) -> LLMGateway:
    collector = sink if sink is not None else CollectingSink()
    routes = [Route(provider=adapter.name, model="m") for adapter in adapters]
    retry = policy_kwargs.pop("retry", FAST_RETRY)
    policy = default_policy(
        routes=routes, retry=retry, timeout=policy_kwargs.pop("timeout", GENEROUS), **policy_kwargs
    )
    kwargs: dict[str, Any] = {}
    if rng is not None:
        kwargs["rng"] = rng
    gateway = LLMGateway(policy=policy, sinks=[collector], attach_default_sink=False, **kwargs)
    for adapter in adapters:
        gateway.register(adapter, [r for r in policy.routes if r.provider == adapter.name])
    gateway._test_sink = collector  # type: ignore[attr-defined]
    return gateway


def _sink(gateway: LLMGateway) -> CollectingSink:
    return gateway._test_sink  # type: ignore[attr-defined,no-any-return]


def _request(**kwargs: Any) -> LLMRequest:
    base: dict[str, Any] = {"caller": CALLER, "prompt": "hello"}
    base.update(kwargs)
    return LLMRequest(**base)


def _task_count() -> int:
    return len(asyncio.all_tasks())


# ═══════════════════════════════════════════════════════════════════════════
# Task cancellation
# ═══════════════════════════════════════════════════════════════════════════


async def test_cancelling_the_callers_task_propagates_cancelled_error_untouched() -> None:
    """The loop is talking to the caller; we must not translate that message."""

    adapter = SlowAdapter(delay=1.0)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.interrupted == 1  # the provider call was actually stopped


async def test_a_cancelled_task_leaves_no_orphan_task_behind() -> None:
    adapter = SlowAdapter(delay=1.0)
    gateway = _gateway([adapter])
    before = _task_count()
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert _task_count() <= before


async def test_cancellation_stops_the_retry_chain_immediately() -> None:
    """A retry loop that ignores cancellation keeps spending quota for nobody."""

    adapter = SlowAdapter(delay=0.3, script=[TransientProviderError("blip", status_code=503)])
    gateway = _gateway([adapter], fallback=FallbackPolicy(enabled=False, max_hops=0))
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.1)  # inside the first attempt
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.calls == 1  # never reached attempt two


async def test_cancellation_during_backoff_does_not_sleep_it_out() -> None:
    adapter = SlowAdapter(
        delay=0.0,
        script=[TransientProviderError("blip", status_code=503), "answer"],
    )
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=3.0, max_delay_seconds=6.0),
        rng=MaxJitter(),
    )
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)  # attempt 1 done, now inside a multi-second backoff
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0  # the 3s backoff was not served
    assert adapter.calls == 1


async def test_a_cancelled_request_releases_its_concurrency_slot() -> None:
    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=4,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["inflight_per_provider"] == {}


async def test_a_cancelled_request_still_leaves_exactly_one_record() -> None:
    """LAW 10 does not stop at the happy path."""

    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    records = list(_sink(gateway).records)
    assert len(records) == 1
    assert records[0].outcome == OUTCOME_CANCELLED


async def test_a_callers_cancellation_does_not_open_the_circuit_breaker() -> None:
    """Our caller walking away says nothing about the provider's health."""

    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway(
        [adapter],
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60.0),
    )
    for _ in range(3):
        task = asyncio.create_task(gateway.execute(_request()))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    breaker = gateway.status()["routes"][0]["circuit"]
    assert breaker["open"] is False
    assert breaker["state"] == "closed"


async def test_a_cancellation_storm_leaves_the_accounting_at_zero() -> None:
    adapter = SlowAdapter(delay=0.2)
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
    tasks = [asyncio.create_task(gateway.execute(_request(prompt=str(i)))) for i in range(40)]
    await asyncio.sleep(0.05)
    for task in tasks:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(outcome, (asyncio.CancelledError, LLMError)) for outcome in outcomes)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["cancelled"] >= 1


async def test_a_waiter_cancelled_in_the_queue_does_not_block_the_holder() -> None:
    adapter = SlowAdapter(delay=0.2)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=4,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    holder = asyncio.create_task(
        gateway.execute(
            _request(prompt="holder", caller=Caller(CallerCategory.AGENT, "h", tenant_id=1))
        )
    )
    await asyncio.sleep(0.02)
    waiter = asyncio.create_task(
        gateway.execute(
            _request(prompt="waiter", caller=Caller(CallerCategory.AGENT, "w", tenant_id=2))
        )
    )
    await asyncio.sleep(0.02)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    response = await asyncio.wait_for(holder, timeout=3.0)
    assert isinstance(response, LLMResponse)


# ═══════════════════════════════════════════════════════════════════════════
# Withdrawal through the request's own cancellation token
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_withdrawal_token_set_before_execution_never_reaches_a_provider() -> None:
    adapter = SlowAdapter(delay=0.0)
    gateway = _gateway([adapter])
    token = asyncio.Event()
    token.set()
    with pytest.raises(CancelledByCallerError) as excinfo:
        await gateway.execute(_request(cancellation=token))
    assert excinfo.value.kind is LLMErrorKind.CANCELLED
    assert excinfo.value.retryable is False
    assert adapter.calls == 0


async def test_a_withdrawal_mid_flight_stops_the_work_with_a_typed_error() -> None:
    """The caller's task is alive, so ``CancelledError`` would be a lie."""

    adapter = SlowAdapter(delay=1.0)
    gateway = _gateway([adapter])
    token = asyncio.Event()
    task = asyncio.create_task(gateway.execute(_request(cancellation=token)))
    await asyncio.sleep(0.05)
    token.set()
    with pytest.raises(CancelledByCallerError):
        await asyncio.wait_for(task, timeout=3.0)
    assert adapter.interrupted == 1


async def test_a_withdrawal_during_backoff_stops_the_retry_chain() -> None:
    adapter = SlowAdapter(
        delay=0.0, script=[TransientProviderError("blip", status_code=503), "answer"]
    )
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=2.0, max_delay_seconds=4.0),
        rng=MaxJitter(),
    )
    token = asyncio.Event()
    task = asyncio.create_task(gateway.execute(_request(cancellation=token)))
    await asyncio.sleep(0.05)  # attempt 1 failed; now inside a 2s+ backoff
    loop = asyncio.get_running_loop()
    withdrawn_at = loop.time()
    token.set()
    with pytest.raises(CancelledByCallerError):
        await asyncio.wait_for(task, timeout=3.0)
    elapsed = loop.time() - withdrawn_at
    assert adapter.calls == 1  # the chain stopped; attempt two was never sent
    # *Immediately* means during the sleep, not after it: a backoff that is not
    # cancellation-aware would still raise the same typed error two seconds
    # later, having held its slot and its rate window the whole time.
    assert elapsed < 1.0, f"the withdrawal took {elapsed:.2f}s of a 2s backoff"


async def test_a_withdrawal_while_queued_is_reported_as_a_typed_cancellation() -> None:
    adapter = SlowAdapter(delay=0.3)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=4,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    holder = asyncio.create_task(
        gateway.execute(
            _request(prompt="holder", caller=Caller(CallerCategory.AGENT, "h", tenant_id=1))
        )
    )
    await asyncio.sleep(0.02)
    token = asyncio.Event()
    queued = asyncio.create_task(
        gateway.execute(
            _request(
                prompt="queued",
                caller=Caller(CallerCategory.AGENT, "q", tenant_id=2),
                cancellation=token,
            )
        )
    )
    await asyncio.sleep(0.02)
    token.set()  # withdraw while still waiting for a slot
    with pytest.raises(CancelledByCallerError) as excinfo:
        await asyncio.wait_for(queued, timeout=3.0)
    assert excinfo.value.kind is LLMErrorKind.CANCELLED
    await asyncio.wait_for(holder, timeout=3.0)


async def test_a_withdrawal_during_a_rate_limit_wait_stops_the_request() -> None:
    """Waiting for quota is exactly the moment a caller is most likely to leave."""

    adapter = SlowAdapter(delay=0.0)
    gateway = _gateway(
        [adapter],
        timeout=TimeoutBudget(
            total_seconds=600.0,
            queue_wait_seconds=30.0,
            connect_seconds=5.0,
            read_seconds=45.0,
            per_attempt_seconds=300.0,
            max_backoff_seconds=120.0,
        ).validate(),
        rate_limit=RateLimitPolicy(
            per_provider={"gemini": ProviderRateLimit(requests_per_minute=1)},
            max_wait_seconds=300.0,
        ),
    )
    await gateway.execute(_request(prompt="first"))
    token = asyncio.Event()
    task = asyncio.create_task(gateway.execute(_request(prompt="second", cancellation=token)))
    await asyncio.sleep(0.05)  # the window is full: the gateway is now waiting
    token.set()
    with pytest.raises(CancelledByCallerError):
        await asyncio.wait_for(task, timeout=3.0)
    assert adapter.calls == 1  # the throttled attempt was never sent


async def test_an_unusable_cancellation_token_is_ignored_rather_than_guessed_at() -> None:
    """Turning a caller bug into a spurious cancellation would be worse."""

    adapter = SlowAdapter(delay=0.0)
    gateway = _gateway([adapter])
    response = await gateway.execute(_request(cancellation=object()))
    assert response.text == "answer"


async def test_a_cancellation_token_with_a_broken_wait_is_ignored() -> None:
    class BrokenToken:
        def wait(self) -> Any:
            raise RuntimeError("token is broken")

    adapter = SlowAdapter(delay=0.0)
    gateway = _gateway([adapter])
    response = await gateway.execute(_request(cancellation=BrokenToken()))
    assert response.text == "answer"


async def test_an_awaitable_cancellation_signal_is_supported() -> None:
    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway([adapter])

    async def signal() -> None:
        await asyncio.sleep(0.05)

    with pytest.raises(CancelledByCallerError):
        await gateway.execute(_request(cancellation=signal()))


# ═══════════════════════════════════════════════════════════════════════════
# Cancellation and shared / idempotent work
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_coalesced_waiter_is_never_falsely_marked_cancelled() -> None:
    """Two callers, one idempotency key, one execution — and one of them leaves.

    Coalesced callers share the execution and therefore share its fate, but the
    survivor's *task* is still alive: leaking a bare ``CancelledError`` into it
    would tell the event loop the wrong thing. The waiter gets a typed
    cancellation it can catch, report and retry.
    """

    adapter = SlowAdapter(delay=0.3)
    gateway = _gateway([adapter])
    leaver = asyncio.create_task(gateway.execute(_request(idempotency_key="shared")))
    stayer = asyncio.create_task(gateway.execute(_request(idempotency_key="shared")))
    await asyncio.sleep(0.05)
    leaver.cancel()
    await asyncio.gather(leaver, return_exceptions=True)
    with pytest.raises(CancelledByCallerError) as excinfo:
        await asyncio.wait_for(stayer, timeout=3.0)
    assert excinfo.value.detail == "shared_execution_cancelled"
    assert excinfo.value.kind is LLMErrorKind.CANCELLED
    assert not stayer.cancelled()  # the survivor's task was not cancelled
    assert adapter.calls == 1  # still only one provider execution


async def test_a_coalesced_waiter_can_retry_after_the_owner_leaves() -> None:
    adapter = SlowAdapter(delay=0.2)
    gateway = _gateway([adapter])
    leaver = asyncio.create_task(gateway.execute(_request(idempotency_key="k")))
    stayer = asyncio.create_task(gateway.execute(_request(idempotency_key="k")))
    await asyncio.sleep(0.05)
    leaver.cancel()
    await asyncio.gather(leaver, return_exceptions=True)
    with pytest.raises(CancelledByCallerError):
        await asyncio.wait_for(stayer, timeout=3.0)
    retry = await asyncio.wait_for(gateway.execute(_request(idempotency_key="k")), timeout=3.0)
    assert retry.text == "answer"


async def test_cancelling_every_waiter_on_a_shared_key_settles_them_all() -> None:
    adapter = SlowAdapter(delay=0.3)
    gateway = _gateway([adapter])
    tasks = [
        asyncio.create_task(gateway.execute(_request(idempotency_key="shared"))) for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    for task in tasks:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(outcome, (asyncio.CancelledError, LLMError)) for outcome in outcomes)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0


async def test_a_cancelled_keyed_request_is_not_cached_as_a_result() -> None:
    adapter = SlowAdapter(delay=0.3)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request(idempotency_key="k")))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert gateway.status()["idempotency"]["cached"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Cancellation interacts correctly with the rest of the policy
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_cancellation_is_never_retried_and_never_falls_back() -> None:
    adapter = SlowAdapter(delay=0.5)
    backup = SlowAdapter("ollama", delay=0.0, script=["local"])
    gateway = _gateway([adapter, backup])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert backup.calls == 0


async def test_a_completed_request_is_not_affected_by_a_late_cancellation() -> None:
    adapter = SlowAdapter(delay=0.0)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    response = await task
    task.cancel()  # too late; the work is done and reported
    assert response.text == "answer"
    await asyncio.gather(task, return_exceptions=True)


async def test_closing_the_gateway_returns_promptly_and_refuses_new_work() -> None:
    """``aclose()`` must not hang on a slow provider, and must not accept more."""

    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.02)
    await asyncio.wait_for(gateway.aclose(), timeout=1.0)
    assert gateway.closed is True
    with pytest.raises(GatewayClosedError):
        await gateway.execute(_request(prompt="after close"))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_closing_the_gateway_stops_an_inflight_retry_chain() -> None:
    """Shutdown at the next attempt boundary: never against a released pool."""

    adapter = SlowAdapter(
        delay=0.0, script=[TransientProviderError("blip", status_code=503), "answer"]
    )
    gateway = _gateway(
        [adapter],
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=2.0, max_delay_seconds=4.0),
        rng=MaxJitter(),
    )
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)  # attempt 1 failed; sitting in a long backoff
    await asyncio.wait_for(gateway.aclose(), timeout=1.0)
    with pytest.raises(LLMError) as excinfo:
        await asyncio.wait_for(task, timeout=5.0)
    error = excinfo.value
    assert error.kind is LLMErrorKind.GATEWAY_CLOSED
    assert adapter.calls == 1  # no attempt was made against a closed gateway
    # The *retry chain* stopped it, at the engine's own attempt boundary — not
    # later, deeper inside admission. Both boundaries raise the same typed kind
    # and the engine stamps the same route attribution onto either, so the
    # message is the only fact that says where the chain stopped; an operator
    # reading "scheduler is closed" would be looking at a request that kept
    # working (rate gate, admission) after the authority had shut down.
    assert error.provider == "gemini"
    assert error.attempt == 2
    assert "retry chain" in str(error)


async def test_cancellation_records_the_attempt_that_was_interrupted() -> None:
    adapter = SlowAdapter(delay=0.5)
    gateway = _gateway([adapter])
    task = asyncio.create_task(gateway.execute(_request()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    record = list(_sink(gateway).records)[0]
    assert record.outcome == OUTCOME_CANCELLED
    payload = record.as_dict()
    assert payload["attempt_outcomes"][-1]["outcome"] == "cancelled"


async def test_many_concurrent_withdrawals_do_not_leak_slots_or_tasks() -> None:
    adapter = SlowAdapter(delay=0.2)
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=3,
            max_inflight_per_provider=3,
            max_inflight_per_tenant=3,
            max_queued=32,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    before = _task_count()
    tokens = [asyncio.Event() for _ in range(30)]
    tasks = [
        asyncio.create_task(
            gateway.execute(
                _request(
                    prompt=str(index),
                    caller=Caller(CallerCategory.AGENT, "w", tenant_id=index),
                    cancellation=token,
                )
            )
        )
        for index, token in enumerate(tokens)
    ]
    await asyncio.sleep(0.05)
    for token in tokens[::2]:
        token.set()
    for task in tasks[1::2]:
        task.cancel()
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
    assert all(
        isinstance(outcome, (LLMResponse, LLMError, asyncio.CancelledError)) for outcome in outcomes
    )
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0
    await asyncio.sleep(0)
    assert _task_count() <= before + 1
