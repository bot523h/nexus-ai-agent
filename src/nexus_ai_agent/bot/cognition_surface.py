"""Gate B — single free-text path through CognitionPort (prose only).

Does not touch CommandBus. Model output is never execution authority.
"""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ContextTypes


def build_cognition_ai_handler(cognition: Any) -> Any:
    """Return an async handler for /ai that uses CognitionPort when present."""

    async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        text = " ".join(args).strip()
        msg = update.effective_message
        if not text:
            if msg:
                await msg.reply_text("❌ Usage: /ai <your message>")
            return

        chat_id = int(update.effective_chat.id) if update.effective_chat else 0
        user_id = int(update.effective_user.id) if update.effective_user else 0
        correlation_id = f"tg:{chat_id}:{user_id}"

        cog = cognition
        if cog is None and context.application is not None:
            cog = context.application.bot_data.get("cognition")

        if cog is None:
            if msg:
                await msg.reply_text("❌ Cognition not configured.")
            return

        from nexus_ai_agent.cognition.port import CognitionError, CognitionRequest, TaskClass

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
            if msg:
                await msg.reply_text(f"❌ Cognition unavailable ({exc.code}): {exc}")
            return

        if msg:
            await msg.reply_text(f"🤖 {proposal.text}")

    return ai_cmd
