"""Smart Fallback Provider — transparently degrades on typed, retryable failures.

When the primary provider raises a typed :class:`LLMError` whose ``kind`` is
retryable (``RATE_LIMIT`` / ``TIMEOUT`` / ``UNAVAILABLE``), ``FallbackProvider``
degrades to ``FakeLLMProvider`` and appends a disclaimer. Failure is decided by
*type and status*, never by searching model text for keywords (LAW 10): a
successful answer that merely mentions "429" is returned unchanged.

Usage:
    provider = FallbackProvider(primary=gemini, fallback=fake)
    result = await provider.generate("Hello")  # primary answer, or fallback on typed error
"""

from __future__ import annotations

from typing import Any

import structlog

from nexus_ai_agent.llm.errors import LLMError
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

    Failure is decided by **type, never by text** (LAW 10 / Wave D):

    - If ``primary.generate()`` returns a non-empty string → that is a
      *successful generation* and is returned verbatim. Model text that merely
      mentions ``429`` / ``quota`` / ``rate limit`` stays a success.
    - If ``primary.generate()`` raises a typed :class:`LLMError` whose ``kind``
      is retryable (``RATE_LIMIT`` / ``TIMEOUT`` / ``UNAVAILABLE``) → degrade to
      ``fallback.generate()`` with a disclaimer appended.
    - Any other exception (typed non-retryable *or* untyped) propagates — a bad
      or unexpected response is never silently replaced by a degraded answer.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        *,
        disclaimer: str = _FALLBACK_DISCLAIMER,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or FakeLLMProvider()
        self._disclaimer = disclaimer
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
        """Return the primary's answer; degrade only on a typed retryable error.

        A normally-returned, non-empty string is a *successful generation* and
        is returned as-is. Model-generated text is never searched for error
        keywords (LAW 10): an answer that merely mentions "429"/"quota" stays a
        success. Degradation is driven solely by a typed :class:`LLMError`.
        """
        try:
            result = await self._primary.generate(prompt, system)
        except LLMError as exc:
            self._primary_count += 1
            if exc.retryable:
                logger.warning("primary_retryable_failure", kind=exc.kind, error=str(exc)[:100])
                return await self._do_fallback(prompt, system)
            # Typed but non-retryable (e.g. INVALID_RESPONSE): fail closed.
            raise
        except Exception:
            # Untyped failure: never infer a rate-limit from message text.
            self._primary_count += 1
            raise
        else:
            self._primary_count += 1
            return result

    async def _do_fallback(self, prompt: str, system: str) -> str:
        """Execute fallback provider and append disclaimer."""
        self._fallback_count += 1
        logger.info("using_fallback_provider", prompt_len=len(prompt))
        try:
            result = await self._fallback.generate(prompt, system)
        except Exception as fallback_exc:
            logger.error("fallback_also_failed", error=str(fallback_exc)[:100])
            # Return a user-friendly message rather than crashing
            return (
                "⚠️ Sorry, both the primary and backup AI engines are currently unavailable. "
                "Please try again in a few minutes."
            )
        # The fallback's output is itself model text — annotate, don't re-scan.
        if result:
            return result + self._disclaimer
        return result

    async def embed(self, text: str) -> list[float]:
        """Always use primary for embeddings — fallback doesn't support real embeddings."""
        return await self._primary.embed(text)
