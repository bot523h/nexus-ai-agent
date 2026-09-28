"""W2 — adversarial testing of the authority (LAW 6, LAW 14, LAW 15).

Everything here is a component behaving *badly* on purpose: a hostile or buggy
adapter, a hostile provider payload, a caller that sends nonsense, a clock that
steps backwards, an rng that lies, a sink that explodes. The invariant under test
is always the same:

    the gateway stays typed, bounded and honest — it never crashes with an
    unclassified exception, never grows without limit, never invents a number,
    and never leaks what it was asked to keep private.

Nothing in this file mocks the gateway itself; only its neighbours misbehave.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    CancelledByCallerError,
    GatewayInternalError,
    LLMError,
    LLMErrorKind,
    MalformedResponseError,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway.adapters import AdapterResult
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    ContentPart,
    FinishReason,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    Message,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.observability import CollectingSink
from nexus_ai_agent.llm.gateway.policy import (
    ConcurrencyPolicy,
    FallbackPolicy,
    OverloadBehavior,
    RetryPolicy,
    Route,
    TimeoutBudget,
    default_policy,
)

CALLER = Caller(category=CallerCategory.AGENT, name="test.adversarial")
NO_RETRY = RetryPolicy(max_attempts=1)
TEXT_OPS = frozenset({LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION})


class HostileAdapter:
    """An adapter whose behaviour the test dictates, including illegal behaviour."""

    def __init__(self, behaviour: Any, name: str = "gemini") -> None:
        self._behaviour = behaviour
        self._name = name
        self.calls = 0
        self.closed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def operations(self) -> Any:
        return TEXT_OPS

    @property
    def modalities(self) -> Any:
        from nexus_ai_agent.llm.gateway.contract import Modality

        return frozenset({Modality.TEXT})

    async def execute(
        self, request: LLMRequest, route: Route, *, budget: Any, request_id: str, attempt: int
    ) -> Any:
        self.calls += 1
        behaviour = self._behaviour
        if callable(behaviour):
            behaviour = behaviour(request, attempt)
        if isinstance(behaviour, BaseException):
            raise behaviour
        if asyncio.iscoroutine(behaviour):
            return await behaviour
        return behaviour

    async def aclose(self) -> None:
        self.closed += 1


class SettledFutureAdapter:
    """An adapter whose ``execute`` returns an already-settled *future*.

    ``asyncio.Task.__step`` re-raises ``KeyboardInterrupt``/``SystemExit`` into
    the running loop, so an interpreter signal raised *inside* a task never
    reaches the engine's "do not convert this" branch — the loop takes it first
    (which is what the sibling test above proves end to end). A plain future can
    carry a ``BaseException`` to ``future.exception()`` without the loop ever
    seeing it, which is the exact shape that branch defends against: a provider
    coroutine wrapper, a shielded task, or a third-party future that stored a
    signal instead of raising it.
    """

    def __init__(self, exc: BaseException, name: str = "gemini") -> None:
        self._exc = exc
        self._name = name
        self.calls = 0
        self.closed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def operations(self) -> Any:
        return TEXT_OPS

    @property
    def modalities(self) -> Any:
        from nexus_ai_agent.llm.gateway.contract import Modality

        return frozenset({Modality.TEXT})

    def execute(
        self, request: LLMRequest, route: Route, *, budget: Any, request_id: str, attempt: int
    ) -> Any:
        self.calls += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.set_exception(self._exc)
        return future

    async def aclose(self) -> None:
        self.closed += 1


def _gateway(adapter: Any, **policy_kwargs: Any) -> LLMGateway:
    route = Route(provider=adapter.name, model="m")
    retry = policy_kwargs.pop("retry", NO_RETRY)
    engine_kwargs: dict[str, Any] = {}
    for name in ("clock", "rng"):
        if name in policy_kwargs:
            engine_kwargs[name] = policy_kwargs.pop(name)
    policy = default_policy(routes=[route], retry=retry, **policy_kwargs)
    collector = CollectingSink()
    gateway = LLMGateway(
        policy=policy, sinks=[collector], attach_default_sink=False, **engine_kwargs
    )
    gateway.register(adapter, [route])
    gateway._test_sink = collector  # type: ignore[attr-defined]
    return gateway


def _sink(gateway: LLMGateway) -> CollectingSink:
    return gateway._test_sink  # type: ignore[attr-defined,no-any-return]


def _request(**kwargs: Any) -> LLMRequest:
    base: dict[str, Any] = {"caller": CALLER, "prompt": "hello"}
    base.update(kwargs)
    return LLMRequest(**base)


def _result(text: str = "answer", **kwargs: Any) -> AdapterResult:
    base: dict[str, Any] = {"text": text, "finish_reason": FinishReason.STOP}
    base.update(kwargs)
    return AdapterResult(**base)


# ═══════════════════════════════════════════════════════════════════════════
# A hostile or buggy adapter
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raised",
    [
        RuntimeError("plain bug"),
        ValueError("bad shape"),
        KeyError("missing"),
        ZeroDivisionError("math"),
        RecursionError("depth"),
        MemoryError("allocation"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    ],
)
async def test_any_exception_an_adapter_raises_becomes_a_typed_gateway_error(
    raised: Exception,
) -> None:
    """The caller's ``except LLMError`` must work no matter what breaks inside."""

    gateway = _gateway(HostileAdapter(raised))
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.kind is LLMErrorKind.GATEWAY_INTERNAL
    assert excinfo.value.retryable is False
    assert type(raised).__name__ in str(excinfo.value)


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
def test_interpreter_signals_are_never_swallowed_as_gateway_failures(raised: type) -> None:
    """Ctrl-C must still mean Ctrl-C, even from inside an adapter.

    This one cannot be an ``async`` test. ``asyncio.Task.__step`` deliberately
    re-raises ``KeyboardInterrupt``/``SystemExit`` into the running loop *after*
    storing them on the task, so the signal escapes any ``try`` inside the
    coroutine and pytest reads it as the operator aborting the session. Running
    the scenario on a private loop in a synchronous test lets us catch it here.
    """

    async def scenario() -> type | None:
        gateway = _gateway(HostileAdapter(raised()))
        try:
            await gateway.execute(_request())
        except BaseException as exc:  # noqa: BLE001 — that is exactly the point
            return type(exc)
        return None

    loop = asyncio.new_event_loop()
    try:
        observed: type | None = loop.run_until_complete(scenario())
    except BaseException as exc:  # noqa: BLE001 — the loop re-raised the signal
        observed = type(exc)
    finally:
        loop.close()
    assert observed is raised, f"{raised.__name__} became {observed}"


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
async def test_a_signal_carried_by_the_provider_future_is_never_converted(raised: type) -> None:
    """The engine's own branch: a ``BaseException`` is re-raised, never wrapped.

    Wrapping it would turn Ctrl-C into an ordinary provider failure — the caller
    would retry, the operator would see a "transient" error, and the process
    would refuse to die (LAW 5: stopping means stopping).
    """

    adapter = SettledFutureAdapter(raised())
    gateway = _gateway(adapter)
    with pytest.raises(raised):
        await gateway.execute(_request())
    assert adapter.calls == 1  # and it was never retried


