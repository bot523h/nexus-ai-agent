"""Pollinations text-to-image adapter (no paid fallback)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .provider import GeneratedImage, ImageRequest
from .resilience import CachedImageAdapter, validate_image


class PollinationsAdapter(CachedImageAdapter):
    def __init__(self, *, model: str = "flux", **kwargs: Any) -> None:
        super().__init__(
            provider="pollinations", model=model, estimated_cost_usd=Decimal("0"), **kwargs
        )

    async def _generate(self, client: httpx.AsyncClient, request: ImageRequest) -> GeneratedImage:
        params: dict[str, str | int] = {
            "model": self.model,
            "width": request.width,
            "height": request.height,
            "nologo": "true",
            "nofeed": "true",
        }
        if request.seed is not None:
            params["seed"] = request.seed
        http_request = client.build_request(
            "GET",
            "https://image.pollinations.ai/prompt/" + quote(request.prompt, safe=""),
            params=params,
        )
        return validate_image(await self._send(client, http_request))
