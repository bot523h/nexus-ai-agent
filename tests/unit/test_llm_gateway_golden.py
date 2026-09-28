"""Golden hardening: deterministic schedules, not probabilistic sleep races."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_llm_gateway_engine import NO_RETRY, ScriptedAdapter, VirtualClock, _gateway

from nexus_ai_agent.llm.errors import (
    GatewayClosedError,
    LLMError,
    LLMErrorKind,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway import registry
from nexus_ai_agent.llm.gateway.adapters import AdapterResult
from nexus_ai_agent.llm.gateway.contract import Caller, CallerCategory, LLMRequest
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.policy import CircuitPolicy, RetryPolicy, default_policy

CALLER = Caller(category=CallerCategory.AGENT, name="golden")


def request(prompt="hello", **kwargs):
    return LLMRequest(caller=CALLER, prompt=prompt, **kwargs)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "raw", "invalid", "timeout"])
async def test_probe_a_never_releases_probe_b(outcome):
    clock = VirtualClock()
    entered = {name: asyncio.Event() for name in ("a", "b")}
    release = {name: asyncio.Event() for name in ("a", "b")}

    class Controlled(ScriptedAdapter):
        async def execute(self, req, *args, **kwargs):
            entered[req.prompt].set()
            await release[req.prompt].wait()
            if req.prompt == "a":
                if outcome == "failure":
                    raise TransientProviderError("synthetic")
                if outcome == "raw":
                    raise ValueError("synthetic")
                if outcome == "invalid":
                    return object()
                if outcome == "timeout":
                    raise asyncio.TimeoutError()
            return AdapterResult(text="ok")

    gateway = _gateway(
        [Controlled("p", ["ok"])],
        clock=clock,
        retry=NO_RETRY,
        circuit=CircuitPolicy(
            failure_threshold=1, recovery_seconds=1, half_open_max_probes=2, success_threshold=2
        ),
    )
    breaker = gateway._breaker("p/m")
    breaker.record_failure(LLMErrorKind.NETWORK)
    clock.advance(2)
    a = asyncio.create_task(gateway.execute(request("a")))
    b = asyncio.create_task(gateway.execute(request("b")))
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 2)
        assert breaker.snapshot()["probes_in_flight"] == 2
        if outcome == "cancel":
            a.cancel()
        else:
            release["a"].set()
        await asyncio.gather(a, return_exceptions=True)
        assert not b.done()
        assert breaker.snapshot()["probes_in_flight"] == 1
    finally:
        release["b"].set()
        await asyncio.gather(a, b, return_exceptions=True)
        await gateway.aclose()
    assert breaker.snapshot()["probes_in_flight"] == 0


async def test_retry_must_reacquire_breaker_permission():
    adapter = ScriptedAdapter("p", [TransientProviderError("synthetic")])
    gateway = _gateway(
        [adapter],
        retry=RetryPolicy(max_attempts=5, base_delay_seconds=0, max_delay_seconds=0),
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=60),
    )
    try:
        with pytest.raises(LLMError):
            await gateway.execute(request())
        assert adapter.calls == 1, "retry bypassed an OPEN breaker"
    finally:
        await gateway.aclose()


async def test_concurrent_installers_cannot_replace_live_authority(monkeypatch):
    monkeypatch.setattr(registry, "_gateway", None)
    gateways = [LLMGateway(policy=default_policy(routes=())) for _ in range(8)]
    barrier = Barrier(len(gateways))

    def install(gateway):
        barrier.wait(timeout=3)
        try:
            registry.install_llm_gateway(gateway)
            return gateway
        except LLMError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(install, gateways))
    try:
        winners = [g for g in results if g is not None]
        assert len(winners) == 1
        assert registry.get_llm_gateway() is winners[0]
    finally:
        for gateway in gateways:
            await gateway.aclose()


async def test_reset_revokes_stale_reference(monkeypatch):
    old = _gateway([ScriptedAdapter("p", ["ok"])])
    monkeypatch.setattr(registry, "_gateway", old)
    registry.reset_llm_gateway()
    try:
        with pytest.raises(GatewayClosedError):
            await old.execute(request())
    finally:
        await old.aclose()


async def test_closed_attempt_does_not_release_later_probe():
    clock = VirtualClock()
    gateway = _gateway(
        [ScriptedAdapter("p", ["ok"])],
        clock=clock,
        circuit=CircuitPolicy(failure_threshold=1, recovery_seconds=1),
    )
    breaker = gateway._breaker("p/m")
    # Permit ownership must survive a CLOSED -> OPEN -> HALF_OPEN transition.
    closed = breaker.acquire()
    breaker.record_failure(LLMErrorKind.NETWORK)
    clock.advance(2)
    probe = breaker.acquire()
    assert closed is not None and probe is not None
    closed.release()
    closed.release()  # repeated cleanup is not another release
    assert breaker.snapshot()["probes_in_flight"] == 1
    probe.release()
    probe.release()
    assert breaker.snapshot()["probes_in_flight"] == 0
    await gateway.aclose()


async def test_reset_refuses_active_work_then_restart_revokes_old(monkeypatch):
    from nexus_ai_agent.llm.errors import GatewayInternalError

    entered, release = asyncio.Event(), asyncio.Event()

    class Held(ScriptedAdapter):
        async def execute(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return AdapterResult(text="ok")

    old = _gateway([Held("p", ["ok"])])
    monkeypatch.setattr(registry, "_gateway", old)
    task = asyncio.create_task(old.execute(request()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(GatewayInternalError):
            registry.reset_llm_gateway()
        assert registry.get_llm_gateway() is old
    finally:
        release.set()
        await task
    await old.aclose()
    fresh = _gateway([ScriptedAdapter("p", ["new"])])
    registry.install_llm_gateway(fresh)
    assert (await registry.get_llm_gateway().execute(request())).text == "new"
    with pytest.raises(GatewayClosedError):
        await old.execute(request())
    await fresh.aclose()


def test_execution_cannot_cross_event_loops():
    from nexus_ai_agent.llm.errors import GatewayInternalError

    gateway = _gateway([ScriptedAdapter("p", ["ok"])])
    asyncio.run(gateway.execute(request()))
    try:
        with pytest.raises(GatewayInternalError, match="event loop"):
            asyncio.run(gateway.execute(request()))
    finally:
        asyncio.run(gateway.aclose())


async def test_worker_must_not_execute_inherited_authority(monkeypatch):
    import nexus_ai_agent.llm.gateway.engine as engine
    from nexus_ai_agent.llm.errors import GatewayInternalError

    gateway = _gateway([ScriptedAdapter("p", ["ok"])])
    monkeypatch.setattr(engine.os, "getpid", lambda: gateway._owner_pid + 1)
    with pytest.raises(GatewayInternalError, match="worker process"):
        await gateway.execute(request())
    await gateway.aclose()


async def test_late_success_cannot_heal_new_breaker_generation():
    gateway = _gateway([ScriptedAdapter("p", ["ok"])], circuit=CircuitPolicy(failure_threshold=1))
    breaker = gateway._breaker("p/m")
    old = breaker.acquire()
    assert old is not None
    breaker.record_failure(LLMErrorKind.NETWORK)
    old.success(0)
    assert breaker.is_open()
    await gateway.aclose()


async def test_failed_scheduler_cleanup_does_not_release_another_probe(monkeypatch):
    gateway = _gateway(
        [ScriptedAdapter("p", ["ok"])],
        retry=NO_RETRY,
        circuit=CircuitPolicy(
            failure_threshold=1, recovery_seconds=1, half_open_max_probes=2, success_threshold=3
        ),
        clock=VirtualClock(),
    )
    breaker = gateway._breaker("p/m")
    breaker.record_failure(LLMErrorKind.NETWORK)
    gateway._clock.advance(2)
    other = breaker.acquire()
    original_release = gateway._scheduler.release

    def fail_after_release(admission):
        original_release(admission)
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(gateway._scheduler, "release", fail_after_release)
    from nexus_ai_agent.llm.errors import GatewayInternalError

    with pytest.raises(GatewayInternalError, match="scheduler cleanup failed"):
        await gateway.execute(request())
    assert gateway.metrics.requests == 1
    assert gateway.metrics.outcomes["error"] == 1
    assert breaker.snapshot()["probes_in_flight"] == 1
    other.release()
    assert breaker.snapshot()["probes_in_flight"] == 0
    await gateway.aclose()


async def test_idempotent_waiter_has_its_own_deadline():
    from nexus_ai_agent.llm.errors import DeadlineExceededError

    entered, release = asyncio.Event(), asyncio.Event()

    class Held(ScriptedAdapter):
        async def execute(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return AdapterResult(text="ok")

    gateway = _gateway([Held("p", ["ok"])])
    owner = asyncio.create_task(gateway.execute(request(idempotency_key="same")))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(DeadlineExceededError):
            await asyncio.wait_for(
                gateway.execute(request(idempotency_key="same", deadline_seconds=0.01)), 0.3
            )
        assert not owner.done()
    finally:
        release.set()
        await owner
        await gateway.aclose()


def test_idempotency_hash_distinguishes_equal_length_binary_payloads():
    from nexus_ai_agent.llm.gateway.contract import ContentPart, Message
    from nexus_ai_agent.llm.gateway.engine import _scoped_idempotency_key

    for location in ("parts", "messages"):

        def key(data, location=location):
            part = ContentPart(mime_type="image/png", data=data)
            payload = (part,) if location == "parts" else (Message("user", parts=(part,)),)
            return _scoped_idempotency_key(request(idempotency_key="same", **{location: payload}))

        assert key(b"image-a") != key(b"image-b")


def test_internal_error_logging_never_serializes_exception_chain():
    from nexus_ai_agent.llm.errors import GatewayInternalError
    from nexus_ai_agent.llm.gateway.contract import LLMOperation
    from nexus_ai_agent.llm.gateway.observability import StructlogSink, build_error_record

    class Logger:
        def error(self, event, **fields):
            self.fields = fields
            self.called = True

    logger = Logger()
    logger.called = False
    try:
        raise ValueError("prompt-secret credential-secret")
    except ValueError:
        record = build_error_record(
            request_id="test",
            caller=CALLER,
            purpose="chat",
            operation=LLMOperation.CHAT,
            outcome="error",
            error=GatewayInternalError("safe category"),
        )
        StructlogSink(logger).emit(record)
    assert logger.called
    assert not logger.fields.get("exc_info"), "traceback can leak a raw provider secret"


async def test_abandoned_provider_blocks_new_execution_until_it_settles():
    from nexus_ai_agent.llm.errors import OverloadedError

    entered, release = asyncio.Event(), asyncio.Event()

    class Stubborn(ScriptedAdapter):
        async def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                return AdapterResult(text="unexpected second execution")
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return AdapterResult(text="ok")

    adapter = Stubborn("p", ["ok"])
    gateway = _gateway([adapter], retry=NO_RETRY)
    task = asyncio.create_task(gateway.execute(request()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    try:
        for _ in range(40):
            with pytest.raises(OverloadedError):
                await gateway.execute(request())
        assert adapter.calls == 1
    finally:
        release.set()
        await asyncio.gather(*tuple(gateway._abandoned), return_exceptions=True)
        await gateway.aclose()


def test_idempotency_token_is_not_plaintext_telemetry():
    from nexus_ai_agent.llm.gateway.contract import LLMOperation
    from nexus_ai_agent.llm.gateway.observability import RequestRecord

    secret = "private-personal-data-not-a-known-key-pattern"
    record = RequestRecord(
        request_id="test",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome="success",
        idempotency_key=secret,
    )
    assert secret not in repr(record.as_dict())


async def test_cancellation_immediately_after_retry_decision_stops_execution(monkeypatch):
    from nexus_ai_agent.llm.errors import CancelledByCallerError

    token = asyncio.Event()
    adapter = ScriptedAdapter("p", [TransientProviderError("synthetic"), "wrong retry"])
    gateway = _gateway([adapter])
    original = gateway._next_delay

    def decision(*args, **kwargs):
        delay = original(*args, **kwargs)
        token.set()
        return delay

    monkeypatch.setattr(gateway, "_next_delay", decision)
    with pytest.raises(CancelledByCallerError):
        await gateway.execute(request(cancellation=token))
    assert adapter.calls == 1
    assert gateway.status()["scheduler"]["inflight_global"] == 0
    await gateway.aclose()


async def test_idempotent_waiter_cancellation_does_not_cancel_owner():
    from nexus_ai_agent.llm.errors import CancelledByCallerError

    entered, release, token = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Held(ScriptedAdapter):
        async def execute(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return AdapterResult(text="ok")

    gateway = _gateway([Held("p", ["ok"])])
    owner = asyncio.create_task(gateway.execute(request(idempotency_key="same")))
    await asyncio.wait_for(entered.wait(), 2)
    waiter = asyncio.create_task(
        gateway.execute(request(idempotency_key="same", cancellation=token))
    )
    await asyncio.sleep(0)  # waiter reaches its first suspension, no elapsed-time assumption
    token.set()
    try:
        with pytest.raises(CancelledByCallerError):
            await asyncio.wait_for(waiter, 1)
        assert not owner.done()
    finally:
        release.set()
        await owner
        await gateway.aclose()


async def test_total_pending_requests_are_bounded_even_when_wait_policy_is_enabled():
    from nexus_ai_agent.llm.errors import OverloadedError
    from nexus_ai_agent.llm.gateway.policy import ConcurrencyPolicy, OverloadBehavior

    entered, release = asyncio.Event(), asyncio.Event()

    class Held(ScriptedAdapter):
        async def execute(self, *args, **kwargs):
            self.calls += 1
            entered.set()
            await release.wait()
            return AdapterResult(text="ok")

    adapter = Held("p", ["ok"])
    gateway = _gateway(
        [adapter],
        concurrency=ConcurrencyPolicy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=2,
            overload_behavior=OverloadBehavior.WAIT,
        ),
    )
    # Coalesced waiters do not occupy scheduler slots; the ingress cap must
    # still count them. The same schedule proves no live idempotency eviction.
    tasks = [
        asyncio.create_task(gateway.execute(request(idempotency_key="same"))) for _ in range(50)
    ]
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert gateway._active_calls == 4
        assert adapter.calls == 1
    finally:
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        await gateway.aclose()
    assert sum(isinstance(o, OverloadedError) for o in outcomes) == 46


def test_observer_registration_has_a_finite_bound():
    from nexus_ai_agent.llm.gateway.engine import _MAX_SINKS

    class Sink:
        def emit(self, record):
            pass

    gateway = LLMGateway(policy=default_policy(routes=()), attach_default_sink=False)
    for _ in range(_MAX_SINKS):
        gateway.add_sink(Sink())
    with pytest.raises(ValueError, match="sinks"):
        gateway.add_sink(Sink())
    assert len(gateway._sinks) == _MAX_SINKS


@pytest.mark.parametrize(
    "order", [("registry", "facade", "engine"), ("engine", "facade", "registry")]
)
def test_fresh_worker_import_order_does_not_install_an_authority(order):
    import subprocess
    import sys

    script = f"""