async def test_an_adapter_returning_the_wrong_type_is_a_typed_gateway_error() -> None:
    for bad in ("just a string", 42, None, {"text": "answer"}, ["a", "b"], object()):
        gateway = _gateway(HostileAdapter(bad))
        with pytest.raises(GatewayInternalError):
            await gateway.execute(_request())


async def test_an_adapter_returning_wrong_typed_fields_is_contained() -> None:
    """Field-level defence: a bad ``text`` or ``usage`` cannot crash the record."""

    result = AdapterResult(
        text=12345,  # type: ignore[arg-type]
        finish_reason=FinishReason.STOP,
        usage="not a usage object",  # type: ignore[arg-type]
    )
    gateway = _gateway(HostileAdapter(result))
    response = await gateway.execute(_request())
    assert response.text == ""
    assert response.usage.source is UsageSource.UNKNOWN


async def test_an_adapter_that_hangs_is_cut_off_by_the_attempt_budget() -> None:
    async def hang() -> None:
        await asyncio.sleep(3600)

    gateway = _gateway(
        HostileAdapter(hang()),
        timeout=TimeoutBudget(
            total_seconds=1.0,
            queue_wait_seconds=0.2,
            connect_seconds=0.05,
            read_seconds=0.1,
            per_attempt_seconds=0.2,
            max_backoff_seconds=0.05,
        ).validate(),
    )
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    assert excinfo.value.kind in {LLMErrorKind.UPSTREAM_TIMEOUT, LLMErrorKind.DEADLINE_EXCEEDED}


