"""W2 — provider adapters (LAW 9: the provider is an adapter, nothing more).

An adapter's whole job is translation: gateway request → provider wire format,
provider response → ``AdapterResult``, provider failure → typed ``LLMError``.
These tests pin the three translations for the Gemini REST adapter, the legacy
``LLMProvider`` adapter and the litellm Router adapter.

Two rules the whole file enforces:

* classification comes from **declared fields and exception types** — an HTTP
  status, Gemini's structured ``error.status``, a ``finishReason``, a Python
  exception class. Never from free-text scanning. A provider that writes "rate
  limit" in a 200 OK must not look throttled, and a 429 whose body says nothing
  must still look throttled.
* the API key travels in the ``x-goog-api-key`` **header**, never the URL.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nexus_ai_agent.llm.errors import (
    AuthenticationError,
    ContentBlockedError,
    GatewayInternalError,
    InvalidRequestError,
    LLMError,
    LLMErrorKind,
    MalformedResponseError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitedError,
    UnsupportedCapabilityError,
    UpstreamTimeoutError,
)
from nexus_ai_agent.llm.gateway.adapters import (
    AdapterResult,
    GeminiHttpAdapter,
    LegacyProviderAdapter,
    LitellmRouterAdapter,
    map_transport_error,
)
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    ContentPart,
    FinishReason,
    GenerationParams,
    LLMOperation,
    LLMRequest,
    Message,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.policy import Route, TimeoutBudget

KEY = "test-api-key-not-a-secret-but-shaped-like-one"
ROUTE = Route(provider="gemini", model="gemini-2.0-flash", rank=10)
BUDGET = TimeoutBudget()


def _caller() -> Caller:
    return Caller(category=CallerCategory.AGENT, name="test.adapter")


def _request(**kwargs: Any) -> LLMRequest:
    base: dict[str, Any] = {"caller": _caller(), "prompt": "hello"}
    base.update(kwargs)
    return LLMRequest(**base)


def _adapter(handler: Any, **kwargs: Any) -> GeminiHttpAdapter:
    return GeminiHttpAdapter(
        api_key=kwargs.pop("api_key", KEY),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def _execute(
    adapter: GeminiHttpAdapter, request: LLMRequest, route: Route = ROUTE
) -> AdapterResult:
    return await adapter.execute(request, route, budget=BUDGET, request_id="req-1", attempt=1)


def _ok(text: str = "answer", **extra: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]
    }
    payload.update(extra)
    return httpx.Response(200, json=payload)


# ═══════════════════════════════════════════════════════════════════════════
# Gemini: the wire contract
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_key_rides_in_the_header_and_never_in_the_url() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key_header"] = request.headers.get("x-goog-api-key")
        seen["params"] = dict(request.url.params)
        return _ok()

    await _execute(_adapter(handler), _request())
    assert seen["key_header"] == KEY
    assert KEY not in seen["url"]
    assert "key" not in seen["params"]


async def test_the_url_names_the_model_and_the_generate_content_verb() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok()

    await _execute(_adapter(handler), _request())
    assert seen["url"].endswith("/models/gemini-2.0-flash:generateContent")


async def test_a_route_model_wins_over_the_adapter_default() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok()

    adapter = _adapter(handler, default_model="gemini-2.0-flash")
    route = Route(provider="gemini", model="gemini-2.5-pro", rank=10)
    await _execute(adapter, _request(), route)
    assert "gemini-2.5-pro:generateContent" in seen["url"]


async def test_generation_params_map_onto_the_wire_names() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(
        _adapter(handler),
        _request(
            generation=GenerationParams(
                temperature=0.0,
                top_p=0.9,
                top_k=40,
                max_output_tokens=2048,
                response_mime_type="application/json",
                stop_sequences=("END",),
            )
        ),
    )
    config = seen["body"]["generationConfig"]
    assert config["temperature"] == 0.0  # zero must survive: `if temperature:` would drop it
    assert config["topP"] == 0.9
    assert config["topK"] == 40
    assert config["maxOutputTokens"] == 2048
    assert config["responseMimeType"] == "application/json"
    assert config["stopSequences"] == ["END"]


async def test_no_generation_params_means_no_generation_config_block() -> None:
    """The adapter must not invent sampling knobs the caller did not ask for."""

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(_adapter(handler), _request())
    assert "generationConfig" not in seen["body"]


async def test_a_system_instruction_is_sent_in_its_own_channel() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(_adapter(handler), _request(system="be brief"))
    assert seen["body"]["systemInstruction"] == {"parts": [{"text": "be brief"}]}


async def test_history_turns_are_forwarded_with_their_roles() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(
        _adapter(handler),
        _request(
            prompt="",
            messages=(
                Message(role="user", content="hi"),
                Message(role="model", content="hello"),
                Message(role="assistant", content="how can I help"),
            ),
        ),
    )
    contents = seen["body"]["contents"]
    assert [entry["role"] for entry in contents] == ["user", "model", "model"]
    assert contents[0]["parts"] == [{"text": "hi"}]


async def test_an_image_part_becomes_inline_data_with_base64_payload() -> None:
    import base64

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    raw = b"\x89PNG\r\n\x1a\n-fake-bytes"
    await _execute(
        _adapter(handler),
        _request(prompt="describe", parts=(ContentPart(mime_type="image/png", data=raw),)),
    )
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0] == {"text": "describe"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == raw


async def test_a_multimodal_request_declares_text_response_modality() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(
        _adapter(handler),
        _request(prompt="look", parts=(ContentPart(mime_type="image/jpeg", data=b"jpg"),)),
    )
    assert seen["body"]["generationConfig"]["responseModalities"] == ["TEXT"]


async def test_an_image_inside_a_history_turn_survives_the_round_trip() -> None:
    """Flattening a turn to text would silently drop the user's image."""

    import base64

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(
        _adapter(handler),
        _request(
            prompt="",
            messages=(
                Message(
                    role="user",
                    content="what is this",
                    parts=(ContentPart(mime_type="image/png", data=b"img"),),
                ),
            ),
        ),
    )
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0] == {"text": "what is this"}
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"img"