import importlib
for name in {order!r}:
    importlib.import_module('nexus_ai_agent.llm.gateway.' + name)
from nexus_ai_agent.llm.gateway import registry
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.policy import default_policy
assert registry._gateway is None
built = []
def factory(settings):
    gateway = LLMGateway(policy=default_policy(routes=()), attach_default_sink=False)
    built.append(gateway)
    return gateway
registry.build_gateway_from_settings = factory
assert registry.get_llm_gateway() is registry.get_llm_gateway()
assert len(built) == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


async def test_canonical_routing_factory_never_enters_a_nested_gateway(monkeypatch):
    import sys
    from types import SimpleNamespace

    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.llm.litellm_provider import build_llm_provider

    class Router:
        def __init__(self, **kwargs):
            self.calls = 0

        async def acompletion(self, **kwargs):
            self.calls += 1
            return {"choices": [{"message": {"content": "canonical"}}]}

    # Importing the real SDK before its offline switch triggers a price-map
    # download and retry jitter. This is a composition test, not an SDK smoke.
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(Router=Router))
    monkeypatch.setattr(registry, "_gateway", None)
    settings = Settings(
        _env_file=None,
        NEXUS_OLLAMA_MODEL="synthetic",
        GEMINI_API_KEY=None,
        GROQ_API_KEY=None,
        OPENROUTER_API_KEY=None,
        NEXUS_MODEL_PATH="/nonexistent/model.gguf",
    )
    first, _ = build_llm_provider(settings)
    second, _ = build_llm_provider(settings)
    authority = registry.get_llm_gateway()
    try:
        assert first.authority() is second.authority() is authority
        assert await first.generate("hello") == "canonical"
        assert authority.metrics.requests == 1
        legacy = authority.adapter("routing-embedding").inner
        assert legacy._gateway is None  # no inner authority was constructed
        assert await first.embed("same") == await legacy.embed("same")
        assert authority.policy.retry.max_attempts == 1
    finally:
        await authority.aclose()


