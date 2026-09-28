"""Base class for the specialized Store agents.

W2 (Global LLM Gateway): this is a *surface*, so it is the layer that turns a
typed :class:`~nexus_ai_agent.llm.errors.LLMError` into words a Telegram user can
read. ``bot/handlers.py`` awaits :meth:`StoreAgent.respond` without a ``try``,
which means this method must never let a provider failure escape as an exception
— but it also must not invent an answer. The compromise is explicit: render the
failure, name its kind in the log, and say so in the reply.

Before W2 the failure arrived already rendered as a Persian error string from
``GeminiEngine.ask`` and this class forwarded it blind — which is why nothing
downstream could tell an answer from an error. Now the provider raises typed, the
surface renders, and the two are finally distinguishable.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.errors import LLMError, LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import Message
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

#: One user-facing rendering per failure class. Keyed by the typed kind, never by
#: a substring of a provider message.
_FAILURE_MESSAGES: dict[LLMErrorKind, str] = {
    LLMErrorKind.RATE_LIMITED: "⏳ سرویس هوش مصنوعی شلوغ است. لطفاً کمی بعد دوباره تلاش کنید.",
    LLMErrorKind.QUOTA_EXHAUSTED: (
        "⏳ سهمیه روزانه سرویس هوش مصنوعی پر شده است. فردا دوباره تلاش کنید."
    ),
    LLMErrorKind.UPSTREAM_TIMEOUT: "⌛ پاسخ سرویس هوش مصنوعی به‌موقع نرسید. لطفاً دوباره تلاش کنید.",
    LLMErrorKind.NETWORK: "🌐 ارتباط با سرویس هوش مصنوعی برقرار نشد. لطفاً دوباره تلاش کنید.",
    LLMErrorKind.TRANSIENT_PROVIDER: (
        "⚠️ سرویس هوش مصنوعی موقتاً در دسترس نیست. لطفاً دوباره تلاش کنید."
    ),
    LLMErrorKind.AUTHENTICATION: (
        "🔑 پیکربندی سرویس هوش مصنوعی نامعتبر است. با مالک ربات تماس بگیرید."
    ),
    LLMErrorKind.CONTENT_BLOCKED: "🚫 پاسخ به دلیل محدودیت محتوایی مسدود شد.",
    LLMErrorKind.CONTEXT_LIMIT: "📏 پیام برای این مدل خیلی طولانی است. لطفاً کوتاه‌تر بفرستید.",
    LLMErrorKind.CANCELLED: "❌ درخواست لغو شد.",
    LLMErrorKind.DEADLINE_EXCEEDED: "⌛ پردازش درخواست بیش از حد طول کشید. لطفاً دوباره تلاش کنید.",
    LLMErrorKind.OVERLOADED: "⏳ ربات در حال حاضر ظرفیت خالی ندارد. لطفاً کمی بعد تلاش کنید.",
}

_DEFAULT_FAILURE_MESSAGE = "⚠️ پردازش درخواست ممکن نشد. لطفاً دوباره تلاش کنید."


class StoreAgent:
    """Base class for all specialized agents in the Store."""

    name: str
    emoji: str
    description: str
    system_prompt: str
    category: str

    def __init__(self, gemini_provider: GeminiProvider | None = None) -> None:
        settings = get_settings()
        self.gemini = gemini_provider or GeminiProvider(api_key=settings.gemini_api_key or "")

    async def respond(
        self, user_id: int, message: str, history: list[dict[str, str]], context: str = ""
    ) -> str:
        """Generate a response using the agent's unique personality.

        *history* is forwarded as real conversation turns (``{"role", "content"}``
        pairs) instead of being dropped, and *context* stays part of the system
        instruction where the model can weigh it.
        """

        full_system_prompt = self.system_prompt
        if context:
            full_system_prompt += f"\n\nContext about user:\n{context}"

        turns = _to_messages(history)
        # An injected double (tests, a different provider) may not expose a
        # gateway; then the plain LLMProvider contract is used. Either way the
        # call ends up inside *an* authority — never on a raw HTTP client.
        gateway = getattr(self.gemini, "gateway", None)
        try:
            if turns and gateway is not None:
                # The typed path carries history without flattening it into the
                # prompt string; the LLMProvider contract has no history slot.
                response = await gateway.execute(
                    _history_request(self, message, turns, full_system_prompt)
                )
                return response.text
            return await self.gemini.generate(prompt=message, system=full_system_prompt)
        except LLMError as exc:
            log.warning(
                "store_agent_llm_failure",
                agent=self.name,
                user_id=user_id,
                error_kind=exc.kind.value,
                status=exc.status_code,
                provider=exc.provider,
                request_id=exc.request_id,
            )
            return _FAILURE_MESSAGES.get(exc.kind, _DEFAULT_FAILURE_MESSAGE)
        except Exception:
            # A bug is not a provider condition. Log it with a type, not a
            # message dump, and tell the user plainly.
            log.exception("store_agent_unexpected_failure", agent=self.name, user_id=user_id)
            return _DEFAULT_FAILURE_MESSAGE


def _to_messages(history: Any) -> tuple[Message, ...]:
    """Convert ``[{"role", "content"}, …]`` into gateway turns, skipping junk."""

    if not history:
        return ()
    turns: list[Message] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "user")
        content = entry.get("content")
        if not isinstance(content, str) or not content:
            continue
        if role not in {"system", "user", "model", "assistant", "tool"}:
            role = "user"
        turns.append(Message(role=role, content=content))
    return tuple(turns)


def _history_request(
    agent: StoreAgent, prompt: str, turns: tuple[Message, ...], system: str
) -> Any:
    """Build the typed request for a history-carrying agent turn."""

    from nexus_ai_agent.llm.gateway.contract import (
        Caller,
        CallerCategory,
        LLMOperation,
        LLMRequest,
    )

    return LLMRequest(
        caller=Caller(category=CallerCategory.AGENT, name=f"agents.store.{agent.name}"),
        purpose=f"agent:{agent.name}",
        operation=LLMOperation.CHAT,
        messages=turns + (Message(role="user", content=prompt),),
        system=system,
        provider="gemini",
        allow_fallback=False,
    )