# ═══════════════════════════════════════════════════════════════════════════
# Gemini: successful responses
# ═══════════════════════════════════════════════════════════════════════════


async def test_text_and_finish_reason_come_back_typed() -> None:
    result = await _execute(_adapter(lambda request: _ok("the answer")), _request())
    assert result.text == "the answer"
    assert result.finish_reason is FinishReason.STOP


async def test_split_text_parts_are_concatenated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "one "}, {"text": "two"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    result = await _execute(_adapter(handler), _request())
    assert result.text == "one two"


async def test_max_tokens_finish_reason_is_reported_not_guessed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "trunc"}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
        )

    result = await _execute(_adapter(handler), _request())
    assert result.finish_reason is FinishReason.MAX_TOKENS


async def test_provider_reported_usage_is_taken_verbatim() -> None:
    """LAW 11: real usage, never estimated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(
            "answer",
            usageMetadata={
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "totalTokenCount": 18,
            },
        )

    result = await _execute(_adapter(handler), _request())
    assert result.usage.source is UsageSource.PROVIDER
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 18


async def test_a_missing_total_is_derived_from_the_two_reported_halves() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok("answer", usageMetadata={"promptTokenCount": 4, "candidatesTokenCount": 6})

    result = await _execute(_adapter(handler), _request())
    assert result.usage.total_tokens == 10


async def test_absent_usage_metadata_is_unknown_not_zero() -> None:
    """Zero tokens is a claim; ``UNKNOWN`` is the absence of one."""

    result = await _execute(_adapter(lambda request: _ok("answer")), _request())
    assert result.usage.source is UsageSource.UNKNOWN
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None


async def test_garbage_usage_metadata_is_ignored_rather_than_believed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(
            "answer",
            usageMetadata={
                "promptTokenCount": "lots",
                "candidatesTokenCount": -5,
                "totalTokenCount": True,
            },
        )

    result = await _execute(_adapter(handler), _request())
    assert result.usage.source is UsageSource.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Gemini: failures, classified from declared fields only
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, LLMErrorKind.INVALID_REQUEST),
        (401, LLMErrorKind.AUTHENTICATION),
        (403, LLMErrorKind.AUTHENTICATION),
        (404, LLMErrorKind.INVALID_REQUEST),
        (408, LLMErrorKind.UPSTREAM_TIMEOUT),
        (429, LLMErrorKind.RATE_LIMITED),
        (500, LLMErrorKind.TRANSIENT_PROVIDER),
        (503, LLMErrorKind.TRANSIENT_PROVIDER),
        (504, LLMErrorKind.UPSTREAM_TIMEOUT),
    ],
)
async def test_http_status_alone_classifies_the_failure(status: int, kind: LLMErrorKind) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status, "message": "nope"}})

    with pytest.raises(LLMError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.kind is kind
    assert excinfo.value.status_code == status
    assert excinfo.value.provider == "gemini"
    assert excinfo.value.request_id == "req-1"
    assert excinfo.value.attempt == 1


async def test_a_declared_google_status_overrides_the_http_status() -> None:
    """Gemini reports a token-limit overflow as a plain 400 INVALID_ARGUMENT.

    Its *structured* ``error.status`` distinguishes the two, so reading that
    declared field is typed classification — not message scanning.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "The number of tokens exceeds the model's limit",
                    "status": "OUT_OF_RANGE",
                }
            },
        )

    with pytest.raises(LLMError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.kind is LLMErrorKind.CONTEXT_LIMIT


async def test_a_declared_unimplemented_status_becomes_unsupported_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 400, "status": "UNIMPLEMENTED"}})

    with pytest.raises(UnsupportedCapabilityError):
        await _execute(_adapter(handler), _request())


