"""Smart Summarizer — URL / text / file summarization via Gemini AI.

W2 (Global LLM Gateway): the Gemini call in :meth:`SummarizerEngine.summarize_text`
now goes through :class:`~nexus_ai_agent.llm.gateway.engine.LLMGateway` instead of
a private ``httpx`` POST. Retry, timeouts, provider quota, circuit breaking,
typed error classification and request-level observability are the gateway's.

``self._http`` is deliberately **kept**: it is the SSRF-guarded client used to
fetch a *user-supplied* URL in :meth:`summarize_url`, which is a different
concern from calling an LLM. ``tests/unit/test_summarizer_ssrf.py`` pins
``engine._http._transport is SafeAsyncTransport`` and that stays true.

The Gemini endpoint itself is a build-time constant with no user-controlled host,
so SSRF is structurally impossible on that leg; the guard stays exactly where
untrusted input enters.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport, SSRFBlockError, validate_url
from nexus_ai_agent.llm.errors import LLMError
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    GenerationParams,
    LLMOperation,
    LLMRequest,
    Message,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.registry import gateway_for_credentials
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: Wire-identical to the pre-W2 ``generationConfig`` for summaries.
_SUMMARY_GENERATION = GenerationParams(temperature=0.3, max_output_tokens=2048)

# ── Default summarization prompts ──────────────────────────────────────

_SUMMARY_PROMPTS: dict[str, str] = {
    "brief": (
        "Summarize the following text in 2-3 concise sentences. Focus on the key points only."
    ),
    "detailed": (
        "Provide a detailed summary of the following text. "
        "Cover all main arguments, evidence, and conclusions. "
        "Use bullet points for clarity."
    ),
    "key_points": (
        "Extract the key points from the following text as a numbered list. "
        "Each point should be one sentence."
    ),
    "eli5": (
        "Explain the following text in simple terms, as if explaining to a 5-year-old. "
        "Use simple words and analogies."
    ),
    "academic": (
        "Write an academic-style abstract for the following text. "
        "Include: background, method, findings, and conclusion."
    ),
}


@dataclass
class SummaryResult:
    """Result of a summarization request."""

    text: str = ""
    mode: str = "brief"
    original_length: int = 0
    summary_length: int = 0
    compression_ratio: float = 0.0
    error: str | None = None


class SummarizerEngine:
    """Smart content summarizer powered by Gemini AI.

    Supports:
    - Direct text summarization
    - URL content fetching + summarization
    - Multiple summary modes (brief, detailed, key_points, eli5, academic)
    """

    def __init__(
        self,
        gemini_api_key: str,
        model: str = "gemini-2.0-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        gateway: LLMGateway | None = None,
    ) -> None:
        self._api_key = gemini_api_key
        self._model = model
        self._base_url = base_url
        # SSRF-guarded client for user-supplied URLs (see module docstring).
        self._http = httpx.AsyncClient(timeout=60.0, transport=SafeAsyncTransport())
        # W2: the single LLM authority for the Gemini leg.
        self._gateway = (
            gateway
            if gateway is not None
            else gateway_for_credentials(gemini_api_key, model, base_url=base_url)
        )

    async def summarize_text(
        self,
        text: str,
        mode: str = "brief",
        language: str | None = None,
    ) -> SummaryResult:
        """Summarize the given text using the specified mode."""
        if not text.strip():
            return SummaryResult(error="No text provided to summarize.")

        prompt_template = _SUMMARY_PROMPTS.get(mode, _SUMMARY_PROMPTS["brief"])
        system_instruction = prompt_template

        if language:
            system_instruction += f" Write the summary in {language}."

        request = LLMRequest(
            caller=Caller(category=CallerCategory.SUMMARIZER, name="features.summarizer"),
            purpose=f"summarize:{mode}",
            operation=LLMOperation.CHAT,
            messages=(Message(role="user", content=text),),
            system=system_instruction,
            provider="gemini",
            model=self._model,
            # A summary must come from the real model. Silently substituting
            # another provider's answer would be a fabricated summary (LAW 8).
            allow_fallback=False,
            generation=_SUMMARY_GENERATION,
        )

        try:
            response = await self._gateway.execute(request)
        except LLMError as exc:
            # Classification already happened, from typed evidence: the status
            # code and the semantic kind. Nothing here parses a message.
            if exc.status_code is not None:
                logger.error(
                    "summarize_http_error",
                    status=exc.status_code,
                    error_kind=exc.kind.value,
                    request_id=exc.request_id,
                )
                return SummaryResult(error=f"API error: {exc.status_code}")
            logger.error(
                "summarize_error",
                error_kind=exc.kind.value,
                detail=exc.detail,
                request_id=exc.request_id,
            )
            return SummaryResult(error=f"Error: {exc.kind.value}")
        except Exception as exc:  # noqa: BLE001 — a summarizer never crashes its caller
            logger.error("summarize_error", error=type(exc).__name__)
            return SummaryResult(error=f"Error: {type(exc).__name__}")

        summary = response.text
        if not summary:
            return SummaryResult(error="Empty response from AI.")

        orig_len = len(text)
        summ_len = len(summary)
        return SummaryResult(
            text=summary,
            mode=mode,
            original_length=orig_len,
            summary_length=summ_len,
            compression_ratio=round(1 - (summ_len / max(orig_len, 1)), 2),
        )

    async def summarize_url(
        self,
        url: str,
        mode: str = "brief",
        language: str | None = None,
    ) -> SummaryResult:
        """Fetch content from a URL and summarize it.

        Security (SSRF protection): only ``https`` URLs whose resolved
        address is public may be fetched — checked before the fetch and
        again at connect time for every connection, including redirects
        (see ``core/ssrf_guard.py``).
        """
        try:
            await asyncio.to_thread(validate_url, url)
        except SSRFBlockError as exc:
            logger.warning("summarize_url_blocked", url=url, error=str(exc))
            return SummaryResult(error=str(exc))

        try:
            resp = await self._http.get(url, follow_redirects=True, timeout=30.0)
            resp.raise_for_status()
            content = resp.text
        except Exception as exc:  # noqa: BLE001
            return SummaryResult(error=f"Failed to fetch URL: {exc}")

        # Clean HTML tags if it's an HTML page
        if "<html" in content.lower() or "<body" in content.lower():
            content = _strip_html(content)

        if not content.strip():
            return SummaryResult(error="No content found at the URL.")

        # Truncate very long content to avoid API limits
        if len(content) > 30000:
            content = content[:30000] + "\n\n[... content truncated ...]"

        return await self.summarize_text(content, mode=mode, language=language)

    @staticmethod
    def get_modes() -> list[dict[str, str]]:
        """Return available summary modes."""
        return [{"id": k, "description": v.split(".")[0]} for k, v in _SUMMARY_PROMPTS.items()]

    @staticmethod
    def format_result(result: SummaryResult) -> str:
        """Format a summary result for display."""
        if result.error:
            return f"❌ Error: {result.error}"

        lines = [
            f"📝 Summary ({result.mode})",
            "━" * 20,
            result.text,
            "",
            f"📊 Original: {result.original_length} chars → "
            f"Summary: {result.summary_length} chars "
            f"({result.compression_ratio:.0%} compression)",
        ]
        return "\n".join(lines)

    async def close(self) -> None:
        """Release the SSRF-guarded fetch client.

        The gateway is *not* closed here: it is a process-wide authority shared
        with every other caller, and one feature closing it would break the
        others. Its lifecycle belongs to the composition root (``aclose()``).
        """

        await self._http.aclose()


def _strip_html(html: str) -> str:
    """Crude HTML → plain-text conversion."""
    # Remove script and style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Decode common entities
    for ent, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        text = text.replace(ent, char)
    return text
