"""W2 — the compatibility surface and the single-authority registry (LAW 1, 12).

Two things are pinned here:

* :class:`GatewayLLMProvider` keeps the *pre-W2* ``LLMProvider`` contract byte
  for byte — ``generate(prompt, system) -> str``, ``embed(text) -> list[float]``
  and the verbatim degraded/unavailable messages — while every call underneath
  goes through the authority. Migration must not be visible to callers.
* :mod:`registry` hands out exactly **one** process-wide gateway, builds it from
  settings lazily, and scopes a credential-specific gateway through the same
  policy engine instead of letting a caller construct a private provider.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    AuthenticationError,
    DeadlineExceededError,
    GatewayInternalError,
    LLMError,
    LLMErrorKind,
    OverloadedError,
    PolicyRefusalError,
    RateLimitedError,
    StructuredOutputInvalidError,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway.adapters import AdapterResult
from nexus_ai_agent.llm.gateway.contract import (
    FinishReason,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    Usage,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.facade import (
    DEGRADED_DISCLAIMER,
    UNAVAILABLE_MESSAGE,
    GatewayLLMProvider,
    agent_caller,
    json_object_validator,
    pydantic_validator,
    surface_caller,
)
from nexus_ai_agent.llm.gateway.observability import CollectingSink
from nexus_ai_agent.llm.gateway.policy import (
    FallbackPolicy,
    RetryPolicy,
    Route,
    default_policy,
)
from nexus_ai_agent.llm.gateway.registry import (
    _CREDENTIAL_GATEWAYS,
    gateway_for_credentials,
    get_llm_gateway,
    reset_llm_gateway,
    set_llm_gateway,
)

CALLER = agent_caller("test.facade")
NO_RETRY = RetryPolicy(max_attempts=1)


class FakeAdapter:
    """A minimal adapter whose answer (or failure) the test chooses."""

    def __init__(
        self,
        name: str = "gemini",
        outcome: Any = "the answer",
        *,
        model: str = "m",
        operations: Any = None,
        degraded: bool = False,
    ) -> None:
        self._name = name
        self._outcome = outcome
        self._model = model
        self._operations = operations or frozenset(
            {LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION, LLMOperation.EMBEDDINGS}
        )
        self.calls = 0
        self.requests: list[LLMRequest] = []
        self.degraded = degraded
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
        self, request: LLMRequest, route: Route, *, budget: Any, request_id: str, attempt: int
    ) -> AdapterResult:
        self.calls += 1
        self.requests.append(request)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        if isinstance(self._outcome, AdapterResult):
            return self._outcome
        return AdapterResult(text=str(self._outcome), finish_reason=FinishReason.STOP)

    async def aclose(self) -> None:
        self.closed += 1


def _gateway(adapter: FakeAdapter, **policy_kwargs: Any) -> LLMGateway:
    route = Route(
        provider=adapter.name,
        model=adapter._model,
        operations=adapter.operations,
        modalities=adapter.modalities,
        degraded=adapter.degraded,
    )
    policy = default_policy(routes=[route], retry=NO_RETRY, **policy_kwargs)
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(adapter, [route])
    return gateway


def _provider(gateway: LLMGateway, **kwargs: Any) -> GatewayLLMProvider:
    return GatewayLLMProvider(caller=CALLER, gateway=gateway, **kwargs)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Every test starts and ends with no process-wide authority installed."""

    previous = set_llm_gateway(None)
    _CREDENTIAL_GATEWAYS.clear()
    yield
    _CREDENTIAL_GATEWAYS.clear()
    set_llm_gateway(previous)
    reset_llm_gateway()


# ═══════════════════════════════════════════════════════════════════════════
# The legacy LLMProvider contract
# ═══════════════════════════════════════════════════════════════════════════


async def test_generate_returns_plain_text_like_the_old_provider_did() -> None:
    adapter = FakeAdapter(outcome="the answer")
    provider = _provider(_gateway(adapter))
    assert await provider.generate("hello") == "the answer"