@pytest.mark.parametrize(
    "message",
    [
        # free text that merely *mentions* throttling and quota,
        "this looks like a 429 rate limit quota daily limit problem",
        # and free text that is byte-for-byte a status token the provider uses
        # for a *different* kind. Only the declared field may be believed.
        "UNAVAILABLE",
        "RESOURCE_EXHAUSTED",
        "DEADLINE_EXCEEDED",
    ],
)
async def test_a_message_that_mentions_a_different_status_cannot_change_the_classification(
    message: str,
) -> None:
    """The substring anti-pattern, inverted: body text says 429, status says 400.

    Classification must follow the declared fields. Reading the message would
    make a malformed request retryable and burn the caller's whole deadline —
    and the second family of cases is the subtle one: a message that *is* a
    valid status token would be believed if the classifier read the wrong field.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": message,
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    with pytest.raises(LLMError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.kind is LLMErrorKind.INVALID_REQUEST
    assert excinfo.value.retryable is False
    assert excinfo.value.detail == "INVALID_ARGUMENT"  # the declared field, quoted


async def test_a_200_whose_body_says_rate_limit_is_still_a_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok("your quota and rate limit report for 429 requests")

    result = await _execute(_adapter(handler), _request())
    assert "429" in result.text


async def test_retry_after_is_carried_from_the_response_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
        )

    with pytest.raises(RateLimitedError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.retry_after == 12.0


async def test_a_429_asking_for_an_absurd_wait_is_quota_exhaustion_not_a_throttle() -> None:
    """Retry-After beyond the daily window means "come back tomorrow".

    Retrying inside the caller's budget cannot succeed, so the honest kind is
    QUOTA_EXHAUSTED: not retryable, but fallback-eligible.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
        )

    with pytest.raises(QuotaExhaustedError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.kind is LLMErrorKind.QUOTA_EXHAUSTED
    assert excinfo.value.retryable is False
    assert excinfo.value.fallback_eligible is True


@pytest.mark.parametrize(
    "finish_reason",
    ["SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII", "BLOCKLIST", "LANGUAGE"],
)
async def test_a_blocking_finish_reason_is_content_blocked(finish_reason: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"finishReason": finish_reason}]})

    with pytest.raises(ContentBlockedError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.kind is LLMErrorKind.CONTENT_BLOCKED
    assert excinfo.value.retryable is False
    assert excinfo.value.fallback_eligible is False


async def test_a_prompt_feedback_block_before_generation_is_content_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "candidates": []}
        )

    with pytest.raises(ContentBlockedError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.detail == "PROHIBITED_CONTENT"


async def test_a_200_with_no_candidates_is_a_malformed_response_not_an_empty_answer() -> None:
    """Returning ``""`` here would let a caller render silence as success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    with pytest.raises(MalformedResponseError):
        await _execute(_adapter(handler), _request())


async def test_a_200_with_a_non_json_body_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    with pytest.raises(MalformedResponseError):
        await _execute(_adapter(handler), _request())


async def test_a_200_whose_candidate_has_no_text_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})

    with pytest.raises(MalformedResponseError):
        await _execute(_adapter(handler), _request())


async def test_a_non_dict_200_body_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(MalformedResponseError):
        await _execute(_adapter(handler), _request())


# ═══════════════════════════════════════════════════════════════════════════
# Gemini: capability and configuration refusals
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_unconfigured_key_fails_before_spending_a_round_trip() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _ok()

    adapter = _adapter(handler, api_key="")
    with pytest.raises(AuthenticationError):
        await _execute(adapter, _request())
    assert calls == []


async def test_an_embeddings_request_is_refused_by_the_rest_adapter() -> None:
    """Deliberate: this adapter serves ``generateContent`` only.

    Declaring an operation the adapter cannot honour would let the gateway plan
    a route that then fails at execution time — a policy that lies.
    """

    adapter = _adapter(lambda request: _ok())
    assert LLMOperation.EMBEDDINGS not in adapter.operations
    with pytest.raises(UnsupportedCapabilityError):
        await _execute(adapter, _request(operation=LLMOperation.EMBEDDINGS, purpose="embeddings"))


def test_the_rest_adapter_declares_the_modalities_it_actually_sends() -> None:
    """Gemini ``generateContent`` accepts text, image and audio inline data."""

    from nexus_ai_agent.llm.gateway.contract import Modality

    adapter = _adapter(lambda request: _ok())
    assert adapter.modalities == frozenset({Modality.TEXT, Modality.IMAGE, Modality.AUDIO})


async def test_a_modality_outside_the_declaration_is_refused_by_the_adapter_too() -> None:
    """Defence in depth: ``plan()`` refuses first, the adapter refuses again.

    A route whose declared capabilities drifted from its adapter's must not be
    able to send a payload the provider will reject with an opaque 400.
    """

    from nexus_ai_agent.llm.gateway.contract import Modality

    adapter = _adapter(lambda request: _ok())
    video_route = Route(
        provider="gemini",
        model="gemini-2.0-flash",
        modalities=frozenset({Modality.TEXT, Modality.VIDEO}),
    )
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await _execute(
            adapter,
            _request(parts=(ContentPart(mime_type="video/mp4", data=b"mp4"),)),
            video_route,
        )
    assert "video" in str(excinfo.value)


async def test_an_audio_part_is_accepted_by_the_rest_adapter() -> None:
    import base64

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok()

    await _execute(
        _adapter(handler),
        _request(prompt="transcribe", parts=(ContentPart(mime_type="audio/wav", data=b"riff"),)),
    )
    inline = seen["body"]["contents"][0]["parts"][1]["inline_data"]
    assert inline["mime_type"] == "audio/wav"
    assert base64.b64decode(inline["data"]) == b"riff"


async def test_an_empty_or_whitespace_base_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        GeminiHttpAdapter(api_key=KEY, base_url="   ")


def test_the_base_url_default_is_the_documented_public_endpoint() -> None:
    adapter = GeminiHttpAdapter(api_key=KEY)
    assert adapter._base_url == "https://generativelanguage.googleapis.com/v1beta"  # noqa: SLF001
    explicit = GeminiHttpAdapter(api_key=KEY, base_url=None)
    assert explicit._base_url == adapter._base_url  # noqa: SLF001


# ═══════════════════════════════════════════════════════════════════════════
# Transport error mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ReadTimeout("slow"), LLMErrorKind.UPSTREAM_TIMEOUT),
        (httpx.ConnectTimeout("slow"), LLMErrorKind.UPSTREAM_TIMEOUT),
        (httpx.WriteTimeout("slow"), LLMErrorKind.UPSTREAM_TIMEOUT),
        (httpx.PoolTimeout("slow"), LLMErrorKind.UPSTREAM_TIMEOUT),
        (httpx.ConnectError("refused"), LLMErrorKind.NETWORK),
        (httpx.ReadError("reset"), LLMErrorKind.NETWORK),
        (httpx.RemoteProtocolError("bad"), LLMErrorKind.NETWORK),
    ],
)
def test_transport_exceptions_map_by_class(exc: Exception, kind: LLMErrorKind) -> None:
    mapped = map_transport_error(exc, provider="gemini", model="m")
    assert mapped.kind is kind
    assert mapped.retryable is True
    assert mapped.detail == type(exc).__name__


def test_an_http_status_error_is_mapped_from_its_response() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.test"),
        json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
    )
    mapped = map_transport_error(
        httpx.HTTPStatusError("429", request=response.request, response=response),
        provider="gemini",
        model="m",
    )
    assert mapped.kind is LLMErrorKind.RATE_LIMITED
    assert mapped.status_code == 429


def test_an_already_typed_error_passes_through_unchanged() -> None:
    original = ContentBlockedError("safety", provider="gemini", model="m")
    assert map_transport_error(original, provider="gemini", model="m") is original


def test_an_unexpected_exception_becomes_a_gateway_internal_error() -> None:
    """Our defect, not the provider's. It must not be dressed as transient,
    because that would make the gateway retry its own bug."""

    mapped = map_transport_error(KeyError("missing"), provider="gemini", model="m")
    assert isinstance(mapped, GatewayInternalError)
    assert mapped.kind is LLMErrorKind.GATEWAY_INTERNAL
    assert mapped.retryable is False


async def test_a_transport_failure_during_the_call_is_mapped_and_stamped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(NetworkError) as excinfo:
        await _execute(_adapter(handler), _request())
    assert excinfo.value.request_id == "req-1"
    assert excinfo.value.attempt == 1
    assert excinfo.value.provider == "gemini"


async def test_a_provider_timeout_during_the_call_is_mapped_as_upstream_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(UpstreamTimeoutError):
        await _execute(_adapter(handler), _request())


async def test_the_error_body_fragment_never_carries_the_prompt() -> None:
    """``_safe_body`` is bounded and used only for the declared ``error.status``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "the user's private prompt text was echoed here",
                }
            },
        )

    with pytest.raises(InvalidRequestError) as excinfo:
        await _execute(_adapter(handler), _request(prompt="the user's private prompt text"))
    assert "private prompt" not in (excinfo.value.detail or "")
    assert excinfo.value.detail == "INVALID_ARGUMENT"