async def test_an_adapter_that_swallows_cancellation_cannot_overrun_the_budget() -> None:
    """A provider that ignores cancellation is a real-world failure mode."""

    async def stubborn() -> AdapterResult:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # pretend not to notice
            return _result("late answer")
        return _result("answer")

    gateway = _gateway(
        HostileAdapter(stubborn()),
        timeout=TimeoutBudget(
            total_seconds=1.0,
            queue_wait_seconds=0.2,
            connect_seconds=0.05,
            read_seconds=0.1,
            per_attempt_seconds=0.2,
            max_backoff_seconds=0.05,
        ).validate(),
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request())
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 2.0
    # The adapter answered *after* its cap. ``asyncio.wait_for`` cannot enforce a
    # deadline against a coroutine that swallows cancellation, so the engine
    # checks the budget when the answer lands and reports the overrun instead of
    # handing back a late answer as if the deadline had been kept.
    assert excinfo.value.kind in {LLMErrorKind.UPSTREAM_TIMEOUT, LLMErrorKind.DEADLINE_EXCEEDED}


async def test_an_adapter_that_raises_from_aclose_does_not_stop_the_shutdown() -> None:
    class ExplodingClose(HostileAdapter):
        async def aclose(self) -> None:
            self.closed += 1
            raise RuntimeError("close is broken")

    first = ExplodingClose(_result(), name="a")
    second = HostileAdapter(_result(), name="b")
    policy = default_policy(
        routes=[Route(provider="a", model="m"), Route(provider="b", model="m")], retry=NO_RETRY
    )
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(first, [Route(provider="a", model="m")])
    gateway.register(second, [Route(provider="b", model="m")])
    await gateway.aclose()  # must not raise
    assert first.closed == 1
    assert second.closed == 1
    assert gateway.closed is True


async def test_an_adapter_that_returns_a_huge_answer_keeps_the_record_bounded() -> None:
    gateway = _gateway(HostileAdapter(_result("x" * 2_000_000)))
    response = await gateway.execute(_request())
    assert len(response.text) == 2_000_000  # the caller gets what the provider said
    record = list(_sink(gateway).records)[0]
    blob = json.dumps(record.as_dict(), default=str)
    assert len(blob) < 8192  # the *record* stays small: sizes, not content
    assert "xxx" not in blob


async def test_an_adapter_that_returns_invalid_unicode_still_yields_a_safe_record() -> None:
    gateway = _gateway(HostileAdapter(_result("answer \ud800 lone surrogate")))
    response = await gateway.execute(_request())
    assert "answer" in response.text
    record = list(_sink(gateway).records)[0]
    assert json.dumps(record.as_dict(), default=str)  # must not raise


async def test_a_failing_adapter_is_never_retried_when_the_kind_says_not_to() -> None:
    adapter = HostileAdapter(RuntimeError("bug"))
    gateway = _gateway(adapter, retry=RetryPolicy(max_attempts=5))
    with pytest.raises(GatewayInternalError):
        await gateway.execute(_request())
    assert adapter.calls == 1  # a gateway bug is not a transient provider fault