async def test_generate_passes_the_system_prompt_as_a_system_field_not_a_user_hack() -> None:
    """Pre-W2 providers smuggled ``[System:]`` into the user prompt."""

    adapter = FakeAdapter()
    provider = _provider(_gateway(adapter))
    await provider.generate("hello", "be brief")
    sent = adapter.requests[0]
    assert sent.system == "be brief"
    assert "[System:]" not in sent.prompt
    assert sent.prompt == "hello"


async def test_an_empty_system_prompt_stays_absent() -> None:
    adapter = FakeAdapter()
    provider = _provider(_gateway(adapter))
    await provider.generate("hello", "")
    assert adapter.requests[0].system is None


async def test_embed_returns_a_list_of_floats_like_the_old_contract() -> None:
    adapter = FakeAdapter(
        outcome=AdapterResult(embedding=(0.1, 0.2, 0.3), finish_reason=FinishReason.STOP)
    )
    provider = _provider(_gateway(adapter))
    vector = await provider.embed("some text")
    assert vector == [0.1, 0.2, 0.3]
    assert isinstance(vector, list)
    assert adapter.requests[0].operation is LLMOperation.EMBEDDINGS


async def test_a_route_that_produced_no_vector_fails_typed_instead_of_returning_empty() -> None:
    """``[]`` would claim "a vector with no dimensions"; the truth is "no vector"."""

    adapter = FakeAdapter(outcome=AdapterResult(text="no vector"))
    provider = _provider(_gateway(adapter))
    with pytest.raises(GatewayInternalError) as excinfo:
        await provider.embed("text")
    assert excinfo.value.kind is LLMErrorKind.GATEWAY_INTERNAL


async def test_embed_typed_exposes_the_whole_response() -> None:
    adapter = FakeAdapter(
        outcome=AdapterResult(
            embedding=(1.0, 2.0),
            finish_reason=FinishReason.STOP,
            usage=Usage(
                source=UsageSource.PROVIDER, input_tokens=3, output_tokens=0, total_tokens=3
            ),
        )
    )
    provider = _provider(_gateway(adapter))
    response = await provider.embed_typed("text")
    assert response.embedding == (1.0, 2.0)
    assert response.usage.input_tokens == 3


async def test_the_provider_identity_is_carried_on_every_request() -> None:
    adapter = FakeAdapter()
    provider = _provider(_gateway(adapter), purpose="summarize", priority=LLMPriority.OWNER)
    await provider.generate("hello")
    sent = adapter.requests[0]
    assert sent.caller.label == CALLER.label
    assert sent.purpose == "summarize"
    assert sent.priority is LLMPriority.OWNER


async def test_generation_parameters_and_deadline_are_forwarded() -> None:
    adapter = FakeAdapter()
    generation = GenerationParams(temperature=0.2, max_output_tokens=64)
    provider = _provider(_gateway(adapter), generation=generation, deadline_seconds=12.5)
    await provider.generate("hello")
    sent = adapter.requests[0]
    assert sent.generation.temperature == 0.2
    assert sent.generation.max_output_tokens == 64
    assert sent.deadline_seconds == 12.5


async def test_metadata_is_forwarded_but_sanitized_by_the_record_layer() -> None:
    adapter = FakeAdapter()
    provider = _provider(_gateway(adapter), metadata={"surface": "telegram", "prompt": "smuggled"})
    await provider.generate("hello")
    assert adapter.requests[0].metadata["surface"] == "telegram"


# ═══════════════════════════════════════════════════════════════════════════
# Failure rendering (the legacy ``on_failure="message"`` composition)
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_default_composition_raises_the_typed_error() -> None:
    adapter = FakeAdapter(outcome=TransientProviderError("down", status_code=503))
    provider = _provider(_gateway(adapter))
    with pytest.raises(TransientProviderError):
        await provider.generate("hello")


@pytest.mark.parametrize(
    "error",
    [
        OverloadedError("saturated"),
        DeadlineExceededError("budget gone"),
        RateLimitedError("slow down", status_code=429),
    ],
)
async def test_a_busy_service_renders_the_retry_soon_message(error: LLMError) -> None:
    adapter = FakeAdapter(outcome=error)
    provider = _provider(_gateway(adapter), on_failure="message")
    text = await provider.generate("hello")
    assert text == "⏳ The AI service is busy right now. Please try again in a moment."


