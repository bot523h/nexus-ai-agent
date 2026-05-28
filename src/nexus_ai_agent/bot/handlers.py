from __future__ import annotations

import json
from typing import Any, Callable
from uuid import uuid4

import structlog
from sqlmodel import select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.storage.models import Chat, User

from .middleware import AuthMiddleware, RateLimiter

logger = get_logger(__name__)


async def _upsert_user(db_session_factory: Callable[[], Any], tg_user: Any) -> User:
    async with db_session_factory() as session:
        stmt = select(User).where(User.telegram_id == int(tg_user.id))
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.username = tg_user.username or existing.username or ""
            await session.commit()
            return existing
        user = User(telegram_id=int(tg_user.id), username=tg_user.username or "", is_allowed=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _upsert_chat(db_session_factory: Callable[[], Any], chat_id: int, thread_id: str) -> Chat:
    async with db_session_factory() as session:
        stmt = select(Chat).where(Chat.chat_id == chat_id)
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.thread_id = thread_id
            await session.commit()
            return existing
        chat = Chat(chat_id=chat_id, thread_id=thread_id)
        session.add(chat)
        await session.commit()
        await session.refresh(chat)
        return chat


def build_handlers(graph: Any, db_session_factory: Callable[[], Any], settings: Settings):
    rate_limiter = RateLimiter()
    auth = AuthMiddleware(settings.allowed_user_ids)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user:
            await _upsert_user(db_session_factory, update.effective_user)
        if update.effective_chat:
            await _upsert_chat(db_session_factory, int(update.effective_chat.id), f"tg:{update.effective_chat.id}")
        await update.message.reply_text(
            "Welcome to NEXUS AI. I'm your offline-first AI assistant."
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        await update.message.reply_text(
            "/start - initialize\n"
            "/help - show this message\n"
            "/status - show runtime status\n"
            "Send any message to chat with NEXUS."
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        model_loaded = "yes" if settings.model_path and __import__("pathlib").Path(settings.model_path).exists() else "no"
        await update.message.reply_text(
            f"model loaded: {model_loaded}\n"
            f"db path: {settings.db_path}\n"
            "memory enabled: yes"
        )

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        if not update.effective_user or not update.message or not update.message.text:
            return

        user_id = int(update.effective_user.id)
        if not auth.is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return

        if not rate_limiter.is_allowed(user_id):
            await update.message.reply_text("Rate limit exceeded. Please wait a moment.")
            return

        correlation_id = str(uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        chat_id = int(update.effective_chat.id) if update.effective_chat else 0
        thread_id = f"tg:{chat_id}"

        # Ensure DB entities exist.
        await _upsert_user(db_session_factory, update.effective_user)
        await _upsert_chat(db_session_factory, chat_id, thread_id)

        state: NexusState = {
            "thread_id": thread_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "correlation_id": correlation_id,
            "messages": [{"role": "user", "content": update.message.text}],
            "intent": "unknown",
            "active_persona": "gemma",
            "current_task": None,
            "tool_results": [],
            "memory_context": "",
            "response": "",
            "error": None,
            "turn_count": 0,
            "moderation_passed": True,
        }

        result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
        await update.message.reply_text(result.get("response") or "")

        logger.info(
            "handled_message",
            chat_id=chat_id,
            user_id=user_id,
            correlation_id=correlation_id,
            intent=result.get("intent"),
            response_len=len(result.get("response") or ""),
            tool_results=json.dumps(result.get("tool_results", [])),
        )

    return [
        CommandHandler("start", start),
        CommandHandler("help", help_cmd),
        CommandHandler("status", status),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ]
