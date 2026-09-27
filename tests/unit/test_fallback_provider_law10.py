"""LAW 10 / Wave D — FallbackProvider must not derive provider errors from model text.

Regression + mutation guards for the contract that separates the channels the
``FallbackProvider`` decides on:

* provider *exception* (typed)        → may trigger a retryable fallback,
* provider *status* / rate-limit      → typed, machine-readable,
* *model-generated text*              → ALWAYS a success, never an error signal.

The historical bug (fixed on this branch): the success channel substring-scanned
the primary's returned text for keywords like ``"429"`` / ``"quota"`` /
``"rate limit"`` and, on a match, discarded a perfectly good answer and degraded
to the local fallback. A legitimate answer that merely *mentions* HTTP 429 must
stay a success.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.llm.errors import LLMError
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.provider import LLMProvider


class _StubProvider(LLMProvider):
    """A programmable primary for exercising the FallbackProvider contract."""

    def __init__(
        self,
        *,
        result: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls = 0

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result

    async def embed(self, text: str) -> list[float]:
        return [0.0]


# ── THE REGRESSION: model text that merely mentions 429 stays a success ──────


async def test_successful_output_mentioning_429_stays_success() -> None:
    """A real answer explaining HTTP 429 must NOT be treated as a rate-limit."""
    answer = (
        "To handle HTTP 429 responses, honour the Retry-After header and use "
        "exponential backoff; do not hammer the endpoint."
    )
    primary = _StubProvider(result=answer)
    llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())

    result = await llm.generate("how do I handle 429s?")

    assert result == answer  # exact success passthrough, no disclaimer appended
    assert primary.calls == 1
    assert llm.stats["fallback_calls"] == 0
    assert "Fallback mode" not in result


async def test_successful_output_mentioning_quota_and_rate_limit_stays_success() -> None:
    answer = "Your daily quota resets at midnight UTC; the rate limit is 60 req/min."
    primary = _StubProvider(result=answer)
    llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())

    result = await llm.generate("what are the limits?")

    assert result == answer
    assert llm.stats["fallback_calls"] == 0


# ── TYPED failure channel: retryable errors still degrade, fail-closed ───────


async def test_typed_rate_limit_falls_back_with_disclaimer() -> None:
    primary = _StubProvider(raises=LLMError("429 Too Many Requests", kind="RATE_LIMIT", status=429))
    llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())

    result = await llm.generate("hi")

    assert "Fallback mode" in result
    assert llm.stats["fallback_calls"] == 1
    assert llm.stats["primary_calls"] == 1


async def test_typed_timeout_and_unavailable_fall_back() -> None:
    for kind in ("TIMEOUT", "UNAVAILABLE"):
        primary = _StubProvider(raises=LLMError("transient", kind=kind))
        llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())
        result = await llm.generate("hi")
        assert "Fallback mode" in result, kind


async def test_typed_non_retryable_error_propagates() -> None:
    """Invalid response is a real failure — fail closed, do not silently degrade."""
    primary = _StubProvider(raises=LLMError("malformed schema", kind="INVALID_RESPONSE"))
    llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())

    with pytest.raises(LLMError):
        await llm.generate("hi")
    assert llm.stats["fallback_calls"] == 0


async def test_untyped_exception_propagates_regardless_of_text() -> None:
    """LAW 10: an exception whose *text* contains '429' is not necessarily a
    provider rate-limit. Only typed errors trigger fallback."""
    primary = _StubProvider(raises=ValueError("unexpected 429 in an unrelated traceback"))
    llm = FallbackProvider(primary=primary, fallback=FakeLLMProvider())

    with pytest.raises(ValueError):
        await llm.generate("hi")
    assert llm.stats["fallback_calls"] == 0