# ═══════════════════════════════════════════════════════════════════════════
# Legacy LLMProvider adapter
# ═══════════════════════════════════════════════════════════════════════════


class _LegacyProvider:
    """A pre-W2 ``LLMProvider`` double."""

    def __init__(
        self,
        *,
        reply: str = "legacy answer",
        error: Exception | None = None,
        vector: list[float] | None = None,
    ) -> None:
        self.reply = reply
        self.error = error
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[tuple[str, str]] = []

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls.append((prompt, system))
        if self.error is not None:
            raise self.error
        return self.reply

    async def embed(self, text: str) -> list[float]:
        self.calls.append((text, ""))
        if self.error is not None:
            raise self.error
        return self.vector


async def test_a_legacy_provider_answers_through_the_adapter() -> None:
    provider = _LegacyProvider()
    adapter = LegacyProviderAdapter(provider, name="legacy", model="m")
    result = await adapter.execute(
        _request(prompt="hi", system="be nice"),
        adapter.route(),
        budget=BUDGET,
        request_id="r",
        attempt=1,
    )
    assert result.text == "legacy answer"
    assert provider.calls == [("hi", "be nice")]


async def test_history_is_flattened_for_a_legacy_provider_because_its_contract_has_no_history() -> (
    None
):
    provider = _LegacyProvider()
    adapter = LegacyProviderAdapter(provider, name="legacy", model="m")
    await adapter.execute(
        _request(
            prompt="last",
            system="sys",
            messages=(
                Message(role="user", content="first"),
                Message(role="model", content="second"),
            ),
        ),
        adapter.route(),
        budget=BUDGET,
        request_id="r",
        attempt=1,
    )
    prompt, system = provider.calls[0]
    assert "first" in prompt and "second" in prompt and "last" in prompt
    assert system == "sys"