async def test_a_transient_failure_from_a_hostile_adapter_is_still_retried() -> None:
    adapter = HostileAdapter(TransientProviderError("down", status_code=503))
    gateway = _gateway(
        adapter,
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.001, max_delay_seconds=0.002),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
    )
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert adapter.calls == 3


# ═══════════════════════════════════════════════════════════════════════════
# A hostile caller
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_absurd_prompt_is_passed_through_and_measured_not_stored() -> None:
    prompt = "a" * 5_000_000
    gateway = _gateway(HostileAdapter(_result("ok")))
    response = await gateway.execute(_request(prompt=prompt))
    assert response.text == "ok"
    record = list(_sink(gateway).records)[0]
    # The record measures the request instead of carrying it: ``prompt_chars``
    # counts text volume, ``payload_bytes`` counts binary parts (there are none).
    assert record.prompt_chars == len(prompt)
    assert record.payload_bytes == 0
    assert prompt not in json.dumps(record.as_dict(), default=str)


async def test_a_prompt_of_only_control_characters_and_emoji_is_handled() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    response = await gateway.execute(_request(prompt="\x00\x01\ud83d\ude00" * 100))
    assert response.text == "ok"
    assert json.dumps(list(_sink(gateway).records)[0].as_dict(), default=str)


async def test_a_caller_cannot_supply_an_anonymous_identity() -> None:
    with pytest.raises((TypeError, ValueError)):
        LLMRequest(prompt="hello")  # type: ignore[call-arg]


async def test_an_overlong_caller_name_is_refused() -> None:
    with pytest.raises(ValueError):
        Caller(category=CallerCategory.AGENT, name="x" * 500)


async def test_a_negative_deadline_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        _request(deadline_seconds=-1.0)


async def test_an_absurd_generation_parameter_is_refused_or_clamped_not_sent() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    with pytest.raises((ValueError, LLMError)):
        await gateway.execute(
            _request(generation=GenerationParams(temperature=99.0, max_output_tokens=-5))
        )


async def test_hostile_metadata_cannot_bloat_or_leak_through_a_record() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    metadata = {
        "prompt": "the user's private prompt",
        "api_key": "AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMYDUMMY00",
        **{f"k{i}": "v" * 1000 for i in range(200)},
    }
    await gateway.execute(_request(metadata=metadata))
    blob = json.dumps(list(_sink(gateway).records)[0].as_dict(), default=str)
    assert len(blob) < 8192
    assert "private prompt" not in blob
    assert "AIzaSyDUMMY" not in blob


async def test_a_tiny_deadline_on_a_busy_gateway_fails_typed_without_spinning() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    with pytest.raises(LLMError) as excinfo:
        await gateway.execute(_request(deadline_seconds=0.000001))
    assert excinfo.value.kind is LLMErrorKind.DEADLINE_EXCEEDED


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency keys are caller-supplied strings — they must not leak answers
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_same_key_from_two_tenants_does_not_share_an_answer() -> None:
    """A shared cache keyed on a caller string would be a cross-tenant leak."""

    answers = iter(["answer for tenant one", "answer for tenant two"])

    def behaviour(request: LLMRequest, attempt: int) -> AdapterResult:
        return _result(next(answers))

    gateway = _gateway(HostileAdapter(behaviour))
    first = await gateway.execute(
        _request(
            prompt="summarise A",
            caller=Caller(CallerCategory.AGENT, "a", tenant_id=1),
            idempotency_key="summarize",
        )
    )
    second = await gateway.execute(
        _request(
            prompt="summarise B",
            caller=Caller(CallerCategory.AGENT, "b", tenant_id=2),
            idempotency_key="summarize",
        )
    )
    assert first.text == "answer for tenant one"
    assert second.text == "answer for tenant two"


async def test_the_same_key_with_a_different_prompt_does_not_return_a_stale_answer() -> None:
    answers = iter(["first answer", "second answer"])
    gateway = _gateway(HostileAdapter(lambda request, attempt: _result(next(answers))))
    first = await gateway.execute(_request(prompt="question one", idempotency_key="k"))
    second = await gateway.execute(_request(prompt="question two", idempotency_key="k"))
    assert (first.text, second.text) == ("first answer", "second answer")


