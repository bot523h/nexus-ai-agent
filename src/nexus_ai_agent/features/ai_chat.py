"""Google Gemini 2.0 Flash AI integration — free-tier chat, vision, code, translate, summarize.

v2.1 improvements:
  - Persistent conversation history via ConversationStore (survives restarts)
  - Fair per-user request throttling
  - Localised response when the API is rate-limited or unavailable

W2 (Global LLM Gateway): this engine no longer owns an HTTP client, a retry
loop, a provider quota counter or an admission queue. Every call goes through
:class:`~nexus_ai_agent.llm.gateway.engine.LLMGateway`, which is the single LLM
authority for the process:

* transport, timeouts and connection pooling → ``GeminiHttpAdapter``
* retry classification (typed, never substring-based) → ``llm/errors.py``
* provider RPM/RPD quota, concurrency bounds, circuit breaking → gateway policy
* request ids, timings, attempts, usage → gateway observability

What stays here, deliberately:

* ``_RateLimiter`` — a **per-user** fairness gate. The gateway's rate policy is
  **per-provider**. Different axes, both needed: one user must not consume the
  whole free-tier minute, and the process must not exceed the provider's minute.
* the localised Persian strings — rendering a failure for a human is a surface
  concern. The gateway classifies it (typed ``LLMError``); this module only
  renders it, and it renders from ``kind``/``status_code``, never by scanning a
  message for a substring.
* ``request_queue`` — still accepted and still reported by :meth:`get_status`
  for compatibility, but it is **no longer in the execution path**. Submitting
  through it would nest a second admission gate and a second retry loop around
  the gateway's own (retries multiply, quota is charged twice), which is exactly
  the two-authorities split this wave removes. See
  ``docs/architecture/LLM_GATEWAY.md`` §"Relationship to GeminiRequestQueue".
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from nexus_ai_agent.llm.errors import InvalidRequestError, LLMError, LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    ContentPart,
    GenerationParams,
    LLMOperation,
    LLMRequest,
    Message,
)
from nexus_ai_agent.llm.gateway.registry import gateway_for_credentials
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


#: Wire-identical to the pre-W2 ``generationConfig`` this engine always sent, so
#: migrating onto the gateway does not change a single sampling knob.
_CHAT_GENERATION = GenerationParams(
    temperature=0.9,
    top_p=0.95,
    top_k=40,
    max_output_tokens=4096,
)


def _contents_to_messages(contents: list[dict[str, Any]]) -> tuple[Message, ...]:
    """Convert Gemini wire ``contents`` into gateway :class:`Message` turns.

    Lossless: a turn's text parts become ``Message.content`` and any non-text
    part (``inline_data``) becomes a :class:`ContentPart` on that same turn, so
    a vision turn keeps its image instead of being flattened to text.
    """

    messages: list[Message] = []
    for entry in contents:
        role = str(entry.get("role") or "user")
        if role not in {"system", "user", "model", "assistant", "tool"}:
            role = "user"
        text_chunks: list[str] = []
        parts: list[ContentPart] = []
        for part in entry.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_chunks.append(part["text"])
                continue
            inline = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                import base64

                parts.append(
                    ContentPart(
                        mime_type=str(
                            inline.get("mime_type") or inline.get("mimeType") or "image/jpeg"
                        ),
                        data=base64.b64decode(inline["data"]),
                    )
                )
        messages.append(Message(role=role, content="".join(text_chunks), parts=tuple(parts)))
    return tuple(messages)


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
        gateway: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._limiter = _RateLimiter(max_rpm=max_rpm, max_daily=max_daily)
        self._max_history = max_history
        # conversation_id -> list of {role, parts}  (in-memory fallback)
        self._history: dict[str, list[dict[str, Any]]] = {}
        # v2.1: persistent store (optional)
        self._store = conversation_store
        # v2.1: request queue — retained for compatibility and status reporting,
        # but no longer in the execution path (see module docstring).
        self._queue = request_queue
        # W2: the single LLM authority. Injected in tests / by a composition
        # root; otherwise resolved from the credentials this engine was given.
        self._gateway = (
            gateway
            if gateway is not None
            else gateway_for_credentials(
                api_key,
                model,
                requests_per_minute=max_rpm,
                requests_per_day=max_daily,
                base_url=self.BASE_URL,
            )
        )

    @property
    def gateway(self) -> Any:
        """The authority every call from this engine goes through (W2, LAW 1)."""

        return self._gateway

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
        purpose: str = "chat",
    ) -> str:
        """Send *contents* to Gemini **through the gateway** and return text.

        Signature and return contract are unchanged from pre-W2 (a string, never
        an exception, for provider-side failures) so every caller — and every
        pinned transport test — keeps working. What changed is *who decides*:

        * the API key rides in the ``x-goog-api-key`` header (unchanged; now
          enforced once, in ``GeminiHttpAdapter``, instead of at each call site)
        * a non-200 is classified by **status code**, not by scanning the body
        * retry, backoff, quota, concurrency, circuit breaking and observability
          are the gateway's, not this method's

        Failure rendering is the last step and is driven by the typed error's
        ``kind``/``status_code`` only.
        """

        if not contents:
            # An empty turn list is a caller bug, not a provider condition.
            # Typed and loud beats a 400 from Google or a silent empty answer.
            raise InvalidRequestError(
                "GeminiEngine._call_gemini needs at least one content turn",
                provider="gemini",
                model=self._model,
                detail="empty_contents",
            )
        request = LLMRequest(
            caller=Caller(category=CallerCategory.SURFACE, name="features.ai_chat"),
            purpose=purpose,
            operation=LLMOperation.CHAT,
            messages=_contents_to_messages(contents),
            system=system_instruction,
            provider="gemini",
            model=self._model,
            # Pinned hard: the bot's chat surface must never silently receive a
            # different provider's (or a locally faked) answer. On exhaustion the
            # caller gets the typed failure and renders it (LAW 8).
            allow_fallback=False,
            generation=_CHAT_GENERATION,
        )
        try:
            response = await self._gateway.execute(request)
        except LLMError as exc:
            return self._render_llm_error(exc)
        return response.text

    @staticmethod
    def _render_llm_error(exc: LLMError) -> str:
        """Render an already-classified failure into the user's language.

        This is presentation, not detection: the ``kind`` and ``status_code``
        were decided by the gateway from typed evidence. Nothing here inspects
        message text.
        """

        kind = exc.kind
        if (
            kind is LLMErrorKind.MALFORMED_RESPONSE
            or kind is LLMErrorKind.STRUCTURED_OUTPUT_INVALID
        ):
            log.error(
                "gemini_unexpected_response", error_kind=kind.value, request_id=exc.request_id
            )
            return "❌ پاسخ نامعتبر از API."
        if kind is LLMErrorKind.CONTENT_BLOCKED:
            # Pre-W2 a safety block surfaced as "invalid response" because the
            # 200 carried no candidates. Naming it is more truthful and lets the
            # user fix the prompt instead of retrying forever.
            log.warning("gemini_content_blocked", detail=exc.detail, request_id=exc.request_id)
            return "❌ پاسخ به دلیل محدودیت محتوایی مسدود شد."
        if kind is LLMErrorKind.CANCELLED:
            return "❌ درخواست لغو شد."
        status = exc.status_code
        log.error(
            "gemini_api_error",
            status=status,
            error_kind=kind.value,
            detail=exc.detail,
            request_id=exc.request_id,
        )
        if status is not None:
            # Byte-identical to the pre-W2 rendering for HTTP failures.
            return f"❌ خطای API ({status}): لطفاً بعداً تلاش کنید."
        return "❌ خطای سرویس: لطفاً بعداً تلاش کنید."

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

        # W2: admission, provider quota, retry and cancellation are the
        # gateway's. Submitting through ``self._queue`` on top of that would
        # nest a second admission gate and a second retry loop around the first
        # (retries multiply, quota is charged twice) — two authorities for one
        # call. The per-user fairness check above stays: it is a different axis.
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
        response = await self._call_gemini(history, system_instruction=system_prompt, purpose=mode)
        # Save to history
        self._append_to_history(conv_id, user_part)
        assistant_part = {"role": "model", "parts": [{"text": response}]}
        self._append_to_history(conv_id, assistant_part)
        self._limiter.record(user_id)
        return response

    async def ask(self, text: str, *, user_id: int) -> str:
        """One-shot question — no conversation memory."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست. لطفاً کمی صبر کنید."

        return await self._do_one_shot(text, system=_SYSTEM_PROMPTS["chat"])

    async def _do_one_shot(self, text: str, *, system: str) -> str:
        """Internal: one-shot Gemini call."""
        contents = [{"role": "user", "parts": [{"text": text}]}]
        return await self._call_gemini(contents, system_instruction=system, purpose="one_shot")

    async def translate(self, text: str, *, target_lang: str, user_id: int) -> str:
        """Translate text to target language."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Translate the following text to {target_lang}:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["translate"], purpose="translate"
        )
        self._limiter.record(user_id)
        return response

    async def summarize(self, text: str, *, user_id: int) -> str:
        """Summarize text."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        prompt = f"Summarize the following text:\n\n{text}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["summarize"], purpose="summarize"
        )
        self._limiter.record(user_id)
        return response

    async def code(self, prompt: str, *, user_id: int) -> str:
        """Generate code from prompt."""
        if not self._limiter.is_allowed(user_id):
            return "⏳ محدودیت درخواست."
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["code"], purpose="code"
        )
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
        response = await self._call_gemini(
            contents, system_instruction=_SYSTEM_PROMPTS["vision"], purpose="vision"
        )
        self._limiter.record(user_id)
        return response

    def get_status(self) -> str:
        """Get engine status info."""
        active_convos = (
            self._store.active_conversations() if self._store is not None else len(self._history)
        )
        queue_info = ""
        if self._queue is not None:
            qs = self._queue.get_status()
            queue_info = f"\n📋 صف درخواست: {qs['queue_size']} در انتظار"
        gateway_info = ""
        try:
            status = self._gateway.status()
            scheduler = status.get("scheduler") or {}
            gateway_info = (
                f"\n🌐 دروازه LLM: {status.get('executed', 0)} درخواست / "
                f"{scheduler.get('inflight_global', 0)} در حال اجرا"
            )
        except Exception:  # noqa: BLE001 — status rendering must never break /status
            gateway_info = ""
        return (
            f"🤖 Gemini AI Engine\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 مدل: {self._model}\n"
            f"🔑 API: {'✅ متصل' if self.is_configured else '❌ تنظیم نشده'}\n"
            f"📊 محدودیت: {self._limiter._max_rpm} RPM / {self._limiter._max_daily} روزانه\n"
            f"💬 مکالمات فعال: {active_convos}"
            + ("\n💾 ذخیره‌سازی: دائمی (SQLite)" if self._store else "\n💾 ذخیره‌سازی: حافظه موقت")
            + f"{queue_info}"
            + f"{gateway_info}"
        )