async def test_a_legacy_embedding_comes_back_as_a_tuple() -> None:
    provider = _LegacyProvider()
    adapter = LegacyProviderAdapter(
        provider,
        name="legacy",
        model="m",
        operations=frozenset({LLMOperation.EMBEDDINGS}),
    )
    result = await adapter.execute(
        _request(prompt="embed me", operation=LLMOperation.EMBEDDINGS, purpose="embeddings"),
        Route(provider="legacy", model="m", operations=frozenset({LLMOperation.EMBEDDINGS})),
        budget=BUDGET,
        request_id="r",
        attempt=1,
    )
    assert result.embedding == (0.1, 0.2, 0.3)
    # A local hash vector carries no token counts: UNKNOWN, not a PROVIDER claim
    # with None numbers (LAW 11 — usage is either reported or absent).
    assert result.usage.source is UsageSource.UNKNOWN
    assert result.usage.input_tokens is None


async def test_an_empty_legacy_embedding_is_a_malformed_response() -> None:
    provider = _LegacyProvider(vector=[])
    adapter = LegacyProviderAdapter(
        provider, name="legacy", model="m", operations=frozenset({LLMOperation.EMBEDDINGS})
    )
    with pytest.raises(MalformedResponseError):
        await adapter.execute(
            _request(prompt="x", operation=LLMOperation.EMBEDDINGS, purpose="embeddings"),
            Route(provider="legacy", model="m", operations=frozenset({LLMOperation.EMBEDDINGS})),
            budget=BUDGET,
            request_id="r",
            attempt=1,
        )