async def test_retry_after_above_backoff_budget_is_refused_never_shortened():
    from nexus_ai_agent.llm.errors import RateLimitedError

    adapter = ScriptedAdapter("p", [RateLimitedError("synthetic", retry_after=9), "too early"])
    gateway = _gateway([adapter], clock=VirtualClock())
    sleeps = []

    async def sleep(delay, cancellation=None):
        sleeps.append(delay)
        gateway._clock.advance(delay)
        return delay

    gateway._sleep = sleep
    try:
        with pytest.raises(RateLimitedError):
            await gateway.execute(request())
        assert adapter.calls == 1
        assert sleeps == []
    finally:
        await gateway.aclose()


async def test_idempotent_callers_each_have_a_record_without_duplicate_usage():
    from nexus_ai_agent.llm.gateway.contract import Usage, UsageSource

    adapter = ScriptedAdapter(
        "p",
        ["ok"],
        delay=0.01,
        usage=Usage(source=UsageSource.PROVIDER, input_tokens=3, output_tokens=2, total_tokens=5),
    )
    gateway = _gateway([adapter])
    try:
        a, b = await asyncio.gather(
            *(gateway.execute(request(idempotency_key="same")) for _ in range(2))
        )
        c = await gateway.execute(request(idempotency_key="same"))
        assert len({a.request_id, b.request_id, c.request_id}) == 3
        assert gateway.metrics.requests == 3
        assert gateway.metrics.attempts == 1
        assert gateway.metrics.usage_input_tokens == 3
        assert adapter.calls == 1
    finally:
        await gateway.aclose()