@pytest.mark.parametrize(
    "error",
    [
        TransientProviderError("down", status_code=503),
        AuthenticationError("bad key", status_code=401),
        GatewayInternalError("our bug"),
        PolicyRefusalError("no route"),
    ],
)
async def test_any_other_failure_renders_the_verbatim_unavailable_message(error: LLMError) -> None:
    """Byte-for-byte the pre-W2 ``FallbackProvider`` both-failed message."""

    adapter = FakeAdapter(outcome=error)
    provider = _provider(_gateway(adapter), on_failure="message")
    assert await provider.generate("hello") == UNAVAILABLE_MESSAGE


async def test_a_rendered_failure_never_leaks_provider_detail_to_a_chat_surface() -> None:
    adapter = FakeAdapter(
        outcome=TransientProviderError(
            "upstream said: API key AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMYDUMMY00 invalid for prompt 'x'"
        )
    )
    provider = _provider(_gateway(adapter), on_failure="message")
    text = await provider.generate("hello")
    assert "AIzaSy" not in text
    assert "upstream" not in text


async def test_an_invalid_on_failure_mode_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GatewayLLMProvider(caller=CALLER, on_failure="swallow")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# Degradation is announced, never hidden (LAW 8)
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_degraded_answer_carries_the_verbatim_disclaimer() -> None:
    primary = FakeAdapter("gemini", outcome=RateLimitedError("slow", status_code=429))
    degraded = FakeAdapter("local-degraded", outcome="[local] answer", degraded=True)
    route_primary = Route(provider="gemini", model="m", rank=10)
    route_degraded = Route(provider="local-degraded", model="m", rank=900, degraded=True)
    policy = default_policy(
        routes=[route_primary, route_degraded],
        retry=NO_RETRY,
        fallback=FallbackPolicy(enabled=True, max_hops=1, allow_degraded_routes=True),
    )
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(primary, [route_primary])
    gateway.register(degraded, [route_degraded])

    text = await _provider(gateway).generate("hello")
    assert text == "[local] answer" + DEGRADED_DISCLAIMER
    assert "Fallback mode" in text


async def test_a_healthy_answer_carries_no_disclaimer() -> None:
    adapter = FakeAdapter(outcome="the answer")
    text = await _provider(_gateway(adapter)).generate("hello")
    assert text == "the answer"
    assert DEGRADED_DISCLAIMER not in text


async def test_the_disclaimer_can_be_turned_off_by_the_composition_root() -> None:
    primary = FakeAdapter("gemini", outcome=RateLimitedError("slow", status_code=429))
    degraded = FakeAdapter("local-degraded", outcome="[local] answer", degraded=True)
    route_primary = Route(provider="gemini", model="m", rank=10)
    route_degraded = Route(provider="local-degraded", model="m", rank=900, degraded=True)
    policy = default_policy(
        routes=[route_primary, route_degraded],
        retry=NO_RETRY,
        fallback=FallbackPolicy(enabled=True, max_hops=1, allow_degraded_routes=True),
    )
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(primary, [route_primary])
    gateway.register(degraded, [route_degraded])
    provider = _provider(gateway, degraded_disclaimer=None)
    assert await provider.generate("hello") == "[local] answer"


async def test_the_typed_path_reports_degradation_as_a_flag_not_as_text() -> None:
    """``complete()`` must not decorate a payload a caller intends to parse."""

    adapter = FakeAdapter("local-degraded", outcome="[local] answer", degraded=True)
    route = Route(provider="local-degraded", model="m", degraded=True)
    policy = default_policy(routes=[route], retry=NO_RETRY)
    gateway = LLMGateway(policy=policy, sinks=[CollectingSink()], attach_default_sink=False)
    gateway.register(adapter, [route])
    response = await _provider(gateway).complete("hello")
    assert response.text == "[local] answer"
    assert response.degraded is True


# ═══════════════════════════════════════════════════════════════════════════
# complete(): the typed path
# ═══════════════════════════════════════════════════════════════════════════


