"""Resilient HTTP client — retry, timeout, and circuit breaker (v3.1.0).

Use this everywhere instead of bare `httpx.AsyncClient()`. It handles:
- Connection/read timeouts (default 10s)
- Exponential backoff retry on transient failures (default 3 attempts)
- Circuit breaker: after N consecutive failures, fail fast for cooldown period
- Structured logging for every request

Usage:
    client = ResilientHttpClient()
    data = await client.get_json("https://api.example.com/data")
    text = await client.get_text("https://example.com/page")

Why this exists:
- v3.0.0 free_tools.py and knowledge/ used raw httpx with no retry,
  no timeout enforcement, no circuit breaker.
- When DuckDuckGo or Wikipedia got slow, the bot would hang indefinitely.
- One client = one consistent failure model across the entire codebase.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class CircuitBreakerState:
    """Per-host circuit breaker state."""

    failures: int = 0
    opened_at: float = 0.0
    threshold: int = 5
    cooldown_seconds: float = 30.0

    def is_open(self) -> bool:
        """True if breaker is open (fail-fast mode)."""
        if self.failures < self.threshold:
            return False
        # Auto-reset after cooldown
        if time.time() - self.opened_at > self.cooldown_seconds:
            self.failures = 0
            self.opened_at = 0.0
            return False
        return True

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at == 0.0:
            self.opened_at = time.time()
            logger.warning("circuit_breaker_opened", failures=self.failures)

    def record_success(self) -> None:
        if self.failures > 0:
            logger.info("circuit_breaker_recovered", prev_failures=self.failures)
        self.failures = 0
        self.opened_at = 0.0


@dataclass
class RetryConfig:
    """Retry policy configuration."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 5.0
    backoff_factor: float = 2.0


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open and request is short-circuited."""


class ResilientHttpClient:
    """HTTP client with retry, timeout, and per-host circuit breaker."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        retry: RetryConfig | None = None,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 30.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._retry = retry or RetryConfig()
        self._circuit_threshold = circuit_threshold
        self._circuit_cooldown = circuit_cooldown
        self._default_headers = default_headers or {
            "User-Agent": "NEXUS-AI-Agent/3.1.0 (+https://github.com/bot523h/nexus-ai-agent)"
        }
        self._breakers: dict[str, CircuitBreakerState] = {}

    def _get_breaker(self, host: str) -> CircuitBreakerState:
        if host not in self._breakers:
            self._breakers[host] = CircuitBreakerState(
                threshold=self._circuit_threshold,
                cooldown_seconds=self._circuit_cooldown,
            )
        return self._breakers[host]

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return httpx.URL(url).host or "unknown"
        except (httpx.InvalidURL, ValueError):
            return "unknown"

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute a request with retry + circuit breaker."""
        host = self._host_of(url)
        breaker = self._get_breaker(host)

        if breaker.is_open():
            logger.warning("circuit_breaker_short_circuit", host=host, url=url)
            raise CircuitOpenError(f"Circuit breaker open for {host}")

        last_exc: Exception | None = None
        delay = self._retry.base_delay

        merged_headers = {**self._default_headers, **kwargs.pop("headers", {})}
        timeout = kwargs.pop("timeout", self._timeout)

        for attempt in range(1, self._retry.max_attempts + 1):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                ) as client:
                    response = await client.request(method, url, headers=merged_headers, **kwargs)
                latency = (time.monotonic() - start) * 1000
                logger.info(
                    "http_request",
                    method=method,
                    host=host,
                    status=response.status_code,
                    attempt=attempt,
                    latency_ms=round(latency, 1),
                )
                # 5xx → retry; 4xx → don't retry, return as-is
                if 500 <= response.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                breaker.record_success()
                return response
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.HTTPStatusError,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                latency = (time.monotonic() - start) * 1000
                logger.warning(
                    "http_request_failed",
                    method=method,
                    host=host,
                    attempt=attempt,
                    error=type(exc).__name__,
                    latency_ms=round(latency, 1),
                )
                if attempt < self._retry.max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * self._retry.backoff_factor, self._retry.max_delay)

        breaker.record_failure()
        assert last_exc is not None
        raise last_exc

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET request with retry + circuit breaker."""
        return await self._request_with_retry("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST request with retry + circuit breaker."""
        return await self._request_with_retry("POST", url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """GET and parse JSON. Returns {} on parse failure."""
        try:
            response = await self.get(url, **kwargs)
            data: Any = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except (httpx.HTTPError, ValueError, CircuitOpenError) as exc:
            logger.error("get_json_failed", url=url, error=str(exc))
            return {}

    async def get_text(self, url: str, **kwargs: Any) -> str:
        """GET and return text. Returns '' on failure."""
        try:
            response = await self.get(url, **kwargs)
            return response.text
        except (httpx.HTTPError, CircuitOpenError) as exc:
            logger.error("get_text_failed", url=url, error=str(exc))
            return ""

    def get_breaker_status(self) -> dict[str, dict[str, Any]]:
        """Return current circuit breaker state for all hosts (for /health)."""
        return {
            host: {
                "open": br.is_open(),
                "failures": br.failures,
                "threshold": br.threshold,
            }
            for host, br in self._breakers.items()
        }


# Module-level singleton (cheap to share — httpx clients created per-request)
_default_client: ResilientHttpClient | None = None


def get_http_client() -> ResilientHttpClient:
    """Return a process-wide default HTTP client (lazy singleton)."""
    global _default_client
    if _default_client is None:
        _default_client = ResilientHttpClient()
    return _default_client
