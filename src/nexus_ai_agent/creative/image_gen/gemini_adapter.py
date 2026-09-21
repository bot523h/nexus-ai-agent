"""Paid Gemini image generation with a fail-closed operator guard."""

from __future__ import annotations

import base64
import binascii
import json
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .provider import GeneratedImage, ImageGenerationError, ImageRequest, PaidTierRequiredError
from .resilience import CachedImageAdapter, validate_image


class GeminiAdapter(CachedImageAdapter):
    def __init__(
        self,
        *,
        api_key: str,
        paid_tier: bool = False,
        model: str = "gemini-2.5-flash-image",
        estimated_cost_usd: Decimal,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            provider="gemini", model=model, estimated_cost_usd=estimated_cost_usd, **kwargs
        )
        self._api_key = api_key
        self.paid_tier = paid_tier

    def _authorize(self) -> None:
        if not self.paid_tier:
            raise PaidTierRequiredError("Gemini image generation requires paid_tier=True")
        if not self._api_key:
            raise ImageGenerationError("Gemini API key is not configured")
        if self.estimated_cost_usd <= 0:
            raise ImageGenerationError("Gemini requires a positive operator cost estimate")

    async def _generate(self, client: httpx.AsyncClient, request: ImageRequest) -> GeneratedImage:
        if request.seed is not None:
            raise ValueError("Gemini image generation does not support seeds")
        ratios = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
        ratio = next(
            (
                r
                for r in ratios
                if request.width * int(r.split(":")[1]) == request.height * int(r.split(":")[0])
            ),
            None,
        )
        if ratio is None:
            raise ValueError("unsupported Gemini image aspect ratio")
        http_request = client.build_request(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + quote(self.model, safe="")
            + ":generateContent",
            headers={"x-goog-api-key": self._api_key},
            json={
                "contents": [{"parts": [{"text": request.prompt}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": ratio},
                },
            },
        )
        raw = await self._send(client, http_request)
        try:
            payload = json.loads(raw)
            for candidate in payload.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    inline = part.get("inlineData")
                    if inline:
                        return validate_image(base64.b64decode(inline["data"], validate=True))
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            raise ImageGenerationError("Gemini returned an invalid image response") from None
        raise ImageGenerationError("Gemini returned no image (possibly safety-filtered)")
