"""Degradation shim — typed fallback from a drained primary to a local answer.

When the primary provider is rate-limited or its quota is drained, this layer
serves a locally generated answer with a clear disclaimer so the Telegram graph
degrades instead of crashing.

W2 (Global LLM Gateway) changed *how* that decision is made, not *what* it does.

Before
------
The decision was made by scanning text::

    if any(kw in result.lower() for kw in ("429", "rate limit", "quota", ...)):
    if any(kw in str(exc).lower() for kw in ("429", "rate limit", ...)):

That is the anti-pattern the gateway exists to remove (LAW 3). It fails in both
directions: a perfectly good answer that happens to contain the word "quota"
(e.g. a summary about disk quotas) was thrown away and replaced by a fake one,
and a genuine rate limit whose message did not contain one of four English words
propagated as a crash. It also forced ``RouterExhaustedError`` to carry magic
words in its message purely so this layer could recognise it.

After
-----
The primary raises a typed :class:`~nexus_ai_agent.llm.errors.LLMError` and this
layer asks it two questions that were already answered upstream, from evidence:

* ``exc.fallback_eligible`` — would a different route plausibly succeed?
  (``CONTENT_BLOCKED`` and ``CANCELLED`` are deliberately *not* eligible.)
* ``exc.kind`` — recorded, so an operator can see *why* we degraded.

Nothing here reads a message. ``error_keywords`` survives as a constructor
parameter for compatibility, but it defaults to **empty**: scanning a returned
answer for error words is opt-in, logged as deprecated when it fires, and only
exists for a legacy primary that cannot raise typed errors.
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

_BOTH_UNAVAILABLE = (
    "⚠️ Sorry, both the primary and backup AI engines are currently unavailable. "
    "Please try again in a few minutes."
)


class FallbackProvider(LLMProvider):
    """Wrap a primary and a local fallback with typed, policy-driven degradation.

    - primary succeeds → the result is returned as-is;
    - primary raises a fallback-eligible :class:`LLMError` → the fallback answers
      and the disclaimer is appended, so a degraded answer is never silent
      (LAW 8);
    - primary raises anything else → it propagates. An ``AUTHENTICATION`` failure
      or a programming error must not be dressed up as "the service is busy";
    - the fallback also fails → one honest "both unavailable" message.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        *,
        disclaimer: str = _FALLBACK_DISCLAIMER,
        error_keywords: tuple[str, ...] = (),
        allow_degraded: bool = True,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or FakeLLMProvider()
        self._disclaimer = disclaimer
        #: Deprecated opt-in. Empty by default: a modern primary raises typed
        #: errors, so there is nothing to scan. See the module docstring.
        self._error_keywords = tuple(error_keywords)
        #: Set ``False`` to make this a pass-through (no degradation at all).
        self._allow_degraded = allow_degraded
        self._fallback_count: int = 0
        self._primary_count: int = 0
        self._keyword_hits: int = 0
        self._last_kind: str | None = None
        if self._error_keywords:
            logger.warning(
                "fallback_provider_keyword_scanning_enabled",
                keywords=list(self._error_keywords),
                hint=(
                    "substring-based error detection is deprecated; make the primary "
                    "raise nexus_ai_agent.llm.errors.LLMError instead"
                ),
            )

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
            "last_fallback_kind": self._last_kind,
            "keyword_hits": self._keyword_hits,
        }

    async def generate(self, prompt: str, system: str = "") -> str:
        """Try the primary; degrade only on a typed, fallback-eligible failure."""

        try:
            result = await self._primary.generate(prompt, system)
        except LLMError as exc:
            self._primary_count += 1
            if self._allow_degraded and exc.fallback_eligible:
                self._last_kind = exc.kind.value
                logger.warning(
                    "primary_failed_degrading",
                    error_kind=exc.kind.value,
                    status=exc.status_code,
                    provider=exc.provider,
                    request_id=exc.request_id,
                )
                return await self._do_fallback(prompt, system)
            # Not eligible (blocked content, authentication, a caller bug, a
            # cancellation): propagate the typed truth to whoever can act on it.
            logger.error(
                "primary_failed_not_eligible",
                error_kind=exc.kind.value,
                status=exc.status_code,
                provider=exc.provider,
            )
            raise
        except Exception:
            self._primary_count += 1
            # An untyped failure carries no evidence about retryability. Guessing
            # from its message is exactly what this rewrite removed.
            raise

        self._primary_count += 1
        if self._error_keywords and result and self._allow_degraded:
            lowered = result.lower()
            if any(keyword in lowered for keyword in self._error_keywords):
                self._keyword_hits += 1
                self._last_kind = "legacy_keyword_match"
                logger.warning(
                    "primary_returned_error_shaped_string",
                    result_preview=result[:100],
                    hint="deprecated substring detection fired; make the primary typed",
                )
                return await self._do_fallback(prompt, system)
        return result

    async def _do_fallback(self, prompt: str, system: str) -> str:
        """Execute the local fallback and mark the answer as degraded."""

        self._fallback_count += 1
        logger.info("using_fallback_provider", prompt_len=len(prompt))
        try:
            result = await self._fallback.generate(prompt, system)
        except Exception as fallback_exc:
            logger.error(
                "fallback_also_failed",
                error_type=type(fallback_exc).__name__,
                error_kind=(
                    fallback_exc.kind.value if isinstance(fallback_exc, LLMError) else None
                ),
            )
            return _BOTH_UNAVAILABLE
        if not result:
            return _BOTH_UNAVAILABLE
        # The disclaimer is unconditional: the degradation happened whether or not
        # the local answer happens to contain a word like "quota".
        return result + self._disclaimer

    async def embed(self, text: str) -> list[float]:
        """Always the primary — the local fallback has no real embeddings."""

        return await self._primary.embed(text)
