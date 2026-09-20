"""Offline contracts for image provider caching and retry behavior."""

from __future__ import annotations

import asyncio
import base64
import io
from decimal import Decimal

# Every failure/cache assertion runs against both transports, never live APIs.
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image

from nexus_ai_agent.creative.image_gen import (
    ImageGenerationError,
    ImageRequest,
    PaidTierRequiredError,
    resilience,
)
from nexus_ai_agent.creative.image_gen.gemini_adapter import GeminiAdapter
from nexus_ai_agent.creative.image_gen.pollinations_adapter import PollinationsAdapter
from nexus_ai_agent.creative.image_gen.resilience import CachedImageAdapter, RetryPolicy


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


async def test_pollinations_retries_then_caches_concurrent_identical_prompts() -> None:
    calls: list[httpx.Request] = []
    sleep = AsyncMock()

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=image_bytes())

    provider = PollinationsAdapter(transport=httpx.MockTransport(respond), sleep=sleep)
    request = ImageRequest("a blue ocean? #sunrise / reflection")
    first, second = await asyncio.gather(provider.generate(request), provider.generate(request))
    assert first == second
    assert first.extension == ".png"
    assert len(calls) == 2
    sleep.assert_awaited_once()
    assert calls[-1].url.params["width"] == "1024"
    assert not calls[-1].url.fragment


async def test_gemini_caches_only_after_decoding_an_image() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"data": base64.b64encode(image_bytes()).decode()}}
                            ]
                        }
                    }
                ]
            },
        )

    provider = GeminiAdapter(
        api_key="test-key",
        paid_tier=True,
        estimated_cost_usd=Decimal("0.04"),
        transport=httpx.MockTransport(respond),
    )
    request = ImageRequest("a blue ocean")
    assert await provider.generate(request) == await provider.generate(request)
    assert len(calls) == 1
    assert calls[0].headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in str(calls[0].url)


def provider_for(kind: str, transport: httpx.MockTransport, **kwargs: object) -> CachedImageAdapter:
    if kind == "gemini":
        return GeminiAdapter(
            api_key="private-api-key",
            paid_tier=True,
            estimated_cost_usd=Decimal("0.04"),
            transport=transport,
            **kwargs,
        )
    return PollinationsAdapter(transport=transport, **kwargs)


def successful_response(kind: str) -> httpx.Response:
    if kind == "gemini":
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"data": base64.b64encode(image_bytes()).decode()}}
                            ]
                        }
                    }
                ]
            },
        )
    return httpx.Response(200, content=image_bytes())


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
@pytest.mark.parametrize("failure", ["connect", "timeout", "rate_limit", "server"])
async def test_transient_errors_retry_with_bounded_delay_and_one_success_cost(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: str,
) -> None:
    calls = 0
    sleep = AsyncMock()
    log = Mock()
    monkeypatch.setattr(resilience, "log", log)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "connect":
                raise httpx.ConnectError("private-prompt", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("private-api-key", request=request)
            return httpx.Response(
                429 if failure == "rate_limit" else 503, headers={"Retry-After": "9000"}
            )
        return successful_response(kind)

    provider = provider_for(kind, httpx.MockTransport(respond), sleep=sleep)
    await provider.generate(ImageRequest("private-prompt"))
    assert calls == 2
    sleep.assert_awaited_once()
    assert 0.5 <= sleep.await_args.args[0] <= 8
    log.info.assert_called_once_with(
        "image_generation_cost",
        provider=kind,
        model=provider.model,
        estimated_cost_usd="0.04" if kind == "gemini" else "0",
        attempt=2,
    )
    assert "private-api-key" not in str(log.mock_calls)
    assert "private-prompt" not in str(log.mock_calls)


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
async def test_exhausted_network_errors_are_not_cached_or_claimed_as_successful_cost(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    calls = 0
    log = Mock()
    monkeypatch.setattr(resilience, "log", log)
    sleep = AsyncMock()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("sensitive URL", request=request)

    provider = provider_for(kind, httpx.MockTransport(respond), sleep=sleep)
    for _ in range(2):
        with pytest.raises(ImageGenerationError, match="bounded retries") as error:
            await provider.generate(ImageRequest("ocean"))
        assert "sensitive URL" not in str(error.value)
    assert calls == 6 and sleep.await_count == 4
    log.info.assert_not_called()
    assert not provider._cache


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 302])
async def test_permanent_failures_and_redirects_never_retry(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    status: int,
) -> None:
    log = Mock()
    monkeypatch.setattr(resilience, "log", log)
    respond = Mock(return_value=httpx.Response(status, headers={"Location": "http://127.0.0.1/"}))
    sleep = AsyncMock()
    provider = provider_for(kind, httpx.MockTransport(respond), sleep=sleep)
    with pytest.raises(ImageGenerationError, match=f"HTTP {status}"):
        await provider.generate(ImageRequest("ocean"))
    respond.assert_called_once()
    sleep.assert_not_awaited()
    log.info.assert_not_called()


