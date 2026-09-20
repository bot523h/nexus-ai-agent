"""Offline contracts for image provider caching and retry behavior."""

from __future__ import annotations

import asyncio
import base64
import io
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
from PIL import Image

from nexus_ai_agent.creative.image_gen import ImageRequest
from nexus_ai_agent.creative.image_gen.gemini_adapter import GeminiAdapter
from nexus_ai_agent.creative.image_gen.pollinations_adapter import PollinationsAdapter


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