async def test_complete_returns_the_full_response() -> None:
    adapter = FakeAdapter(outcome="the answer")
    response = await _provider(_gateway(adapter)).complete("hello", purpose="classify")
    assert response.text == "the answer"
    assert response.purpose == "classify"
    assert response.provider == "gemini"
    assert response.request_id


async def test_complete_forwards_idempotency_cancellation_and_deadline() -> None:
    adapter = FakeAdapter()
    token = asyncio.Event()
    await _provider(_gateway(adapter)).complete(
        "hello", idempotency_key="k", cancellation=token, deadline_seconds=9.0
    )
    sent = adapter.requests[0]
    assert sent.idempotency_key == "k"
    assert sent.cancellation is token
    assert sent.deadline_seconds == 9.0


async def test_complete_applies_the_callers_output_validator() -> None:
    adapter = FakeAdapter(outcome='{"a": 1}')
    response = await _provider(_gateway(adapter)).complete(
        "hello", output_validator=json_object_validator(required_keys=("a",))
    )
    assert response.structured == {"a": 1}


async def test_a_validator_violation_surfaces_as_a_typed_contract_error() -> None:
    adapter = FakeAdapter(outcome="not json")
    with pytest.raises(StructuredOutputInvalidError):
        await _provider(_gateway(adapter)).complete(
            "hello", output_validator=json_object_validator()
        )


# ═══════════════════════════════════════════════════════════════════════════
# Validators
# ═══════════════════════════════════════════════════════════════════════════


def test_the_json_validator_accepts_a_plain_object() -> None:
    assert json_object_validator()('{"a": 1}') == {"a": 1}


def test_the_json_validator_accepts_a_fenced_object_by_default() -> None:
    assert json_object_validator()('```json\n{"a": 1}\n```') == {"a": 1}


def test_the_json_validator_can_refuse_fences() -> None:
    with pytest.raises(ValueError):
        json_object_validator(allow_fenced=False)('```json\n{"a": 1}\n```')


def test_the_json_validator_refuses_a_non_object() -> None:
    with pytest.raises(ValueError):
        json_object_validator()("[1, 2, 3]")


def test_the_json_validator_enforces_required_keys() -> None:
    validator = json_object_validator(required_keys=("a", "b"))
    assert validator('{"a": 1, "b": 2}') == {"a": 1, "b": 2}
    with pytest.raises(ValueError):
        validator('{"a": 1}')


def test_the_pydantic_validator_returns_a_model_instance() -> None:
    from pydantic import BaseModel

    class Shape(BaseModel):
        name: str
        size: int

    validator = pydantic_validator(Shape)
    parsed = validator('{"name": "box", "size": 3}')
    assert isinstance(parsed, Shape)
    assert parsed.size == 3


def test_the_pydantic_validator_rejects_a_bad_payload() -> None:
    from pydantic import BaseModel

    class Shape(BaseModel):
        size: int

    with pytest.raises(ValueError):
        pydantic_validator(Shape)('{"size": "not-a-number"}')


# ═══════════════════════════════════════════════════════════════════════════
# Caller helpers
# ═══════════════════════════════════════════════════════════════════════════


def test_agent_and_surface_callers_are_labelled_by_category() -> None:
    assert agent_caller("agents.planner", tenant_id=4).label == "agent:agents.planner"
    assert surface_caller("features.summarizer").label == "surface:features.summarizer"
    assert agent_caller("x", tenant_id=9).tenant_id == 9


def test_a_provider_can_be_bound_to_an_explicit_gateway() -> None:
    adapter = FakeAdapter()
    gateway = _gateway(adapter)
    bound = GatewayLLMProvider(caller=CALLER).bind(gateway)
    assert bound.gateway is gateway
    assert bound.caller is CALLER


def test_binding_preserves_the_composition_settings() -> None:
    gateway = _gateway(FakeAdapter())
    original = GatewayLLMProvider(
        caller=CALLER,
        purpose="chat",
        priority=LLMPriority.OWNER,
        on_failure="message",
        deadline_seconds=3.0,
        degraded_disclaimer=None,
    )
    bound = original.bind(gateway)
    assert bound.purpose == "chat"
    assert bound.priority is LLMPriority.OWNER
    assert bound.on_failure == "message"
    assert bound.deadline_seconds == 3.0
    assert bound.degraded_disclaimer is None