async def test_paid_tier_false_rejects_before_network_and_even_before_cache_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Mock()
    monkeypatch.setattr(resilience, "log", log)
    respond = Mock(return_value=successful_response("gemini"))
    provider = GeminiAdapter(
        api_key="private-api-key",
        paid_tier=False,
        estimated_cost_usd=Decimal("0.04"),
        transport=httpx.MockTransport(respond),
    )
    request = ImageRequest("ocean")
    with pytest.raises(PaidTierRequiredError):
        await provider.generate(request)
    respond.assert_not_called()
    log.info.assert_not_called()

    provider.paid_tier = True
    await provider.generate(request)
    await provider.generate(request)  # cache hit must not emit another billable event
    respond.assert_called_once()
    log.info.assert_called_once_with(
        "image_generation_cost",
        provider="gemini",
        model="gemini-2.5-flash-image",
        estimated_cost_usd="0.04",
        attempt=1,
    )
    provider.paid_tier = False
    with pytest.raises(PaidTierRequiredError):
        await provider.generate(request)
    assert respond.call_count == 1 and log.info.call_count == 1


@pytest.mark.parametrize("key,cost", [("", "0.04"), ("key", "0")])
async def test_gemini_missing_credentials_or_estimate_fails_before_network(
    key: str, cost: str
) -> None:
    respond = Mock()
    provider = GeminiAdapter(
        api_key=key,
        paid_tier=True,
        estimated_cost_usd=Decimal(cost),
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ImageGenerationError):
        await provider.generate(ImageRequest("ocean"))
    respond.assert_not_called()


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
async def test_malformed_success_is_not_cached_but_is_logged_as_potentially_billable(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    log = Mock()
    monkeypatch.setattr(resilience, "log", log)
    respond = Mock(return_value=httpx.Response(200, content=b"not an image or JSON"))
    sleep = AsyncMock()
    provider = provider_for(kind, httpx.MockTransport(respond), sleep=sleep)
    for _ in range(2):
        with pytest.raises(ImageGenerationError):
            await provider.generate(ImageRequest("ocean"))
    assert respond.call_count == 2 and log.info.call_count == 2
    assert not provider._cache
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"candidates": []},
        {"candidates": [{"content": {"parts": [{"text": "refused"}]}}]},
        {"candidates": [{"content": {"parts": [{"inlineData": {"data": "!!!!"}}]}}]},
        {"candidates": None},
        [],
    ],
)
async def test_gemini_safety_refusal_or_invalid_shape_is_not_an_image(payload: object) -> None:
    provider = provider_for(
        "gemini", httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(ImageGenerationError):
        await provider.generate(ImageRequest("ocean"))
    assert not provider._cache


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
async def test_prompt_dimensions_and_ttl_are_part_of_cache_behavior(kind: str) -> None:
    now = [0.0]
    respond = Mock(side_effect=lambda _: successful_response(kind))
    provider = provider_for(kind, httpx.MockTransport(respond), clock=lambda: now[0], cache_ttl=10)
    request = ImageRequest("ocean")
    await provider.generate(request)
    await provider.generate(request)
    assert respond.call_count == 1
    await provider.generate(ImageRequest("forest"))
    await provider.generate(ImageRequest("ocean", width=1280, height=720))
    assert respond.call_count == 3
    now[0] = 10.0
    await provider.generate(request)
    assert respond.call_count == 4
    assert len(provider._cache) == 1
    assert all("ocean" not in key and len(key) == 64 for key in provider._cache)


async def test_pollinations_seed_separates_cache_entries() -> None:
    respond = Mock(side_effect=lambda _: successful_response("pollinations"))
    provider = provider_for("pollinations", httpx.MockTransport(respond))
    for seed in [0, 0, 1, None]:
        await provider.generate(ImageRequest("ocean", seed=seed))
    assert respond.call_count == 3


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
@pytest.mark.parametrize("limit", ["entries", "bytes"])
async def test_cache_enforces_lru_entry_and_byte_limits(kind: str, limit: str) -> None:
    respond = Mock(side_effect=lambda _: successful_response(kind))
    kwargs = {"cache_entries": 2} if limit == "entries" else {"cache_bytes": 2 * len(image_bytes())}
    provider = provider_for(kind, httpx.MockTransport(respond), **kwargs)
    for prompt in ["A", "B", "A", "C", "A"]:
        await provider.generate(ImageRequest(prompt))
    assert respond.call_count == 3  # refreshing A evicted B, not A
    await provider.generate(ImageRequest("B"))
    assert respond.call_count == 4
    assert len(provider._cache) == 2


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
async def test_cancelled_request_is_not_retried_and_lock_is_released(kind: str) -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return successful_response(kind)

    sleep = AsyncMock()
    provider = provider_for(kind, httpx.MockTransport(respond), sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await provider.generate(ImageRequest("ocean"))
    assert not provider._cache
    await asyncio.wait_for(provider.generate(ImageRequest("ocean")), timeout=1)
    assert calls == 2
    sleep.assert_not_awaited()


@pytest.mark.parametrize("kind", ["pollinations", "gemini"])
async def test_response_size_is_bounded_and_failures_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(resilience, "MAX_RESPONSE_BYTES", 16)
    respond = Mock(return_value=httpx.Response(200, content=b"x" * 17))
    provider = provider_for(kind, httpx.MockTransport(respond))
    with pytest.raises(ImageGenerationError, match="size limit"):
        await provider.generate(ImageRequest("ocean"))
    respond.assert_called_once()
    assert not provider._cache


@pytest.mark.parametrize("attempts", [0, 6])
def test_retry_attempts_are_bounded(attempts: int) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=attempts)
