"""Gate B — single free-text path through CognitionPort (prose only).

Does not touch CommandBus. Model output is never execution authority.
User-facing errors are redacted (no raw provider payloads).
"""

from __future__ import annotations

from typing import Any

# Safe user-facing messages by CognitionError.code
_SAFE_USER: dict[str, str] = {
    "provider_unavailable": "AI is temporarily unavailable. Please try again later.",
    "policy_denied": "This request cannot be processed under current policy.",
    "provider_error": "AI service error. Please try again later.",
    "timeout": "AI request timed out. Please try a shorter message.",
    "cancelled": "AI request was cancelled.",
}


def build_cognition_ai_handler(cognition: Any) -> Any:
    """Return an async handler for /ai that uses CognitionPort when present."""

    async def ai_cmd(update: Any, context: Any) -> None:
        args = getattr(context, "args", None) or []
        text = " ".join(args).strip()
        msg = getattr(update, "effective_message", None)
        if not text:
            if msg is not None:
                await msg.reply_text("❌ Usage: /ai <your message>")
            return

        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        chat_id = int(chat.id) if chat is not None else 0
        user_id = int(user.id) if user is not None else 0
        message_id = int(getattr(msg, "message_id", 0) or 0)
        correlation_id = f"tg:{chat_id}:{user_id}:{message_id}"

        cog = cognition
        if cog is None:
            app = getattr(context, "application", None)
            if app is not None:
                cog = getattr(app, "bot_data", {}).get("cognition")

        if cog is None:
            if msg is not None:
                await msg.reply_text("❌ Cognition not configured.")
            return

        from nexus_ai_agent.cognition.port import CognitionError, CognitionRequest, TaskClass

        try:
            from nexus_ai_agent.observability.logging import get_logger

            log = get_logger(__name__)
        except Exception:  # noqa: BLE001
            log = None

        try:
            proposal = await cog.propose(
                CognitionRequest(
                    prompt=text,
                    task_class=TaskClass.CHAT,
                    user_id=user_id,
                    correlation_id=correlation_id,
                )
            )
        except CognitionError as exc:
            if log is not None:
                log.warning(
                    "cognition_error",
                    code=exc.code,
                    correlation_id=exc.correlation_id or correlation_id,
                )
            safe = _SAFE_USER.get(exc.code, "AI request failed. Please try again later.")
            if msg is not None:
                await msg.reply_text(f"❌ {safe}")
            return
        except Exception:  # noqa: BLE001
            if log is not None:
                log.exception("cognition_unexpected", correlation_id=correlation_id)
            if msg is not None:
                await msg.reply_text("❌ AI request failed. Please try again later.")
            return

        if msg is not None:
            await msg.reply_text(f"🤖 {proposal.text}")

    return ai_cmd
