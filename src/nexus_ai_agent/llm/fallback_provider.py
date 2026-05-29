"""Smart Fallback Provider — transparently degrades when Gemini rate limits hit.

When the primary GeminiProvider returns a 429 or daily limit exceeded error,
FallbackProvider intercepts the failure and retries with FakeLLMProvider,
appending a clear disclaimer so the user knows they're getting a degraded response.

Usage:
    provider = FallbackProvider(primary=gemini, fallback=fake)
    result = await provider.generate("Hello")  # tries Gemini first, falls back if 429
"""

from __future__ import annotations

from typing import Any

import structlog

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.provider import LLMProvider

logger = structlog.get_logger(__name__)

_FALLBACK_DISCLAIMER = (
    "\n\n---\n⚠️ _Fallback mode_: The primary AI engine is currently rate-limited. "
    "This response was generated locally and may be less accurate. "
    "Please try again in a minute for the full AI experience._"
)


class FallbackProvider(LLMProvider):
    """Wraps a primary and fallback provider with automatic degradation.

    - If `primary.generate()` succeeds → returns the result as-is.
    - If `primary.generate()` raises RateLimitError or returns an error string
      containing '429' or 'rate limit' → falls back to `fallback.generate()`
      with a disclaimer appended.
    - If the fallback also fails → returns the original error.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        *,
        disclaimer: str = _FALLBACK_DISCLAIMER,
        error_keywords: tuple[str, ...] = ("429", "rate limit", "quota", "daily limit"),
    ) -> None:
        self._primary = primary
        self._fallback = fallback or FakeLLMProvider()
        self._disclaimer = disclaimer
        self._error_keywords = error_keywords
        self._fallback_count: int = 0
        self._primary_count: int = 0

    @property
    def primary(self) -> LLMProvider:
        return self._primary

    @property
    def fallback(self) -> LLMProvider:
        return self._fallback

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "primary_calls": self._primary_count,
            "fallback_calls": self._fallback_count,
            "fallback_ratio": (
                round(self._fallback_count / max(self._primary_count + self._fallback_count, 1), 2)
            ),
        }

    async def generate(self, prompt: str, system: str = "") -> str:
        """Try primary; on rate-limit errors, fall back with disclaimer."""
        try:
            result = await self._primary.generate(prompt, system)
            self._primary_count += 1

            # Check if the result itself indicates a rate-limit error
            # (GeminiEngine returns error strings rather than raising)
            if result and any(kw in result.lower() for kw in self._error_keywords):
                logger.warning("primary_returned_rate_limit", result_preview=result[:100])
                return await self._do_fallback(prompt, system)

            return result

        except Exception as exc:
            self._primary_count += 1
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in self._error_keywords):
                logger.warning("primary_rate_limited", error=str(exc)[:100])
                return await self._do_fallback(prompt, system)
            # Non-rate-limit error: propagate
            raise

    async def _do_fallback(self, prompt: str, system: str) -> str:
        """Execute fallback provider and append disclaimer."""
        self._fallback_count += 1
        logger.info("using_fallback_provider", prompt_len=len(prompt))
        try:
            result = await self._fallback.generate(prompt, system)
            if result and not any(kw in result.lower() for kw in self._error_keywords):
                return result + self._disclaimer
            return result
        except Exception as fallback_exc:
            logger.error("fallback_also_failed", error=str(fallback_exc)[:100])
            # Return a user-friendly message rather than crashing
            return (
                "⚠️ Sorry, both the primary and backup AI engines are currently unavailable. "
                "Please try again in a few minutes."
            )

    async def embed(self, text: str) -> list[float]:
        """Always use primary for embeddings — fallback doesn't support real embeddings."""
        return await self._primary.embed(text)