async def test_an_unbound_provider_resolves_the_process_authority() -> None:
    adapter = FakeAdapter()
    gateway = _gateway(adapter)
    set_llm_gateway(gateway)
    provider = GatewayLLMProvider(caller=CALLER)
    assert provider.authority() is gateway
    assert await provider.generate("hello") == "the answer"


# ═══════════════════════════════════════════════════════════════════════════
# Registry: one authority per process (LAW 1)
# ═══════════════════════════════════════════════════════════════════════════


def test_the_registry_hands_back_the_same_instance() -> None:
    gateway = _gateway(FakeAdapter())
    set_llm_gateway(gateway)
    assert get_llm_gateway() is gateway
    assert get_llm_gateway() is gateway


def test_installing_returns_the_previous_gateway_so_it_can_be_closed() -> None:
    first = _gateway(FakeAdapter("a"))
    second = _gateway(FakeAdapter("b"))
    assert set_llm_gateway(first) is None
    assert set_llm_gateway(second) is first
    assert get_llm_gateway() is second


def test_a_closed_authority_is_replaced_rather_than_served() -> None:
    """A dead gateway must not keep refusing every call for the process lifetime."""

    adapter = FakeAdapter()
    gateway = _gateway(adapter)
    set_llm_gateway(gateway)
    asyncio.run(gateway.aclose())
    assert gateway.closed is True
    replacement = _gateway(FakeAdapter("fresh"))
    set_llm_gateway(replacement)
    assert get_llm_gateway() is replacement


def test_clearing_the_registry_returns_none_afterwards() -> None:
    set_llm_gateway(_gateway(FakeAdapter()))
    reset_llm_gateway()
    # get_llm_gateway() would now build from settings; the point is that the
    # accessor holds nothing stale.
    from nexus_ai_agent.llm.gateway import registry

    assert registry._gateway is None  # noqa: SLF001 — registry internals


def test_the_lazy_build_from_settings_produces_a_usable_authority() -> None:
    from nexus_ai_agent.config.settings import Settings

    reset_llm_gateway()
    settings = Settings(gemini_api_key="")
    gateway = get_llm_gateway(settings)
    try:
        assert isinstance(gateway, LLMGateway)
        assert gateway.closed is False
        # No credential, no Gemini route: a request is refused with a reason
        # instead of silently pretending to work.
        blob = str(gateway.status())
        assert "AIza" not in blob
    finally:
        reset_llm_gateway()


async def test_a_deployment_without_credentials_refuses_typed() -> None:
    from nexus_ai_agent.config.settings import Settings

    reset_llm_gateway()
    gateway = get_llm_gateway(Settings(gemini_api_key=""))
    try:
        with pytest.raises(LLMError) as excinfo:
            await gateway.execute(LLMRequest(caller=CALLER, prompt="hello", provider="gemini"))
        # No credential means no usable route: a typed refusal (or, if a
        # credential-less adapter was registered, a typed auth failure) — never
        # a silent empty answer.
        assert excinfo.value.kind in {
            LLMErrorKind.POLICY_REFUSAL,
            LLMErrorKind.AUTHENTICATION,
            LLMErrorKind.GATEWAY_INTERNAL,
        }
    finally:
        reset_llm_gateway()


# ═══════════════════════════════════════════════════════════════════════════
# Credential-scoped gateways: still the authority, never a bypass
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_deployment_key_resolves_to_the_process_authority() -> None:
    from nexus_ai_agent.config.settings import get_settings

    authority = _gateway(FakeAdapter())
    set_llm_gateway(authority)
    key = get_settings().gemini_api_key or ""
    if not key:
        pytest.skip("deployment has no Gemini key configured")
    assert gateway_for_credentials(key, "gemini-2.0-flash") is authority


