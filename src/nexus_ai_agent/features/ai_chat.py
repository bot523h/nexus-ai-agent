"""Google Gemini 2.0 Flash AI integration — free-tier chat, vision, code, translate, summarize.

v2.1 improvements:
  - Persistent conversation history via ConversationStore (survives restarts)
  - Smart request queue integration (GeminiRequestQueue) for fair 15 RPM sharing
  - Fallback response when rate-limited or API unavailable
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import httpx

from nexus_ai_agent.llm.provider import SYSTEM_PRINCIPAL_ID
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class _ProviderFailureText(str):
    """Marker for an error envelope produced by this client, never by the model.

    Provider failures are surfaced as user-visible strings for backward
    compatibility, but quota accounting must distinguish them from a successful
    model answer that happens to begin with the same glyph.  This ``str``
    subclass preserves equality/serialisation while carrying that distinction.
    """

    __slots__ = ()


def _provider_failure(message: str) -> str:
    return _ProviderFailureText(message)


def _is_principal(user_id: object) -> bool:
    """True only for a real positive user id or the explicit system principal.

    ``0``, booleans, strings, and negative Telegram group ids are ambiguous and
    must never be metered as a user identity.
    """
    return (
        isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and (user_id > 0 or user_id == SYSTEM_PRINCIPAL_ID)
    )


# System prompts for different modes
_SYSTEM_PROMPTS: dict[str, str] = {
    "chat": (
        "You are NEXUS AI, a helpful, friendly, and knowledgeable assistant inside a Telegram bot. "
        "Respond concisely (under 4000 chars). Use Markdown formatting when helpful. "
        "You support multiple languages — reply in the same language the user writes in."
    ),
    "code": (
        "You are NEXUS AI Code Assistant. Write clean, well-commented code. "
        "Always specify the language. Add a brief explanation after the code block. "
        "Keep responses under 4000 chars."
    ),
    "translate": (
        "You are a professional translator. Translate the given text to the target language. "
        "Only output the translated text, nothing else."
    ),
    "summarize": (
        "You are a summarization expert. Produce a concise, structured summary "
        "with bullet points for key facts. Keep under 2000 chars."
    ),
    "vision": (
        "You are NEXUS AI Vision. Analyze the provided image in detail. "
        "Describe what you see, answer questions about the image. Respond in the user's language."
    ),
}


class _RateLimiter:
    """Simple per-minute and per-day rate limiter for Gemini free tier."""

    def __init__(self, max_rpm: int = 15, max_daily: int = 1500) -> None:
        self._max_rpm = max_rpm
        self._max_daily = max_daily
        self._minute_buckets: dict[int, list[float]] = defaultdict(list)
        self._daily_counts: dict[int, int] = defaultdict(int)
        self._day: int = time.gmtime().tm_yday

    def _reset_day_if_needed(self) -> None:
        today = time.gmtime().tm_yday
        if today != self._day:
            self._daily_counts.clear()
            self._day = today

    def is_allowed(self, user_id: int) -> bool:
        self._reset_day_if_needed()
        now = time.monotonic()
        # Clean old minute entries
        bucket = self._minute_buckets[user_id]
        self._minute_buckets[user_id] = [t for t in bucket if now - t < 60]
        # Check limits
        if len(self._minute_buckets[user_id]) >= self._max_rpm:
            return False
        if self._daily_counts[user_id] >= self._max_daily:
            return False
        return True

    def record(self, user_id: int) -> None:
        now = time.monotonic()
        self._minute_buckets[user_id].append(now)
        self._daily_counts[user_id] += 1

    def remaining(self, user_id: int) -> dict[str, int]:
        self._reset_day_if_needed()
        now = time.monotonic()
        bucket = [t for t in self._minute_buckets[user_id] if now - t < 60]
        return {
            "rpm_remaining": max(0, self._max_rpm - len(bucket)),
            "daily_remaining": max(0, self._max_daily - self._daily_counts[user_id]),
        }


_ERROR_PREFIX = "❌"
_INVALID_IDENTITY_MSG = _provider_failure("❌ هویت کاربر نامعتبر است؛ این درخواست انجام نشد.")


class GeminiEngine:
    """Google Gemini 2.0 Flash API client with rate limiting and conversation memory.

    v2.1: Supports optional ConversationStore for persistent history
    and optional GeminiRequestQueue for fair request scheduling.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        max_rpm: int = 15,
        max_daily: int = 1500,
        max_history: int = 20,
        conversation_store: Any | None = None,
        request_queue: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._limiter = _RateLimiter(max_rpm=max_rpm, max_daily=max_daily)
        self._max_history = max_history
        # conversation_id -> list of {role, parts}  (in-memory fallback)
        self._history: dict[str, list[dict[str, Any]]] = {}
        # v2.1: persistent store (optional)
        self._store = conversation_store
        # v2.1: request queue (optional)
        self._queue = request_queue

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    @property
    def queue(self) -> Any:
        """Access the request queue (if configured)."""
        return self._queue

    def _get_history(self, conv_id: str) -> list[dict[str, Any]]:
        """Get conversation history — from persistent store if available, else in-memory."""
        if self._store is not None:
            return self._store.get_history(conv_id, limit=self._max_history)
        # Fallback: in-memory
        if conv_id not in self._history:
            self._history[conv_id] = []
        return self._history[conv_id]

    def _append_to_history(self, conv_id: str, message: dict[str, Any]) -> None:
        """Append a message to conversation history — persistent store if available."""
        if self._store is not None:
            self._store.append(conv_id, message)
            # Trim to limit
            self._store.trim_to_limit(conv_id, limit=self._max_history)
        else:
            # In-memory fallback
            if conv_id not in self._history:
                self._history[conv_id] = []
            self._history[conv_id].append(message)
            while len(self._history[conv_id]) > self._max_history:
                self._history[conv_id].pop(0)

    def clear_history(self, conv_id: str) -> None:
        """Clear conversation history for a given conv_id."""
        if self._store is not None:
            self._store.clear(conv_id)
        self._history.pop(conv_id, None)

    async def _call_gemini(
        self,
        contents: list[dict[str, Any]],
        *,
        system_instruction: str | None = None,
    ) -> str:
        """Make a request to the Gemini API."""
        # API key rides in the x-goog-api-key header, never in the URL:
        # query parameters end up in httpx INFO log lines, proxy logs and
        # exception reprs (the redaction layer is defence-in-depth, not
        # the primary control).
        url = f"{self.BASE_URL}/models/{self._model}:generateContent"
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        payload["generationConfig"] = {
            "temperature": 0.9,
            "topP": 0.95,
            "topK": 40,
            "maxOutputTokens": 4096,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers={"x-goog-api-key": self._api_key})
            if resp.status_code != 200:
                error_text = resp.text[:500]
                log.error("gemini_api_error", status=resp.status_code, body=error_text)
                return _provider_failure(
                    f"{_ERROR_PREFIX} خطای API ({resp.status_code}): لطفاً بعداً تلاش کنید."
                )
            data = resp.json()
            try:
                # A successful model answer remains a plain str even if its
                # generated text starts with the same glyph.
                return str(data["candidates"][0]["content"]["parts"][0]["text"])
            except (KeyError, IndexError, TypeError):
                log.error("gemini_unexpected_response", data=str(data)[:500])
                return _provider_failure(f"{_ERROR_PREFIX} پاسخ نامعتبر از API.")

    async def chat(
        self,
        text: str,
        *,
        conv_id: str,
        user_id: int,
        mode: str = "chat",
    ) -> str:
        """Send a chat message and get AI response."""
        if not _is_principal(user_id):
            return _INVALID_IDENTITY_MSG
        if not self._limiter.is_allowed(user_id):
            rem = self._limiter.remaining(user_id)
            return (
                f"⏳ محدودیت درخواست.\n"
                f"باقیمانده دقیقه‌ای: {rem['rpm_remaining']}\n"
                f"باقیمانده روزانه: {rem['daily_remaining']}"
            )

        # If a request queue is configured, use it for fair scheduling
        if self._queue is not None:
            from nexus_ai_agent.features.request_queue import Priority

            result = await self._queue.submit(
                lambda: self._do_chat(text, conv_id=conv_id, user_id=user_id, mode=mode),
                user_id=user_id,
                priority=Priority.NORMAL,
            )
            return result

        return await self._do_chat(text, conv_id=conv_id, user_id=user_id, mode=mode)

    async def _do_chat(
        self,
        text: str,
        *,
        conv_id: str,
        user_id: int,
        mode: str = "chat",
    ) -> str:
        """Internal: perform the actual chat call."""
        history = list(self._get_history(conv_id))
        # Add user message
        user_part: dict[str, Any] = {"role": "user", "parts": [{"text": text}]}
        history.append(user_part)
        system_prompt = _SYSTEM_PROMPTS.get(mode, _SYSTEM_PROMPTS["chat"])
        response = await self._call_gemini(history, system_instruction=system_prompt)
        # Save to history
        self._append_to_history(conv_id, user_part)
        assistant_part = {"role": "model", "parts": [{"text": response}]}
        self._append_to_history(conv_id, assistant_part)
        self._record_success(user_id, response)
        return response

    async def ask(self, text: str, *, user_id: int) -> str:
        """One-shot question — no conversation memory."""
        if not _is_principal(user_id):
            raise ValueError(
                "invalid identity; pass a real positive user id "
                "(or SYSTEM_PRINCIPAL_ID for system work)"
            )
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست. لطفاً کمی صبر کنید."

        if self._queue is not None:
            from nexus_ai_agent.features.request_queue import Priority

            return await self._queue.submit(
                lambda: self._do_one_shot(text, system=_SYSTEM_PROMPTS["chat"], user_id=user_id),
                user_id=user_id,
                priority=Priority.NORMAL,
            )

        return await self._do_one_shot(text, system=_SYSTEM_PROMPTS["chat"], user_id=user_id)

    async def _do_one_shot(self, text: str, *, system: str, user_id: int) -> str:
        """Internal: one-shot Gemini call; records quota on success (once).

        Mirrors ``_do_chat``: the record lives inside the internal call so the
        direct and queued paths share one accounting point — a provider error
        raises before ``record()`` and is never counted, and a request blocked
        by ``is_allowed`` never reaches this method at all.
        """
        contents = [{"role": "user", "parts": [{"text": text}]}]
        response = await self._call_gemini(contents, system_instruction=system)
        self._record_success(user_id, response)
        return response

    async def translate(self, text: str, *, target_lang: str, user_id: int) -> str:
        """Translate text to target language."""
        if not _is_principal(user_id):
            return _INVALID_IDENTITY_MSG
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Translate the following text to {target_lang}:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["translate"]
        )
        self._record_success(user_id, response)
        return response

    async def summarize(self, text: str, *, user_id: int) -> str:
        """Summarize text."""
        if not _is_principal(user_id):
            return _INVALID_IDENTITY_MSG
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Summarize the following text:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["summarize"]
        )
        self._record_success(user_id, response)
        return response

    async def code(self, prompt: str, *, user_id: int) -> str:
        """Generate code from prompt."""
        if not _is_principal(user_id):
            return _INVALID_IDENTITY_MSG
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["code"])
        self._record_success(user_id, response)
        return response

    async def vision(
        self,
        image_bytes: bytes,
        *,
        user_id: int,
        question: str = "Describe this image in detail.",
        mime_type: str = "image/jpeg",
    ) -> str:
        """Analyze an image with Gemini Vision."""
        if not _is_principal(user_id):
            return _INVALID_IDENTITY_MSG
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        import base64

        b64 = base64.b64encode(image_bytes).decode()
        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": question},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64,
                        }
                    },
                ],
            }
        ]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["vision"])
        self._record_success(user_id, response)
        return response

    def _record_success(self, user_id: int, response: str) -> None:
        """Book quota for a successful user request — exactly once per call.

        Local provider failures are marked with :class:`_ProviderFailureText`;
        they are never booked.  A successful model answer is a plain string and
        is always booked, even if the model chose to begin its text with "❌" —
        quota enforcement must not depend on attacker-controlled content.
        Blocked requests never reach a recording site, and a raised provider
        exception propagates before it.  Direct and queued paths share this one
        accounting point, so there is no double count.
        """
        if isinstance(response, _ProviderFailureText):
            return
        self._limiter.record(user_id)

    def get_status(self) -> str:
        """Get engine status info."""
        active_convos = (
            self._store.active_conversations() if self._store is not None else len(self._history)
        )
        queue_info = ""
        if self._queue is not None:
            qs = self._queue.get_status()
            queue_info = f"\n📋 صف درخواست: {qs['queue_size']} در انتظار"
        return (
            f"🤖 Gemini AI Engine\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 مدل: {self._model}\n"
            f"🔑 API: {'✅ متصل' if self.is_configured else '❌ تنظیم نشده'}\n"
            f"📊 محدودیت: {self._limiter._max_rpm} RPM / {self._limiter._max_daily} روزانه\n"
            f"💬 مکالمات فعال: {active_convos}"
            + ("\n💾 ذخیره‌سازی: دائمی (SQLite)" if self._store else "\n💾 ذخیره‌سازی: حافظه موقت")
            + f"{queue_info}"
        )