async def test_the_same_key_and_payload_still_deduplicates() -> None:
    adapter = HostileAdapter(_result("shared answer"))
    gateway = _gateway(adapter)
    first = await gateway.execute(_request(prompt="same", idempotency_key="k"))
    second = await gateway.execute(_request(prompt="same", idempotency_key="k"))
    assert adapter.calls == 1
    assert first.text == second.text == "shared answer"


async def test_an_absurd_idempotency_key_is_bounded() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    await gateway.execute(_request(idempotency_key="k" * 100_000))
    status = gateway.status()["idempotency"]
    assert status["cached"] <= status["capacity"]


# ═══════════════════════════════════════════════════════════════════════════
# A lying clock and a lying rng
# ═══════════════════════════════════════════════════════════════════════════


class SteppingClock:
    """A clock that jumps backwards — what an NTP step or a bad double does."""

    def __init__(self, sequence: list[float]) -> None:
        self._sequence = sequence
        self.index = 0

    def __call__(self) -> float:
        value = self._sequence[min(self.index, len(self._sequence) - 1)]
        self.index += 1
        return value


async def test_a_clock_that_steps_backwards_never_produces_negative_durations() -> None:
    clock = SteppingClock([1000.0, 900.0, 800.0, 700.0, 600.0, 500.0, 400.0, 300.0])
    gateway = _gateway(HostileAdapter(_result("ok")), clock=clock)
    response = await gateway.execute(_request())
    assert response.text == "ok"
    timings = response.timings.as_dict()
    assert all(value >= 0.0 for value in timings.values()), timings
    record = list(_sink(gateway).records)[0]
    assert all(value >= 0.0 for value in record.timings.as_dict().values())
    assert json.dumps(record.as_dict(), default=str)