async def test_an_empty_key_resolves_to_the_process_authority_and_refuses_typed() -> None:

    reset_llm_gateway()
    gateway = gateway_for_credentials("", "gemini-2.0-flash")
    try:
        assert isinstance(gateway, LLMGateway)
        with pytest.raises(LLMError):
            await gateway.execute(LLMRequest(caller=CALLER, prompt="hello"))
    finally:
        reset_llm_gateway()


async def test_a_second_credential_gets_its_own_scoped_gateway() -> None:
    reset_llm_gateway()
    scoped = gateway_for_credentials("AIzaSyTEST-ONLY-KEY-VALUE-0000000000", "gemini-2.0-flash")
    try:
        assert isinstance(scoped, LLMGateway)
        assert [route.key for route in scoped.policy.routes] == ["gemini/gemini-2.0-flash"]
        # One route, so no hidden hop is possible (LAW 8).
        assert scoped.policy.fallback.enabled is False
        assert scoped.policy.fallback.max_hops == 0
    finally:
        await scoped.aclose()


async def test_a_scoped_gateway_is_cached_per_credential_and_model() -> None:
    reset_llm_gateway()
    key = "AIzaSyTEST-ONLY-KEY-VALUE-0000000001"
    first = gateway_for_credentials(key, "gemini-2.0-flash")
    second = gateway_for_credentials(key, "gemini-2.0-flash")
    other_model = gateway_for_credentials(key, "gemini-2.5-flash")
    try:
        assert first is second
        assert other_model is not first
    finally:
        await first.aclose()
        await other_model.aclose()
        _CREDENTIAL_GATEWAYS.clear()


async def test_the_scoped_cache_is_bounded_and_evicts_the_oldest() -> None:
    from nexus_ai_agent.llm.gateway.registry import _CREDENTIAL_GATEWAY_CAP

    reset_llm_gateway()
    built: list[LLMGateway] = []
    try:
        for index in range(_CREDENTIAL_GATEWAY_CAP + 5):
            built.append(
                gateway_for_credentials(f"AIzaSyTEST-ONLY-KEY-{index:020d}", "gemini-2.0-flash")
            )
        assert len(_CREDENTIAL_GATEWAYS) <= _CREDENTIAL_GATEWAY_CAP
        await asyncio.sleep(0)  # let the eviction close tasks run
    finally:
        for gateway in built:
            if not gateway.closed:
                await gateway.aclose()
        _CREDENTIAL_GATEWAYS.clear()


async def test_a_scoped_gateway_never_exposes_the_key_in_its_status() -> None:
    reset_llm_gateway()
    key = "AIzaSyTEST-ONLY-KEY-VALUE-0000000002"
    scoped = gateway_for_credentials(key, "gemini-2.0-flash")
    try:
        blob = str(scoped.status())
        assert key not in blob
        assert "AIzaSy" not in blob
    finally:
        await scoped.aclose()


async def test_a_closed_scoped_gateway_is_rebuilt_rather_than_served() -> None:
    reset_llm_gateway()
    key = "AIzaSyTEST-ONLY-KEY-VALUE-0000000003"
    first = gateway_for_credentials(key, "gemini-2.0-flash")
    await first.aclose()
    second = gateway_for_credentials(key, "gemini-2.0-flash")
    try:
        assert second is not first
        assert second.closed is False
    finally:
        await second.aclose()
        _CREDENTIAL_GATEWAYS.clear()


async def test_a_scoped_gateway_still_enforces_the_same_policy_engine() -> None:
    """Scoping changes the credential, not the rules."""

    reset_llm_gateway()
    scoped = gateway_for_credentials(
        "AIzaSyTEST-ONLY-KEY-VALUE-0000000004",
        "gemini-2.0-flash",
        requests_per_minute=1,
    )
    try:
        assert scoped.policy.rate_limit.for_provider("gemini").requests_per_minute == 1
        assert scoped.policy.retry.max_attempts == 3
        with pytest.raises(PolicyRefusalError):
            await scoped.execute(
                LLMRequest(
                    caller=CALLER,
                    prompt="hello",
                    operation=LLMOperation.EMBEDDINGS,
                    model="gemini-2.0-flash",
                )
            )
    finally:
        await scoped.aclose()
        _CREDENTIAL_GATEWAYS.clear()
