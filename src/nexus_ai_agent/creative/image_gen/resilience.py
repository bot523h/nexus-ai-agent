"""Bounded async retries and per-adapter, TTL/LRU prompt-hash caching."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from decimal import Decimal

import httpx
from PIL import Image, UnidentifiedImageError

from nexus_ai_agent.observability.logging import get_logger

from .provider import GeneratedImage, ImageGenerationError, ImageRequest

log = get_logger(__name__)
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 5:
            raise ValueError("retry attempts must be between one and five")
        if not 0 <= self.base_delay <= self.max_delay <= 60:
            raise ValueError("retry delays must be bounded by sixty seconds")


def validate_image(data: bytes) -> GeneratedImage:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("provider image exceeds size limits or is empty")
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
                image.format or ""
            )
            if mime is None or image.width * image.height > 16_777_216:
                raise ImageGenerationError("unsupported provider image")
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ImageGenerationError("provider returned an invalid image") from None
    return GeneratedImage(data, mime)


class CachedImageAdapter:
    """One bounded session cache, including single-flight for concurrent requests.

    A per-instance lock deliberately serializes provider calls to avoid bursts and
    duplicate billing. Cancellation propagates; failed results are never cached.
    HTTP clients are scoped to each cache miss so no connection pool leaks.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        estimated_cost_usd: Decimal,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl: float = 3600,
        cache_entries: int = 16,
        cache_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if cache_ttl <= 0 or cache_entries < 1 or cache_bytes < 1:
            raise ValueError("cache bounds must be positive")
        if not estimated_cost_usd.is_finite() or estimated_cost_usd < 0:
            raise ValueError("estimated cost must be finite and non-negative")
        self.provider = provider
        self.model = model
        self.estimated_cost_usd = estimated_cost_usd
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._ttl = cache_ttl
        self._entries = cache_entries
        self._byte_limit = cache_bytes
        self._cache: OrderedDict[str, tuple[float, GeneratedImage]] = OrderedDict()
        self._lock = asyncio.Lock()

    def _authorize(self) -> None:
        """Adapters may reject a request before cache access or network I/O."""

    async def generate(self, request: ImageRequest) -> GeneratedImage:
        self._authorize()
        key = hashlib.sha256(
            json.dumps([self.provider, self.model, asdict(request)], sort_keys=True).encode()
        ).hexdigest()
        async with self._lock:
            self._authorize()
            now = self._clock()
            for expired in [k for k, (ts, _) in self._cache.items() if now - ts >= self._ttl]:
                del self._cache[expired]
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key][1]
            async with httpx.AsyncClient(
                transport=self._transport, timeout=90.0, follow_redirects=False, trust_env=False
            ) as client:
                result = await self._generate(client, request)
            if len(result.data) <= self._byte_limit:
                self._cache[key] = (self._clock(), result)
                while (
                    len(self._cache) > self._entries
                    or sum(len(item.data) for _, item in self._cache.values()) > self._byte_limit
                ):
                    self._cache.popitem(last=False)
            return result

    async def _generate(self, client: httpx.AsyncClient, request: ImageRequest) -> GeneratedImage:
        raise NotImplementedError

    async def _send(self, client: httpx.AsyncClient, request: httpx.Request) -> bytes:
        for attempt in range(1, self._retry.attempts + 1):
            delay = min(self._retry.max_delay, self._retry.base_delay * 2 ** (attempt - 1))
            try:
                response = await client.send(request, stream=True)
                try:
                    status = response.status_code
                    if 200 <= status < 300:
                        # Log each successful HTTP response before decoding: a malformed
                        # image may still be billable. Estimates are not invoice totals.
                        log.info(
                            "image_generation_cost",
                            provider=self.provider,
                            model=self.model,
                            estimated_cost_usd=str(self.estimated_cost_usd),
                            attempt=attempt,
                        )
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise ImageGenerationError("provider response exceeds size limit")
                        return bytes(data)
                    if status != 429 and not 500 <= status < 600:
                        raise ImageGenerationError(
                            f"image provider rejected request (HTTP {status})"
                        )
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(self._retry.max_delay, max(delay, float(retry_after)))
                    except ValueError:
                        pass
                finally:
                    await response.aclose()
            except httpx.TransportError:
                # Never log raw HTTP exceptions: Pollinations URLs contain the prompt.
                pass
            log.warning("image_generation_retry", provider=self.provider, attempt=attempt)
            if attempt < self._retry.attempts:
                await self._sleep(min(self._retry.max_delay, delay + random.uniform(0, delay / 4)))
        raise ImageGenerationError("image provider unavailable after bounded retries") from None