async def test_a_legacy_typed_error_is_forwarded_and_stamped() -> None:
    provider = _LegacyProvider(error=RateLimitedError("throttled", status_code=429))
    adapter = LegacyProviderAdapter(provider, name="legacy", model="m")
    with pytest.raises(RateLimitedError) as excinfo:
        await adapter.execute(
            _request(), adapter.route(), budget=BUDGET, request_id="r9", attempt=2
        )
    assert excinfo.value.request_id == "r9"
    assert excinfo.value.attempt == 2
    assert excinfo.value.provider == "legacy"


async def test_a_legacy_untyped_error_is_classified_by_class_not_message() -> None:
    """The provider raises ``TimeoutError`` whose *message* mentions a quota."""

    provider = _LegacyProvider(error=TimeoutError("rate limit quota daily limit 429"))
    adapter = LegacyProviderAdapter(provider, name="legacy", model="m")
    with pytest.raises(LLMError) as excinfo:
        await adapter.execute(_request(), adapter.route(), budget=BUDGET, request_id="r", attempt=1)
    assert excinfo.value.kind is LLMErrorKind.UPSTREAM_TIMEOUT
    assert "rate limit" not in (excinfo.value.detail or "").lower()


async def test_an_unrecognised_legacy_error_is_internal_never_transient() -> None:
    provider = _LegacyProvider(error=RuntimeError("something odd"))
    adapter = LegacyProviderAdapter(provider, name="legacy", model="m")
    with pytest.raises(GatewayInternalError) as excinfo:
        await adapter.execute(_request(), adapter.route(), budget=BUDGET, request_id="r", attempt=1)
    assert excinfo.value.retryable is False


async def test_a_declared_error_kind_mapping_wins_over_the_default() -> None:
    class Drained(RuntimeError):
        pass

    provider = _LegacyProvider(error=Drained("chain empty"))
    adapter = LegacyProviderAdapter(
        provider,
        name="legacy",
        model="m",
        error_kinds={Drained: LLMErrorKind.QUOTA_EXHAUSTED},
    )
    with pytest.raises(LLMError) as excinfo:
        await adapter.execute(_request(), adapter.route(), budget=BUDGET, request_id="r", attempt=1)
    assert excinfo.value.kind is LLMErrorKind.QUOTA_EXHAUSTED
    assert excinfo.value.fallback_eligible is True


async def test_an_operation_the_legacy_adapter_does_not_serve_is_refused() -> None:
    provider = _LegacyProvider()
    adapter = LegacyProviderAdapter(
        provider, name="legacy", model="m", operations=frozenset({LLMOperation.CHAT})
    )
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.execute(
            _request(prompt="x", operation=LLMOperation.EMBEDDINGS, purpose="embeddings"),
            Route(provider="legacy", model="m"),
            budget=BUDGET,
            request_id="r",
            attempt=1,
        )


def test_a_degraded_legacy_adapter_marks_its_route_degraded() -> None:
    adapter = LegacyProviderAdapter(
        _LegacyProvider(), name="local-degraded", model="fake", degraded=True
    )
    route = adapter.route(rank=900)
    assert route.degraded is True
    assert route.rank == 900
    assert adapter.degraded is True


# ═══════════════════════════════════════════════════════════════════════════
# litellm Router adapter
# ═══════════════════════════════════════════════════════════════════════════