async def test_invalid_structured_output_has_one_failure_record():
    gateway = _gateway([ScriptedAdapter("p", ["not json"])])

    def invalid(text):
        raise ValueError("synthetic")

    try:
        with pytest.raises(LLMError):
            await gateway.execute(request(output_validator=invalid))
        assert gateway.metrics.requests == 1
    finally:
        await gateway.aclose()


async def test_hostile_provider_labels_do_not_create_unbounded_metric_keys():
    gateway = _gateway([ScriptedAdapter("p", ["ok"])])
    try:
        for index in range(300):
            with pytest.raises(LLMError):
                await gateway.execute(request(provider=f"unregistered-{index}"))
        assert len(gateway.metrics.provider_errors) <= 129
    finally:
        await gateway.aclose()


def test_error_classification_sets_offline_pricing_before_sdk_import(monkeypatch):
    import builtins
    import os
    from types import SimpleNamespace

    from nexus_ai_agent.llm import litellm_provider

    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    monkeypatch.setattr(litellm_provider, "_LITELLM_KINDS", None)
    real_import = builtins.__import__
    seen = []

    def importing(name, *args, **kwargs):
        if name == "litellm":
            seen.append(os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
            return SimpleNamespace(exceptions=SimpleNamespace())
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    litellm_provider._litellm_error_types()
    assert seen == ["True"]
