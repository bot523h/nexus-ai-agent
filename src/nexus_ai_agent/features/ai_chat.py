"""Google Gemini 2.0 Flash AI integration — free-tier chat, vision, code, translate, summarize."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

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


class GeminiEngine:
    """Google Gemini 2.0 Flash API client with rate limiting and conversation memory."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        max_rpm: int = 15,
        max_daily: int = 1500,
        max_history: int = 20,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._limiter = _RateLimiter(max_rpm=max_rpm, max_daily=max_daily)
        self._max_history = max_history
        # conversation_id -> list of {role, parts}
        self._history: dict[str, list[dict[str, Any]]] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_history(self, conv_id: str) -> list[dict[str, Any]]:
        if conv_id not in self._history:
            self._history[conv_id] = []
        return self._history[conv_id]

    def clear_history(self, conv_id: str) -> None:
        self._history.pop(conv_id, None)

    async def _call_gemini(
        self,
        contents: list[dict[str, Any]],
        *,
        system_instruction: str | None = None,
    ) -> str:
        """Make a request to the Gemini API."""
        url = f"{self.BASE_URL}/models/{self._model}:generateContent?key={self._api_key}"
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }
        payload["generationConfig"] = {
            "temperature": 0.9,
            "topP": 0.95,
            "topK": 40,
            "maxOutputTokens": 4096,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                error_text = resp.text[:500]
                log.error("gemini_api_error", status=resp.status_code, body=error_text)
                return f"❌ خطای API ({resp.status_code}): لطفاً بعداً تلاش کنید."
            data = resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                log.error("gemini_unexpected_response", data=str(data)[:500])
                return "❌ پاسخ نامعتبر از API."

    async def chat(
        self,
        text: str,
        *,
        conv_id: str,
        user_id: int,
        mode: str = "chat",
    ) -> str:
        """Send a chat message and get AI response."""
        if not self._limiter.is_allowed(user_id):
            rem = self._limiter.remaining(user_id)
            return (
                f"⏳ محدودیت درخواست.\n"
                f"باقیمانده دقیقه‌ای: {rem['rpm_remaining']}\n"
                f"باقیمانده روزانه: {rem['daily_remaining']}"
            )
        history = self._get_history(conv_id)
        # Add user message
        user_part: dict[str, Any] = {"role": "user", "parts": [{"text": text}]}
        history.append(user_part)
        # Trim history
        while len(history) > self._max_history:
            history.pop(0)
        system_prompt = _SYSTEM_PROMPTS.get(mode, _SYSTEM_PROMPTS["chat"])
        response = await self._call_gemini(history, system_instruction=system_prompt)
        # Save assistant response
        history.append({"role": "model", "parts": [{"text": response}]})
        self._limiter.record(user_id)
        return response

    async def ask(self, text: str, *, user_id: int) -> str:
        """One-shot question — no conversation memory."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست. لطفاً کمی صبر کنید."
        contents = [{"role": "user", "parts": [{"text": text}]}]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["chat"])
        self._limiter.record(user_id)
        return response

    async def translate(self, text: str, *, target_lang: str, user_id: int) -> str:
        """Translate text to target language."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Translate the following text to {target_lang}:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["translate"])
        self._limiter.record(user_id)
        return response

    async def summarize(self, text: str, *, user_id: int) -> str:
        """Summarize text."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Summarize the following text:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["summarize"])
        self._limiter.record(user_id)
        return response

    async def code(self, prompt: str, *, user_id: int) -> str:
        """Generate code from prompt."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(contents, system_instruction=_SYSTEM_PROMPTS["code"])
        self._limiter.record(user_id)
        return response

    async def vision(
        self,
        image_bytes: bytes,
        *,
        question: str = "Describe this image in detail.",
        user_id: int = 0,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Analyze an image with Gemini Vision."""
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
        self._limiter.record(user_id)
        return response

    def get_status(self) -> str:
        """Get engine status info."""
        return (
            f"🤖 Gemini AI Engine\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📋 مدل: {self._model}\n"
            f"🔑 API: {'✅ متصل' if self.is_configured else '❌ تنظیم نشده'}\n"
            f"📊 محدودیت: {self._limiter._max_rpm} RPM / {self._limiter._max_daily} روزانه\n"
            f"💬 مکالمات فعال: {len(self._history)}"
        )