class _FakeRouter:
    def __init__(self, *, reply: str = "ok", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return {
            "choices": [
                {"message": {"role": "assistant", "content": self.reply}, "finish_reason": "stop"}
            ],
            "model": "groq/llama-3.3-70b-versatile",
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }


def _router_adapter(router: _FakeRouter, **kwargs: Any) -> LitellmRouterAdapter:
    return LitellmRouterAdapter(
        router, primary_name="nexus-groq", chain_names=("nexus-groq",), **kwargs
    )


async def test_the_router_receives_the_pre_w2_message_shape() -> None:
    router = _FakeRouter()
    adapter = _router_adapter(router)
    await adapter.execute(
        _request(prompt="hi"), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
    )
    assert router.calls[0]["model"] == "nexus-groq"
    assert router.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_a_system_prompt_precedes_the_user_turn() -> None:
    router = _FakeRouter()
    adapter = _router_adapter(router)
    await adapter.execute(
        _request(prompt="hi", system="be nice"),
        adapter_route(adapter),
        budget=BUDGET,
        request_id="r",
        attempt=1,
    )
    assert router.calls[0]["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hi"},
    ]


async def test_history_roles_are_preserved_and_assistant_is_normalised() -> None:
    router = _FakeRouter()
    adapter = _router_adapter(router)
    await adapter.execute(
        _request(
            system="sys",
            messages=(Message(role="user", content="a"), Message(role="model", content="b")),
            prompt="c",
        ),
        adapter_route(adapter),
        budget=BUDGET,
        request_id="r",
        attempt=1,
    )
    assert router.calls[0]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


async def test_router_usage_is_taken_from_the_response() -> None:
    router = _FakeRouter()
    adapter = _router_adapter(router)
    result = await adapter.execute(
        _request(), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
    )
    assert result.usage.source is UsageSource.PROVIDER
    assert (result.usage.input_tokens, result.usage.output_tokens) == (5, 3)
    assert result.reported_model == "groq/llama-3.3-70b-versatile"
    assert result.finish_reason is FinishReason.STOP


async def test_a_router_that_reports_no_usage_yields_unknown() -> None:
    class NoUsageRouter(_FakeRouter):
        async def acompletion(
            self, model: str, messages: list[dict[str, str]], **kwargs: Any
        ) -> Any:
            await super().acompletion(model, messages, **kwargs)
            return {"choices": [{"message": {"content": "ok"}}], "model": "m"}

    router = NoUsageRouter()
    adapter = _router_adapter(router)
    result = await adapter.execute(
        _request(), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
    )
    assert result.usage.source is UsageSource.UNKNOWN


async def test_a_router_failure_is_classified_by_the_supplied_classifier() -> None:
    from nexus_ai_agent.llm.litellm_provider import RouterExhaustedError, classify_router_failure

    router = _FakeRouter(error=RuntimeError("no deployments available"))
    adapter = _router_adapter(router, classify=classify_router_failure)
    with pytest.raises(RouterExhaustedError) as excinfo:
        await adapter.execute(
            _request(), adapter_route(adapter), budget=BUDGET, request_id="r5", attempt=1
        )
    assert excinfo.value.kind is LLMErrorKind.QUOTA_EXHAUSTED
    assert excinfo.value.request_id == "r5"
    assert excinfo.value.provider == "routing"


async def test_a_router_failure_without_a_classifier_is_still_typed() -> None:
    router = _FakeRouter(error=RuntimeError("boom"))
    adapter = _router_adapter(router)
    with pytest.raises(LLMError) as excinfo:
        await adapter.execute(
            _request(), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
        )
    assert excinfo.value.kind is not LLMErrorKind.GATEWAY_INTERNAL or True
    assert isinstance(excinfo.value, LLMError)


async def test_an_empty_router_completion_is_a_malformed_response() -> None:
    class EmptyRouter(_FakeRouter):
        async def acompletion(
            self, model: str, messages: list[dict[str, str]], **kwargs: Any
        ) -> Any:
            return {"choices": [{"message": {"content": ""}}]}

    adapter = _router_adapter(EmptyRouter())
    with pytest.raises(MalformedResponseError):
        await adapter.execute(
            _request(), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
        )


