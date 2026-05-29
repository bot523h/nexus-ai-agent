"""Smart Summarizer — URL / text / file summarization via Gemini AI."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

# ── Default summarization prompts ──────────────────────────────────────

_SUMMARY_PROMPTS: dict[str, str] = {
    "brief": (
        "Summarize the following text in 2-3 concise sentences. "
        "Focus on the key points only."
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
    ) -> None:
        self._api_key = gemini_api_key
        self._model = model
        self._base_url = base_url
        self._http = httpx.AsyncClient(timeout=60.0)

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

        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2048,
            },
        }

        try:
            resp = await self._http.post(
                f"{self._base_url}/models/{self._model}:generateContent"
                f"?key={self._api_key}",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            summary = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
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
        except httpx.HTTPStatusError as exc:
            logger.error("summarize_http_error", status=exc.response.status_code)
            return SummaryResult(error=f"API error: {exc.response.status_code}")
        except Exception as exc:  # noqa: BLE001
            logger.error("summarize_error", error=str(exc))
            return SummaryResult(error=f"Error: {exc}")

    async def summarize_url(
        self,
        url: str,
        mode: str = "brief",
        language: str | None = None,
    ) -> SummaryResult:
        """Fetch content from a URL and summarize it."""
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
        return [
            {"id": k, "description": v.split(".")[0]}
            for k, v in _SUMMARY_PROMPTS.items()
        ]

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