class LyingRng:
    """An rng that returns values outside ``[0, 1]``."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value

    def uniform(self, low: float, high: float) -> float:
        return self._value * high * 1000.0


async def test_a_lying_rng_cannot_push_a_backoff_past_the_policy_ceiling() -> None:
    adapter = HostileAdapter(TransientProviderError("down", status_code=503))
    slept: list[float] = []
    gateway = _gateway(
        adapter,
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=2.0),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        rng=LyingRng(5.0),
    )
    real_sleep = asyncio.sleep

    async def spy(delay: float, token: Any = None) -> float:
        slept.append(delay)
        await real_sleep(0)
        return delay

    gateway._sleep = spy  # type: ignore[method-assign]
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert slept
    assert all(0.0 <= delay <= 2.0 for delay in slept), slept


async def test_a_negative_rng_draw_cannot_produce_a_negative_sleep() -> None:
    adapter = HostileAdapter(TransientProviderError("down", status_code=503))
    slept: list[float] = []
    gateway = _gateway(
        adapter,
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.5, max_delay_seconds=2.0),
        fallback=FallbackPolicy(enabled=False, max_hops=0),
        rng=LyingRng(-1.0),
    )
    real_sleep = asyncio.sleep

    async def spy(delay: float, token: Any = None) -> float:
        slept.append(delay)
        await real_sleep(0)
        return delay

    gateway._sleep = spy  # type: ignore[method-assign]
    with pytest.raises(TransientProviderError):
        await gateway.execute(_request())
    assert all(delay >= 0.0 for delay in slept), slept


# ═══════════════════════════════════════════════════════════════════════════
# Hostile observation sinks
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_sink_that_raises_on_every_record_cannot_break_a_request() -> None:
    class AlwaysExploding:
        def emit(self, record: Any) -> None:
            raise RuntimeError("sink down")

    adapter = HostileAdapter(_result("ok"))
    route = Route(provider="gemini", model="m")
    policy = default_policy(routes=[route], retry=NO_RETRY)
    gateway = LLMGateway(policy=policy, sinks=[AlwaysExploding()], attach_default_sink=False)
    gateway.register(adapter, [route])
    response = await gateway.execute(_request())
    assert response.text == "ok"


async def test_a_sink_that_mutates_the_record_cannot_corrupt_the_metrics_buffer() -> None:
    class Mutating:
        def emit(self, record: Any) -> None:
            record.provider = "tampered"
            record.purpose = "x" * 10_000

    adapter = HostileAdapter(_result("ok"))
    route = Route(provider="gemini", model="m")
    policy = default_policy(routes=[route], retry=NO_RETRY)
    gateway = LLMGateway(policy=policy, sinks=[Mutating()], attach_default_sink=False)
    gateway.register(adapter, [route])
    await gateway.execute(_request())
    recent = gateway.recent(limit=5)
    assert len(recent) == 1  # the buffer still holds one bounded record


async def test_a_hundred_sinks_do_not_multiply_the_work() -> None:
    sinks = [CollectingSink(limit=4) for _ in range(100)]
    adapter = HostileAdapter(_result("ok"))
    route = Route(provider="gemini", model="m")
    policy = default_policy(routes=[route], retry=NO_RETRY)
    gateway = LLMGateway(policy=policy, sinks=sinks, attach_default_sink=False)
    gateway.register(adapter, [route])
    await gateway.execute(_request())
    assert adapter.calls == 1
    assert all(len(sink.records) == 1 for sink in sinks)


# ═══════════════════════════════════════════════════════════════════════════
# Bounds under hostile load
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_flood_of_requests_keeps_every_structure_bounded() -> None:
    adapter = HostileAdapter(_result("ok"))
    gateway = _gateway(adapter)
    for index in range(400):
        await gateway.execute(_request(prompt=str(index), idempotency_key=f"key-{index % 50}"))
    status = gateway.status()
    assert status["idempotency"]["cached"] <= status["idempotency"]["capacity"]
    assert status["metrics"]["buffer_size"] <= status["metrics"]["buffer_capacity"]
    assert status["scheduler"]["inflight_global"] == 0
    assert len(gateway.recent(limit=1000)) <= status["metrics"]["buffer_capacity"]


async def test_a_flood_of_failures_keeps_the_error_counters_bounded_in_cardinality() -> None:
    adapter = HostileAdapter(TransientProviderError("down", status_code=503))
    gateway = _gateway(adapter, fallback=FallbackPolicy(enabled=False, max_hops=0))
    for index in range(200):
        with pytest.raises(TransientProviderError):
            await gateway.execute(
                _request(
                    prompt=f"distinct prompt {index}",
                    purpose=f"purpose-{index}",
                    caller=Caller(CallerCategory.AGENT, f"caller-{index}", tenant_id=index),
                )
            )
    metrics = gateway.status()["metrics"]
    assert len(metrics["provider_errors"]) == 1
    assert len(metrics["outcomes"]) == 1
    assert metrics["buffer_size"] <= metrics["buffer_capacity"]


async def test_a_flood_of_cancellations_leaves_no_slot_and_no_task() -> None:
    adapter = HostileAdapter(_result("ok"))
    gateway = _gateway(
        adapter,
        concurrency=ConcurrencyPolicy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=16,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    before = len(asyncio.all_tasks())

    async def slow(request: LLMRequest, attempt: int) -> AdapterResult:
        await asyncio.sleep(0.2)
        return _result("ok")

    gateway.register(HostileAdapter(slow), None)
    tasks = [asyncio.create_task(gateway.execute(_request(prompt=str(i)))) for i in range(60)]
    await asyncio.sleep(0.05)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    snapshot = gateway.status()["scheduler"]
    assert snapshot["inflight_global"] == 0
    assert snapshot["queued"] == 0
    assert len(asyncio.all_tasks()) <= before + 1


async def test_a_cancelled_caller_token_storm_does_not_leak_watcher_tasks() -> None:
    async def slow(request: LLMRequest, attempt: int) -> AdapterResult:
        await asyncio.sleep(0.2)
        return _result("ok")

    gateway = _gateway(HostileAdapter(slow))
    before = len(asyncio.all_tasks())
    tokens = [asyncio.Event() for _ in range(30)]
    tasks = [
        asyncio.create_task(gateway.execute(_request(prompt=str(i), cancellation=token)))
        for i, token in enumerate(tokens)
    ]
    await asyncio.sleep(0.02)
    for token in tokens:
        token.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(outcome, CancelledByCallerError) for outcome in outcomes)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before + 1


async def test_a_malformed_history_cannot_break_the_request() -> None:
    gateway = _gateway(HostileAdapter(_result("ok")))
    response = await gateway.execute(
        _request(
            prompt="",
            messages=(
                Message(role="user", content=""),
                Message(role="assistant", content="\ud800"),
                Message(role="system", content="x" * 100_000),
            ),
        )
    )
    assert response.text == "ok"


async def test_a_request_with_binary_parts_is_measured_but_never_logged() -> None:
    from nexus_ai_agent.llm.gateway.contract import Modality

    class VisionAdapter(HostileAdapter):
        @property
        def modalities(self) -> Any:
            return frozenset({Modality.TEXT, Modality.IMAGE})

    payload = bytes(range(256)) * 400
    adapter = VisionAdapter(_result("ok"))
    route = Route(
        provider="gemini",
        model="m",
        operations=TEXT_OPS,
        modalities=frozenset({Modality.TEXT, Modality.IMAGE}),
    )
    policy = default_policy(routes=[route], retry=NO_RETRY)
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(adapter, [route])
    gateway._test_sink = CollectingSink()  # type: ignore[attr-defined]
    await gateway.execute(_request(parts=(ContentPart(mime_type="image/png", data=payload),)))
    record = list(gateway._sinks[0].records)[0]  # type: ignore[attr-defined]
    blob = json.dumps(record.as_dict(), default=str)
    assert record.payload_bytes == len(payload)  # measured
    assert "\u0000" not in blob  # the bytes themselves were never carried
    assert "AAECA" not in blob  # nor a base64 rendering of them
    assert len(blob) < 8192


async def test_priority_extremes_are_still_served_in_order_under_contention() -> None:
    order: list[str] = []

    def behaviour(request: LLMRequest, attempt: int) -> AdapterResult:
        order.append(request.priority.value)
        return _result("ok")

    gateway = _gateway(
        HostileAdapter(behaviour),
        concurrency=ConcurrencyPolicy(
            max_inflight_global=1,
            max_inflight_per_provider=1,
            max_inflight_per_tenant=1,
            max_queued=32,
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

    async def slow(request: LLMRequest, attempt: int) -> AdapterResult:
        await asyncio.sleep(0.15)
        return _result("ok")

    gateway.register(HostileAdapter(slow), None)
    holder = asyncio.create_task(
        gateway.execute(_request(prompt="holder", caller=Caller(CallerCategory.AGENT, "h", 1)))
    )
    await asyncio.sleep(0.02)
    callers = [
        (LLMPriority.LOW, 2),
        (LLMPriority.OWNER, 3),
        (LLMPriority.NORMAL, 4),
        (LLMPriority.REFERRAL_BONUS, 5),
    ]
    tasks = [
        asyncio.create_task(
            gateway.execute(
                _request(
                    prompt=str(tenant),
                    priority=priority,
                    caller=Caller(CallerCategory.AGENT, "c", tenant),
                )
            )
        )
        for priority, tenant in callers
    ]
    await asyncio.gather(holder, *tasks)
    served = order[1:]  # the holder went first
    # LLMPriority is an IntEnum where a lower value wins.
    assert served == sorted(served, key=lambda value: LLMPriority(value).value)


async def test_a_malformed_provider_answer_is_typed_and_not_retried_forever() -> None:
    adapter = HostileAdapter(MalformedResponseError("no candidates in payload"))
    gateway = _gateway(adapter, retry=RetryPolicy(max_attempts=5))
    with pytest.raises(MalformedResponseError):
        await gateway.execute(_request())
    assert adapter.calls == 1