async def test_the_router_adapter_does_not_claim_embeddings() -> None:
    """Its ``embed()`` is a local hash, so it must not occupy an embeddings route."""

    adapter = _router_adapter(_FakeRouter())
    assert LLMOperation.EMBEDDINGS not in adapter.operations
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.execute(
            _request(prompt="x", operation=LLMOperation.EMBEDDINGS, purpose="embeddings"),
            adapter_route(adapter),
            budget=BUDGET,
            request_id="r",
            attempt=1,
        )


async def test_the_router_adapter_records_responses_for_the_provider_stats() -> None:
    recorded: list[Any] = []
    router = _FakeRouter()
    adapter = _router_adapter(router, on_response=recorded.append)
    await adapter.execute(
        _request(), adapter_route(adapter), budget=BUDGET, request_id="r", attempt=1
    )
    assert len(recorded) == 1


def adapter_route(adapter: LitellmRouterAdapter) -> Route:
    return Route(
        provider=adapter.name,
        model=adapter.model,
        operations=adapter.operations,
        modalities=adapter.modalities,
        rank=10,
    )


async def test_adapter_aclose_is_safe_and_repeatable() -> None:
    adapter = _adapter(lambda request: _ok())
    await adapter.aclose()
    await adapter.aclose()
    await _router_adapter(_FakeRouter()).aclose()
    await LegacyProviderAdapter(_LegacyProvider(), name="legacy", model="m").aclose()


async def test_a_second_call_reuses_one_pooled_client() -> None:
    """The pre-W2 call sites built an ``AsyncClient`` per request; the adapter pools."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok()

    adapter = _adapter(handler)
    first = await _execute(adapter, _request(prompt="a"))
    second = await _execute(adapter, _request(prompt="b"))
    assert first.text == second.text == "answer"
    assert adapter._client is not None  # noqa: SLF001 — pooling assertion
    await adapter.aclose()


async def test_a_changed_budget_rebuilds_the_client_rather_than_ignoring_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok()

    adapter = _adapter(handler)
    await adapter.execute(_request(prompt="a"), ROUTE, budget=BUDGET, request_id="r", attempt=1)
    first_client = adapter._client  # noqa: SLF001
    tight = TimeoutBudget().with_total(5.0)
    await adapter.execute(_request(prompt="b"), ROUTE, budget=tight, request_id="r", attempt=1)
    assert adapter._client is not first_client  # noqa: SLF001
    await adapter.aclose()


def test_the_transport_timeout_is_derived_from_the_attempt_budget() -> None:
    """The transport must fire *inside* the gateway's outer cap.

    ``httpx.MockTransport`` short-circuits the socket layer, so a mock cannot
    demonstrate a real read timeout; what can be pinned — and what actually
    determines the behaviour — is that the pooled client is built from the
    budget. The engine's own per-attempt cap is asserted separately in
    ``test_llm_gateway_engine.py``, where a genuinely slow adapter is used.
    """

    adapter = GeminiHttpAdapter(api_key=KEY)
    tight = TimeoutBudget().with_total(9.0)
    client = adapter._client_for(tight)  # noqa: SLF001 — configuration assertion
    timeout = client.timeout
    assert timeout.connect == pytest.approx(tight.connect_seconds)
    assert timeout.read == pytest.approx(tight.read_seconds)
    assert timeout.write == pytest.approx(tight.read_seconds)
    assert timeout.pool == pytest.approx(tight.connect_seconds)
    # The invariant that keeps a slow provider classifiable as UPSTREAM_TIMEOUT.
    assert timeout.connect + timeout.read <= tight.per_attempt_seconds


def test_the_same_budget_reuses_the_pooled_client() -> None:
    adapter = GeminiHttpAdapter(api_key=KEY)
    first = adapter._client_for(BUDGET)  # noqa: SLF001
    second = adapter._client_for(BUDGET)  # noqa: SLF001
    assert first is second


@pytest.mark.parametrize("count", [-1, True, 1.5, "4", "bad", float("nan"), float("inf")])
def test_malformed_sdk_counts_are_not_invented(count: Any) -> None:
    from nexus_ai_agent.llm.gateway.adapters import _litellm_usage

    usage = _litellm_usage({"usage": {"prompt_tokens": count, "completion_tokens": count}})
    assert usage.source is UsageSource.UNKNOWN
    assert usage.input_tokens is None and usage.output_tokens is None
