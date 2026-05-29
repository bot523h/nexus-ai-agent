from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from sqlmodel import select
from telegram import Message, Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from nexus_ai_agent.config.settings import Settings

# Feature managers — lazy-initialised inside build_handlers
from nexus_ai_agent.features.channel_manager import ChannelManager
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.models import Chat, User

from .middleware import AuthMiddleware, RateLimiter

logger = get_logger(__name__)
SessionFactory = Callable[[], Any]


async def _upsert_user(db_session_factory: SessionFactory, tg_user: Any) -> User:
    async with db_session_factory() as session:
        stmt = select(User).where(User.telegram_id == int(tg_user.id))
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.username = tg_user.username or existing.username or ""
            await session.commit()
            return cast(User, existing)
        user = User(telegram_id=int(tg_user.id), username=tg_user.username or "", is_allowed=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _upsert_chat(db_session_factory: SessionFactory, chat_id: int, thread_id: str) -> Chat:
    async with db_session_factory() as session:
        stmt = select(Chat).where(Chat.chat_id == chat_id)
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.thread_id = thread_id
            await session.commit()
            return cast(Chat, existing)
        chat = Chat(chat_id=chat_id, thread_id=thread_id)
        session.add(chat)
        await session.commit()
        await session.refresh(chat)
        return chat


def _message(update: Update) -> Message | None:
    return update.effective_message


def _user_id(update: Update) -> int | None:
    return int(update.effective_user.id) if update.effective_user else None


def _chat_id(update: Update) -> int:
    return int(update.effective_chat.id) if update.effective_chat else 0


def _base_state(update: Update, text: str, *, persona: str = "gemma") -> NexusState:
    chat_id = _chat_id(update)
    return {
        "thread_id": f"tg:{chat_id}",
        "chat_id": chat_id,
        "user_id": _user_id(update) or 0,
        "correlation_id": str(uuid4()),
        "messages": [{"role": "user", "content": text}],
        "intent": "chat",
        "active_persona": persona,
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


async def _reply(update: Update, text: str) -> None:
    message = _message(update)
    if message is not None:
        await message.reply_text(text)


async def _heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    presence = context.application.bot_data.get("presence")
    if not isinstance(presence, PresenceStore):
        return
    for user_id in list(context.application.bot_data.get("heartbeat_user_ids", set())):
        presence.mark_online(int(user_id))


def build_handlers(
    graph: Any,
    db_session_factory: SessionFactory,
    settings: Settings,
    *,
    presence: PresenceStore | None = None,
    storage: Any | None = None,
) -> Sequence[Any]:
    rate_limiter = RateLimiter()
    auth = AuthMiddleware(settings.allowed_user_ids)
    presence_store = presence or PresenceStore()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        user_id = _user_id(update)
        if user_id is not None:
            presence_store.mark_online(user_id)
        if update.effective_user:
            await _upsert_user(db_session_factory, update.effective_user)
        if update.effective_chat:
            await _upsert_chat(db_session_factory, _chat_id(update), f"tg:{_chat_id(update)}")
        await _reply(update, "Welcome to NEXUS AI. I'm your offline-first AI assistant.")

    async def online(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        presence_store.mark_online(user_id)
        context.application.bot_data.setdefault("heartbeat_user_ids", set()).add(user_id)
        await _reply(update, "✅ You are online. Heartbeat is active.")

    async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        presence_store.mark_offline(user_id)
        context.application.bot_data.setdefault("heartbeat_user_ids", set()).discard(user_id)
        await _reply(update, "🔌 Disconnected. You are offline.")

    async def storage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        if storage is None:
            await _reply(update, "Storage: local cache only / not configured.")
            return
        try:
            keys = await storage.list_files(prefix="")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"Storage unavailable: {exc}")
            return
        preview = "\n".join(keys[:10]) if keys else "no files"
        await _reply(update, f"Storage files:\n{preview}")

    async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        model_path = Path(settings.model_path)
        status = "available" if model_path.exists() else "missing"
        await _reply(update, f"Model: {status}\nPath: {settings.model_path}")

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        await _reply(
            update,
            "/start - initialize\n"
            "/online - mark yourself online and enable heartbeat\n"
            "/disconnect - mark yourself offline\n"
            "/storage - show storage status\n"
            "/model - show model status\n"
            "/status - show runtime status\n"
            "Send any message to chat with NEXUS.",
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        user_id = _user_id(update)
        online_status = presence_store.is_online(user_id) if user_id is not None else False
        model_loaded = "yes" if settings.model_path and Path(settings.model_path).exists() else "no"
        await _reply(
            update,
            (
                f"online: {online_status}\n"
                f"model loaded: {model_loaded}\n"
                f"db path: {settings.db_path}\n"
                "memory enabled: yes"
            ),
        )

    # ── Phase 1: Channel & Group Management ────────────────────────
    channel_mgr = ChannelManager()

    async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Post text to the current channel/group. Usage: /post <text>"""
        if not context.args:
            await _reply(update, "❌ استفاده: /post <متن>")
            return
        text = " ".join(context.args)
        chat_id = _chat_id(update)
        try:
            channel_mgr.bot = context.bot
            await channel_mgr.post_to_channel(chat_id, text)
            await _reply(update, "✅ پست ارسال شد.")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ خطا: {exc}")

    async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Schedule a post. Usage: /schedule <YYYY-MM-DD HH:MM> <text>"""
        if not context.args or len(context.args) < 3:
            await _reply(update, "❌ استفاده: /schedule YYYY-MM-DD HH:MM <متن>")
            return
        date_str = context.args[0]
        time_str = context.args[1]
        text = " ".join(context.args[2:])
        try:
            when = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            await _reply(update, "❌ فرمت زمان نادرست. مثال: 2025-06-01 14:30")
            return
        chat_id = _chat_id(update)
        try:
            channel_mgr.bot = context.bot
            sid = await channel_mgr.schedule_post(chat_id, text, when)
            await _reply(update, f"✅ پست زمان‌بندی شد (id={sid}).")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ خطا: {exc}")

    async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ban a user (reply to their message or give user_id)."""
        chat_id = _chat_id(update)
        target_id: int | None = None
        # Try from reply
        if (
            update.message
            and update.message.reply_to_message
            and update.message.reply_to_message.from_user
        ):
            target_id = update.message.reply_to_message.from_user.id
        elif context.args:
            try:
                target_id = int(context.args[0])
            except ValueError:
                await _reply(update, "❌ شناسه کاربر باید عدد باشد.")
                return
        if target_id is None:
            await _reply(update, "❌ ریپلای روی پیام کاربر یا /ban <user_id>")
            return
        channel_mgr.bot = context.bot
        ok = await channel_mgr.ban_user(chat_id, target_id)
        await _reply(update, "✅ کاربر بن شد." if ok else "❌ خطا در بن کردن.")

    async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Unban a user by id."""
        if not context.args:
            await _reply(update, "❌ استفاده: /unban <user_id>")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        chat_id = _chat_id(update)
        channel_mgr.bot = context.bot
        ok = await channel_mgr.unban_user(chat_id, target_id)
        await _reply(update, "✅ کاربر آزاد شد." if ok else "❌ خطا در آزاد کردن.")

    async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show group/channel statistics."""
        chat_id = _chat_id(update)
        channel_mgr.bot = context.bot
        try:
            count = await channel_mgr.get_members_count(chat_id)
            admins = await channel_mgr.get_admins(chat_id)
            admin_names = ", ".join(
                a.get("username", str(a.get("user_id", "?"))) for a in admins[:10]
            )
            await _reply(update, f"📊 آمار:\n👥 اعضا: {count}\n🛡 ادمین‌ها: {admin_names}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ خطا: {exc}")

    async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set welcome message. Use {name} for new member name."""
        if not context.args:
            await _reply(update, "❌ استفاده: /welcome <متن> — {name} جای اسم عضو جدید")
            return
        text = " ".join(context.args)
        chat_id = _chat_id(update)
        channel_mgr.set_welcome_message(chat_id, text)
        await _reply(update, f"✅ پیام خوشامد تنظیم شد:\n{text}")

    async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pin the replied-to message."""
        if not update.message or not update.message.reply_to_message:
            await _reply(update, "❌ ریپلای روی پیامی که می‌خوای پین بشه")
            return
        chat_id = _chat_id(update)
        msg_id = update.message.reply_to_message.message_id
        channel_mgr.bot = context.bot
        try:
            await channel_mgr.pin_message(chat_id, msg_id)
            await _reply(update, "📌 پیام پین شد.")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ خطا: {exc}")

    async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send welcome message when a new member joins."""
        if not update.message or not update.message.new_chat_members:
            return
        chat_id = _chat_id(update)
        channel_mgr.bot = context.bot
        for member in update.message.new_chat_members:
            name = member.first_name or "دوست جدید"
            await channel_mgr.welcome_new_member(chat_id, name)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        if not update.effective_user or not update.message or not update.message.text:
            return

        user_id = int(update.effective_user.id)
        presence_store.mark_online(user_id)
        if not auth.is_allowed(user_id):
            await _reply(update, "Access denied.")
            return

        if not rate_limiter.is_allowed(user_id):
            await _reply(update, "Rate limit exceeded. Please wait a moment.")
            return

        correlation_id = str(uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        chat_id = _chat_id(update)
        thread_id = f"tg:{chat_id}"

        await _upsert_user(db_session_factory, update.effective_user)
        await _upsert_chat(db_session_factory, chat_id, thread_id)

        state = _base_state(update, update.message.text)
        state["correlation_id"] = correlation_id
        state["intent"] = "unknown"

        result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
        await _reply(update, result.get("response") or "")

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
        CommandHandler("online", online),
        CommandHandler("disconnect", disconnect),
        CommandHandler("storage", storage_cmd),
        CommandHandler("model", model_cmd),
        CommandHandler("help", help_cmd),
        CommandHandler("status", status),
        # Phase 1: Channel & Group Management
        CommandHandler("post", post_cmd),
        CommandHandler("schedule", schedule_cmd),
        CommandHandler("ban", ban_cmd),
        CommandHandler("unban", unban_cmd),
        CommandHandler("stats", stats_cmd),
        CommandHandler("welcome", welcome_cmd),
        CommandHandler("pin", pin_cmd),
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ]


async def story_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else "Begin a new adventure story"
    graph = context.application.bot_data["graph"]
    state = _base_state(update, text, persona="qwen")
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})
    await _reply(update, result.get("response", ""))


async def companion_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    graph = context.application.bot_data["graph"]
    state = _base_state(update, "Hello, I'd like to talk", persona="gemma")
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})
    await _reply(update, result.get("response", ""))


async def analyze_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else "Analyze the current situation"
    graph = context.application.bot_data["graph"]
    state = _base_state(update, text, persona="phi")
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})
    await _reply(update, result.get("response", ""))


async def persona_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    await _reply(
        update,
        "🤖 NEXUS Active Cores:\n"
        "• /story   → Qwen (Storytelling)\n"
        "• /companion → Gemma (Social/Emotion)\n"
        "• /analyze  → Phi (Logic/Analysis)\n"
        "Just chat normally for auto-routing.",
    )


def install_presence_heartbeat(application: Any, *, interval_seconds: float = 30.0) -> None:
    if application.job_queue is None:

        async def _loop() -> None:
            while True:
                presence = application.bot_data.get("presence")
                if isinstance(presence, PresenceStore):
                    for user_id in list(application.bot_data.get("heartbeat_user_ids", set())):
                        presence.mark_online(int(user_id))
                await asyncio.sleep(interval_seconds)

        application.bot_data["presence_heartbeat_task_factory"] = _loop
        return
    application.job_queue.run_repeating(
        _heartbeat, interval=interval_seconds, first=interval_seconds
    )
