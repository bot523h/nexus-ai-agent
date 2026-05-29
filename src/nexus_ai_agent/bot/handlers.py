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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ads import AdManager

# Feature managers — lazy-initialised inside build_handlers
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.channel_manager import ChannelManager
from nexus_ai_agent.features.engagement import EngagementEngine
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.features.games import NumberGuess, QuickPoll, QuizGame, WordleFA
from nexus_ai_agent.features.owner_control import OwnerControl, is_owner
from nexus_ai_agent.features.personality import PersonalityEngine
from nexus_ai_agent.features.tools import Calculator, ReminderSystem, Translator, UnitConverter
from nexus_ai_agent.features.viral_engine import ViralEngine
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

        # ── Phase 5: Main Menu with Inline Keyboard ──
        keyboard = [
            [
                InlineKeyboardButton("💬 چت هوشمند", callback_data="menu_chat"),
                InlineKeyboardButton("🎮 بازی‌ها", callback_data="menu_games"),
            ],
            [
                InlineKeyboardButton("👤 چت ناشناس", callback_data="menu_anon"),
                InlineKeyboardButton("📢 کانال", callback_data="menu_channel"),
            ],
            [
                InlineKeyboardButton("🛠️ ابزارها", callback_data="menu_tools"),
                InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = _message(update)
        if msg is not None:
            await msg.reply_text(
                "🤖 NEXUS AI\n\nیکی از گزینه‌ها رو انتخاب کن:",
                reply_markup=reply_markup,
            )

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
            "🤖 NEXUS AI v1.2.0 — راهنما\n\n"
            "━━━ 💬 چت ━━━\n"
            "هر پیامی بفرست = چت با AI\n"
            "/persona → شخصیت‌ها\n"
            "/story /companion /analyze\n\n"
            "━━━ 👤 ناشناس ━━━\n"
            "/anon_start /anon_stop /anon_report\n\n"
            "━━━ 🎮 بازی ━━━\n"
            "/quiz /guess_start /wordle /poll\n"
            "/leaderboard /guess_stop /wordle_stop\n\n"
            "━━━ 📢 کانال ━━━\n"
            "/post /schedule /ban /unban\n"
            "/stats /welcome /pin\n\n"
            "━━━ 🛠 ابزار ━━━\n"
            "/remind /tr /convert /calc\n\n"
            "━━━ ⚙️ سیستم ━━━\n"
            "/start → منوی اصلی\n"
            "/online /disconnect /status\n"
            "/help → همین پیام",
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

    # ── Phase 2: Anonymous Chat ────────────────────────────────────
    anon_mgr = AnonymousChatManager()

    async def anon_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Join the anonymous chat queue."""
        user_id = _user_id(update)
        if user_id is None:
            return
        anon_mgr.bot = context.bot
        result = await anon_mgr.join_queue(user_id)
        await _reply(update, result)

    async def anon_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Leave the anonymous chat."""
        user_id = _user_id(update)
        if user_id is None:
            return
        anon_mgr.bot = context.bot
        result = await anon_mgr.leave_chat(user_id)
        await _reply(update, result)

    async def anon_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report the current anonymous chat partner."""
        user_id = _user_id(update)
        if user_id is None:
            return
        anon_mgr.bot = context.bot
        result = await anon_mgr.report_user(user_id, settings.owner_telegram_id)
        await _reply(update, result)

    async def anon_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward text messages as anonymous if user is in an anon session."""
        user_id = _user_id(update)
        if user_id is None or not update.message or not update.message.text:
            return
        anon_mgr.bot = context.bot
        # Only intercept if the user is in an active anon session AND in private chat
        if (
            user_id in anon_mgr._active
            and update.effective_chat
            and update.effective_chat.type == "private"
        ):
            ok = await anon_mgr.send_anon_message(user_id, update.message.text)
            if ok:
                await _reply(update, "✅ پیام ناشناس ارسال شد.")
            else:
                await _reply(update, "❌ خطا در ارسال پیام ناشناس.")

    # ── Phase 3: Games & Entertainment ─────────────────────────────
    quiz_game = QuizGame()
    number_guess = NumberGuess()
    wordle_fa = WordleFA()
    quick_poll = QuickPoll()

    async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a quiz question with inline keyboard."""
        user_id = _user_id(update)
        if user_id is None:
            return
        q = quiz_game.get_question(user_id)
        if q is None:
            await _reply(update, "❌ سوالی موجود نیست.")
            return
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"quiz_{user_id}_{i}")]
            for i, opt in enumerate(q["options"])
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = _message(update)
        if msg is not None:
            await msg.reply_text(f"❓ {q['q']}", reply_markup=reply_markup)

    async def quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle quiz inline keyboard answer."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        # Parse: quiz_{user_id}_{choice_idx}
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] != "quiz":
            return
        try:
            target_user = int(parts[1])
            choice = int(parts[2])
        except ValueError:
            return
        user_id = query.from_user.id if query.from_user else 0
        if user_id != target_user:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        correct = quiz_game.check_answer(user_id, choice)
        chat_id = getattr(query.message, "chat_id", 0) or 0
        score = quiz_game.update_score(user_id, chat_id, correct)
        emoji = "✅" if correct else "❌"
        await query.edit_message_text(
            f"{emoji} {'درست!' if correct else 'نادرست!'}\nامتیاز شما: {score}"
        )
        quiz_game.clear(user_id)

    async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show quiz leaderboard."""
        chat_id = _chat_id(update)
        board = quiz_game.get_leaderboard(chat_id)
        if not board:
            await _reply(update, "📊 هنوز امتیازی ثبت نشده.")
            return
        lines = ["🏆 جدول امتیازات:"]
        for i, entry in enumerate(board, 1):
            lines.append(f"  {i}. کاربر {entry['user_id']}: {entry['score']}/{entry['answered']}")
        await _reply(update, "\n".join(lines))

    async def guess_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a number guessing game."""
        user_id = _user_id(update)
        if user_id is None:
            return
        result = number_guess.start(user_id)
        await _reply(update, result)

    async def guess_number_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle number guesses when a game is active."""
        user_id = _user_id(update)
        if user_id is None or not update.message or not update.message.text:
            return
        if not number_guess.is_active(user_id):
            return
        try:
            number = int(update.message.text.strip())
        except ValueError:
            return  # not a number, ignore
        result = number_guess.guess(user_id, number)
        await _reply(update, result)

    async def guess_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Stop the number guessing game."""
        user_id = _user_id(update)
        if user_id is None:
            return
        result = number_guess.stop(user_id)
        await _reply(update, result)

    async def wordle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a Persian Wordle game."""
        user_id = _user_id(update)
        if user_id is None:
            return
        result = wordle_fa.start(user_id)
        await _reply(update, result)

    async def wordle_guess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle Wordle guesses when a game is active."""
        user_id = _user_id(update)
        if user_id is None or not update.message or not update.message.text:
            return
        if not wordle_fa.is_active(user_id):
            return
        word = update.message.text.strip()
        if len(word) != 5:
            return  # not a 5-letter word, skip
        # Check it's Persian/Arabic script
        if not all("\u0600" <= c <= "\u06ff" or "\ufb50" <= c <= "\ufdff" for c in word):
            return
        result = wordle_fa.guess(user_id, word)
        await _reply(update, result)

    async def wordle_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Stop the current Wordle game."""
        user_id = _user_id(update)
        if user_id is None:
            return
        result = wordle_fa.stop(user_id)
        await _reply(update, result)

    async def poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a quick poll. Usage: /poll سوال | گزینه1 | گزینه2 ..."""
        if not context.args:
            await _reply(update, "❌ استفاده: /poll سوال | گزینه۱ | گزینه۲")
            return
        full = " ".join(context.args)
        parts = [p.strip() for p in full.split("|")]
        if len(parts) < 3:
            await _reply(update, "❌ حداقل ۲ گزینه نیاز است: /poll سوال | گزینه۱ | گزینه۲")
            return
        question = parts[0]
        options = parts[1:]
        poll_id = quick_poll.create(question, options)
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"poll_{poll_id}_{i}")]
            for i, opt in enumerate(options)
        ]
        keyboard.append([InlineKeyboardButton("📊 نتایج", callback_data=f"pollr_{poll_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = _message(update)
        if msg is not None:
            await msg.reply_text(f"📊 {question}", reply_markup=reply_markup)

    async def poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle poll vote and results callbacks."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        data = query.data
        if data.startswith("pollr_"):
            poll_id = data[6:]
            results = quick_poll.get_results(poll_id)
            if results:
                await query.edit_message_text(results)
            return
        if data.startswith("poll_"):
            parts = data.split("_")
            if len(parts) != 3:
                return
            poll_id = parts[1]
            try:
                option_idx = int(parts[2])
            except ValueError:
                return
            user_id = query.from_user.id if query.from_user else 0
            ok = quick_poll.vote(poll_id, option_idx, user_id)
            if ok:
                await query.answer("✅ رأی ثبت شد!", show_alert=True)
            else:
                await query.answer("⚠️ قبلاً رأی داده‌اید!", show_alert=True)
            # Show updated results
            results = quick_poll.get_results(poll_id)
            if results and query.message:
                # Rebuild keyboard
                poll = quick_poll.get_poll(poll_id)
                if poll:
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                opt,
                                callback_data=f"poll_{poll_id}_{i}",
                            )
                        ]
                        for i, opt in enumerate(poll["options"])
                    ]
                    keyboard.append(
                        [InlineKeyboardButton("📊 نتایج", callback_data=f"pollr_{poll_id}")]
                    )
                    await query.edit_message_text(
                        results, reply_markup=InlineKeyboardMarkup(keyboard)
                    )

    # ── Phase 4: Utility Tools ─────────────────────────────────────
    reminder_sys = ReminderSystem()
    translator = Translator()
    converter = UnitConverter()
    calculator = Calculator()

    async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set a reminder. Usage: /remind 30m نماز"""
        if not context.args or len(context.args) < 2:
            await _reply(update, "❌ استفاده: /remind 30m متن — واحدها: s/m/h/d")
            return
        time_str = context.args[0]
        text = " ".join(context.args[1:])
        user_id = _user_id(update)
        if user_id is None:
            return
        chat_id = _chat_id(update)
        reminder_sys.bot = context.bot
        result = await reminder_sys.set_reminder(user_id, chat_id, time_str, text)
        await _reply(update, result)

    async def tr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Translate text. Usage: /tr [en] متن — default: fa→en"""
        if not context.args:
            await _reply(
                update,
                "❌ استفاده: /tr متن (فارسی→انگلیسی)\nیا /tr en متن (انگلیسی→فارسی)",
            )
            return
        # Check if first arg is a language code
        source = "fa"
        target = "en"
        start_idx = 0
        if len(context.args) >= 2 and len(context.args[0]) == 2:
            lang = context.args[0].lower()
            if lang == "en":
                source = "en"
                target = "fa"
            elif lang == "fa":
                source = "fa"
                target = "en"
            else:
                source = lang
                target = "fa"
            start_idx = 1
        text = " ".join(context.args[start_idx:])
        result = await translator.translate(text, source=source, target=target)
        await _reply(update, f"🌐 ترجمه:\n{result}")

    async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Convert units. Usage: /convert 100 usd to irt"""
        if not context.args or len(context.args) < 4:
            await _reply(
                update,
                "❌ استفاده: /convert 100 usd to irt\n"
                "ارز: usd, eur, irt, gbp, cad, aud, jpy, cny\n"
                "طول: km, m, cm, mm, mile, yard, ft, in\n"
                "وزن: kg, g, mg, lb, oz, ton\n"
                "دما: c, f, k",
            )
            return
        try:
            amount = float(context.args[0])
        except ValueError:
            await _reply(update, "❌ مقدار باید عدد باشد.")
            return
        from_unit = context.args[1]
        # args[2] should be "to"
        to_unit = context.args[3]
        result = converter.convert(amount, from_unit, to_unit)
        await _reply(update, result)

    async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Safe calculator. Usage: /calc 2^10 + sin(45)"""
        if not context.args:
            await _reply(update, "❌ استفاده: /calc عبارت_ریاضی")
            return
        expr = " ".join(context.args)
        result = calculator.evaluate(expr)
        await _reply(update, result)

    # ── Phase 5: Inline Keyboard Menu Callbacks ────────────────────

    async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all menu button callbacks."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        data = query.data

        if data == "menu_chat":
            keyboard = [
                [InlineKeyboardButton("💬 شروع چت", callback_data="chat_start")],
                [InlineKeyboardButton("🤖 شخصیت‌ها", callback_data="chat_personas")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "💬 چت هوشمند\n\nبا NEXUS AI چت کن یا شخصیت مورد نظرت رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "chat_start":
            await query.edit_message_text(
                "💬 کافیه پیام بفرستی تا NEXUS AI جواب بده!\nبرای تغییر شخصیت: /persona"
            )

        elif data == "chat_personas":
            await query.edit_message_text(
                "🤖 شخصیت‌های NEXUS:\n\n"
                "• /story → Qwen (داستان‌سرایی)\n"
                "• /companion → Gemma (اجتماعی/هیجانی)\n"
                "• /analyze → Phi (منطق/تحلیل)\n\n"
                "چت عادی = مسیریابی خودکار"
            )

        elif data == "menu_games":
            keyboard = [
                [InlineKeyboardButton("❓ کوییز", callback_data="game_quiz")],
                [InlineKeyboardButton("🔢 حدس عدد", callback_data="game_guess")],
                [InlineKeyboardButton("🟩 وردل فارسی", callback_data="game_wordle")],
                [InlineKeyboardButton("📊 نظرسنجی", callback_data="game_poll")],
                [InlineKeyboardButton("🏆 جدول امتیازات", callback_data="game_leaderboard")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🎮 بازی‌ها\n\nیکی رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "game_quiz":
            await query.edit_message_text("❓ کوییز\n\nبرای شروع: /quiz")

        elif data == "game_guess":
            await query.edit_message_text(
                "🔢 حدس عدد\n\nبرای شروع: /guess_start\nبعد عدد حدسی بفرست.\nبرای توقف: /guess_stop"
            )

        elif data == "game_wordle":
            await query.edit_message_text(
                "🟩 وردل فارسی\n\nبرای شروع: /wordle\n"
                "کلمه ۵ حرفی فارسی حدس بزن.\nبرای توقف: /wordle_stop"
            )

        elif data == "game_poll":
            await query.edit_message_text(
                "📊 نظرسنجی سریع\n\nاستفاده: /poll سوال | گزینه۱ | گزینه۲"
            )

        elif data == "game_leaderboard":
            await query.edit_message_text("🏆 جدول امتیازات\n\n/leaderboard")

        elif data == "menu_anon":
            keyboard = [
                [InlineKeyboardButton("🟢 ورود به صف", callback_data="anon_join")],
                [InlineKeyboardButton("🔴 قطع چت", callback_data="anon_leave")],
                [InlineKeyboardButton("🚨 گزارش", callback_data="anon_rep")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "👤 چت ناشناس\n\nبا کاربر ناشناس چت کن بدون اینکه هویتت فاش بشه:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "anon_join":
            user_id = query.from_user.id if query.from_user else 0
            anon_mgr.bot = context.bot
            result = await anon_mgr.join_queue(user_id)
            await query.edit_message_text(result)

        elif data == "anon_leave":
            user_id = query.from_user.id if query.from_user else 0
            anon_mgr.bot = context.bot
            result = await anon_mgr.leave_chat(user_id)
            await query.edit_message_text(result)

        elif data == "anon_rep":
            user_id = query.from_user.id if query.from_user else 0
            anon_mgr.bot = context.bot
            result = await anon_mgr.report_user(user_id, settings.owner_telegram_id)
            await query.edit_message_text(result)

        elif data == "menu_channel":
            keyboard = [
                [InlineKeyboardButton("📝 پست در کانال", callback_data="ch_post")],
                [InlineKeyboardButton("📋 زمان‌بندی", callback_data="ch_schedule")],
                [InlineKeyboardButton("👋 پیام خوشامد", callback_data="ch_welcome")],
                [InlineKeyboardButton("📊 آمار", callback_data="ch_stats")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "📢 مدیریت کانال و گروه\n\n",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "ch_post":
            await query.edit_message_text("📝 پست در کانال\n\nاستفاده: /post متن")

        elif data == "ch_schedule":
            await query.edit_message_text(
                "📋 زمان‌بندی پست\n\nاستفاده: /schedule YYYY-MM-DD HH:MM متن"
            )

        elif data == "ch_welcome":
            await query.edit_message_text(
                "👋 پیام خوشامد\n\nاستفاده: /welcome متن\n{name} = اسم عضو جدید"
            )

        elif data == "ch_stats":
            await query.edit_message_text("📊 آمار\n\nاستفاده: /stats")

        elif data == "menu_tools":
            keyboard = [
                [InlineKeyboardButton("⏰ یادآور", callback_data="tool_remind")],
                [InlineKeyboardButton("🌐 ترجمه", callback_data="tool_tr")],
                [InlineKeyboardButton("💱 تبدیل واحد", callback_data="tool_convert")],
                [InlineKeyboardButton("🧮 ماشین‌حساب", callback_data="tool_calc")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🛠️ ابزارهای کاربردی\n\nیکی رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "tool_remind":
            await query.edit_message_text("⏰ یادآور\n\nاستفاده: /remind 30m متن\nواحدها: s/m/h/d")

        elif data == "tool_tr":
            await query.edit_message_text(
                "🌐 ترجمه\n\n/tr متن → فارسی به انگلیسی\n/tr en متن → انگلیسی به فارسی"
            )

        elif data == "tool_convert":
            await query.edit_message_text(
                "💱 تبدیل واحد\n\n"
                "/convert 100 usd to irt\n"
                "/convert 5 km to mile\n"
                "/convert 32 f to c"
            )

        elif data == "tool_calc":
            await query.edit_message_text(
                "🧮 ماشین‌حساب\n\n/calc 2^10 + sin(45)\nتوابع: sin, cos, tan, sqrt, log, pi, e"
            )

        elif data == "menu_settings":
            keyboard = [
                [InlineKeyboardButton("🟢 آنلاین", callback_data="set_online")],
                [InlineKeyboardButton("🔴 آفلاین", callback_data="set_offline")],
                [InlineKeyboardButton("📋 وضعیت", callback_data="set_status")],
                [InlineKeyboardButton("ℹ️ راهنما", callback_data="set_help")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "⚙️ تنظیمات\n\n",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "set_online":
            user_id = query.from_user.id if query.from_user else 0
            if user_id:
                presence_store.mark_online(user_id)
                context.application.bot_data.setdefault("heartbeat_user_ids", set()).add(user_id)
            await query.edit_message_text("✅ شما آنلاین هستید. Heartbeat فعال شد.")

        elif data == "set_offline":
            user_id = query.from_user.id if query.from_user else 0
            if user_id:
                presence_store.mark_offline(user_id)
                hb = context.application.bot_data.setdefault("heartbeat_user_ids", set())
                hb.discard(user_id)
            await query.edit_message_text("🔌 قطع شد. شما آفلاین هستید.")

        elif data == "set_status":
            user_id = query.from_user.id if query.from_user else 0
            on = presence_store.is_online(user_id) if user_id else False
            await query.edit_message_text(f"📋 وضعیت: {'آنلاین' if on else 'آفلاین'}")

        elif data == "set_help":
            await query.edit_message_text(
                "ℹ️ راهنمای NEXUS AI v1.2.0\n\n"
                "💬 چت: فقط پیام بفرست\n"
                "🎮 بازی‌ها: /quiz /guess_start /wordle /poll\n"
                "👤 ناشناس: /anon_start /anon_stop /anon_report\n"
                "📢 کانال: /post /schedule /ban /unban /stats /welcome /pin\n"
                "🛠 ابزارها: /remind /tr /convert /calc\n"
                "⚙️ تنظیمات: /online /disconnect /status /help"
            )

        elif data == "menu_back":
            keyboard = [
                [
                    InlineKeyboardButton("💬 چت هوشمند", callback_data="menu_chat"),
                    InlineKeyboardButton("🎮 بازی‌ها", callback_data="menu_games"),
                ],
                [
                    InlineKeyboardButton("👤 چت ناشناس", callback_data="menu_anon"),
                    InlineKeyboardButton("📢 کانال", callback_data="menu_channel"),
                ],
                [
                    InlineKeyboardButton("🛠️ ابزارها", callback_data="menu_tools"),
                    InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"),
                ],
            ]
            await query.edit_message_text(
                "🤖 NEXUS AI\n\nیکی از گزینه‌ها رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

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

    # ── Phase 7: Owner Control ────────────────────────────────────
    async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show owner dashboard."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        status = OwnerControl.system_status()
        await _reply(update, status)

    async def system_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system status (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        await _reply(update, OwnerControl.system_status())

    async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Broadcast a message to all chats (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن پیام را وارد کنید: /broadcast <text>")
            return
        from sqlalchemy import create_engine as _ce
        from sqlmodel import Session as _Session

        from nexus_ai_agent.config.settings import get_settings as _gs

        _eng = _ce(f"sqlite:///{_gs().db_path}", echo=False)
        with _Session(_eng) as _s:
            from nexus_ai_agent.storage.models import Chat as _Chat

            _chats = _s.exec(select(_Chat)).all()
            _ids = [c.chat_id for c in _chats]
        result = await OwnerControl.owner_broadcast(context.bot, _ids, text)
        await _reply(
            update,
            f"📢 پیام ارسال شد\n✅ موفق: {result['success']}\n❌ ناموفق: {result['failed']}",
        )

    async def broadcast_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Broadcast to every known chat (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن پیام را وارد کنید: /broadcast_all <text>")
            return
        from sqlalchemy import create_engine as _ce
        from sqlmodel import Session as _Session

        from nexus_ai_agent.config.settings import get_settings as _gs

        _eng = _ce(f"sqlite:///{_gs().db_path}", echo=False)
        with _Session(_eng) as _s:
            from nexus_ai_agent.storage.models import Chat as _Chat

            _chats = _s.exec(select(_Chat)).all()
            _ids = [c.chat_id for c in _chats]
        result = await OwnerControl.owner_broadcast(context.bot, _ids, text)
        await _reply(
            update,
            f"📢 ارسال به همه چت‌ها\n✅ موفق: {result['success']}\n❌ ناموفق: {result['failed']}",
        )

    async def admin_logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent admin logs (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        logs = OwnerControl.admin_logs(limit=10)
        if not logs:
            await _reply(update, "📋 لاگ ادمینی وجود ندارد.")
            return
        lines = ["📋 **لاگ‌های ادمین**\n━━━━━━━━━━━━━━━━"]
        for log in logs:
            lines.append(f"• [{log['action']}] {log['target']} — {log['details'][:50]}")
        await _reply(update, "\n".join(lines))

    # ── Phase 8: Force Join ────────────────────────────────────────
    force_join_mgr = ForceJoinManager()

    async def forcejoin_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable force-join for the current chat (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        cfg = ForceJoinManager.set_config(
            chat_id, enabled=True, channel_username="@nexus_ai_official"
        )
        await _reply(update, f"✅ عضوگیری اجباری فعال شد.\n📢 کانال: {cfg.channel_username}")

    async def forcejoin_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable force-join (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        ForceJoinManager.set_config(chat_id, enabled=False)
        await _reply(update, "❌ عضوگیری اجباری غیرفعال شد.")

    async def forcejoin_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show force-join status."""
        chat_id = _chat_id(update)
        cfg = ForceJoinManager.get_config(chat_id)
        if cfg is None or not cfg.enabled:
            await _reply(update, "📋 عضوگیری اجباری: غیرفعال")
            return
        await _reply(
            update,
            f"📋 عضوگیری اجباری: فعال\n📢 کانال: {cfg.channel_username}",
        )

    async def forcejoin_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set custom force-join message (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن را وارد کنید: /forcejoin_message <text>")
            return
        chat_id = _chat_id(update)
        ForceJoinManager.set_config(chat_id, enabled=True, welcome_message=text)
        await _reply(update, "✅ پیام عضوگیری اجباری تغییر کرد.")

    async def forcejoin_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle the 'verify' button press."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        user_id = query.from_user.id
        is_member = await force_join_mgr.check_membership(user_id)
        if is_member:
            force_join_mgr.invalidate_cache(user_id)
            await query.edit_message_text("✅ عضویت شما تأیید شد! می‌تونید از ربات استفاده کنید.")
        else:
            await query.edit_message_text(
                "❌ شما هنوز در کانال عضو نشدید. لطفاً اول عضو بشید."
            )

    # ── Phase 9: Personality Engine ────────────────────────────────
    async def personality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage group personality: list, current, set."""
        args = context.args or []
        chat_id = _chat_id(update)

        if not args or args[0] == "list":
            await _reply(update, PersonalityEngine.list_personalities())
            return

        if args[0] == "current":
            await _reply(update, PersonalityEngine.current_personality(chat_id))
            return

        if args[0] == "set" and len(args) >= 2:
            user_id = update.effective_user.id if update.effective_user else 0
            result = PersonalityEngine.set_personality(
                chat_id, args[1], set_by=user_id
            )
            await _reply(update, result)
            return

        await _reply(
            update,
            "🎭 شخصیت گروه\n━━━━━━━━━━━━━━━━\n"
            "/personality list — لیست شخصیت‌ها\n"
            "/personality current — شخصیت فعلی\n"
            "/personality set <name> — تغییر شخصیت",
        )

    # ── Phase 10: Engagement ───────────────────────────────────────

    async def engagement_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable auto-engagement (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        freq = 60
        if context.args:
            try:
                freq = int(context.args[0])
            except ValueError:
                pass
        EngagementEngine.set_config(chat_id, enabled=True, frequency_minutes=freq)
        await _reply(update, f"✅ تعامل خودکار فعال شد (هر {freq} دقیقه)")

    async def engagement_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable auto-engagement (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        EngagementEngine.set_config(chat_id, enabled=False)
        await _reply(update, "❌ تعامل خودکار غیرفعال شد.")

    async def challenge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random challenge."""
        await _reply(update, EngagementEngine.get_challenge())

    async def joke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random Persian joke."""
        await _reply(update, EngagementEngine.get_joke())

    async def event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random group event prompt."""
        await _reply(update, EngagementEngine.get_event())

    # ── Phase 11: Viral Content Engine ──────────────────────────────────────────

    async def viral_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Generate and post a viral content now (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = ViralEngine.generate_post()
        score = ViralEngine.calculate_viral_score(text)
        if "#" not in text:
            tags = ViralEngine.auto_hashtags(text)
            text = f"{text}\n\n{tags}"
            score = ViralEngine.calculate_viral_score(text)
        chat_id = _chat_id(update)
        ViralEngine.save_post(chat_id, text, viral_score=score)
        await _reply(
            update,
            f"🔥 پست وایرال تولید شد!\n\n{text}\n\n📊 امتیاز وایرال: {score:.1f}/10",
        )

    async def viral_preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Preview a viral post without sending (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = ViralEngine.generate_post()
        score = ViralEngine.calculate_viral_score(text)
        if "#" not in text:
            tags = ViralEngine.auto_hashtags(text)
            text = f"{text}\n\n{tags}"
            score = ViralEngine.calculate_viral_score(text)
        await _reply(
            update,
            f"👁 پیش‌نمایش پست وایرال:\n\n{text}\n\n"
            f"📊 امتیاز وایرال: {score:.1f}/10\n\n"
            "✅ /viral_now برای ارسال\n📋 /viral_schedule برای زمان‌بندی",
        )

    async def viral_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show viral engine statistics (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        stats = ViralEngine.get_stats(chat_id)
        await _reply(
            update,
            f"📊 آمار موتور وایرال\n━━━━━━━━━━━━━━━━━━\n"
            f"📝 کل: {stats['total']}\n"
            f"⏳ در انتظار: {stats['pending']}\n"
            f"✅ ارسال شده: {stats['posted']}\n"
            f"❌ ناموفق: {stats['failed']}",
        )

    async def viral_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show pending viral posts (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        pending = ViralEngine.get_pending_posts(chat_id, limit=5)
        if not pending:
            await _reply(update, "📋 هیچ پست وایرال در انتظاری وجود ندارد.")
            return
        lines = ["📋 پست‌های وایرال در انتظار:\n━━━━━━━━━━━━━━━━━━"]
        for p in pending:
            lines.append(f"#{p['id']} | امتیاز: {p['viral_score']:.1f} | {p['text'][:60]}...")
        await _reply(update, "\n".join(lines))

    # ── Phase 12: Advertisement System ─────────────────────────────────────────

    async def ad_create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create an ad campaign (owner only). Usage: /ad_create <interval_h> <text>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args or len(context.args) < 2:
            await _reply(
                update,
                "❌ استفاده: /ad_create <فاصله_ساعت> <متن>\n"
                "مثال: /ad_create 24 🔥 پیشنهاد ویژه امروز!",
            )
            return
        try:
            interval = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ فاصله زمانی باید عدد (ساعت) باشد.")
            return
        text = " ".join(context.args[1:])
        chat_id = _chat_id(update)
        user_id = update.effective_user.id if update.effective_user else 0
        cid = AdManager.create_campaign(
            chat_id, text, interval_hours=interval, created_by=user_id
        )
        await _reply(update, f"✅ کمپین تبلیغاتی ایجاد شد\n🆔 شناسه: {cid}\n⏰ هر {interval} ساعت")

    async def ad_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List ad campaigns (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        campaigns = AdManager.list_campaigns(chat_id)
        if not campaigns:
            await _reply(update, "📋 هیچ کمپین تبلیغاتی وجود ندارد.")
            return
        lines = ["📋 کمپین‌های تبلیغاتی:\n━━━━━━━━━━━━━━━━━━"]
        for c in campaigns:
            status_icon = {"active": "🟢", "paused": "⏸️", "completed": "✅"}.get(
                c["status"], "❓"
            )
            lines.append(
                f"{status_icon} #{c['id']} | هر {c['interval_hours']}ساعت | "
                f"تکرار: {c['repeat_count']}/{c['max_repeats'] or '∞'} | "
                f"{c['text'][:40]}..."
            )
        await _reply(update, "\n".join(lines))

    async def ad_pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pause an ad campaign (owner only). Usage: /ad_pause <id>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args:
            await _reply(update, "❌ استفاده: /ad_pause <شناسه_کمپین>")
            return
        try:
            cid = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        ok = AdManager.pause_campaign(cid)
        await _reply(update, f"⏸️ کمپین #{cid} متوقف شد." if ok else f"❌ کمپین #{cid} یافت نشد.")

    async def ad_resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resume a paused ad campaign (owner only). Usage: /ad_resume <id>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args:
            await _reply(update, "❌ استفاده: /ad_resume <شناسه_کمپین>")
            return
        try:
            cid = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        ok = AdManager.resume_campaign(cid)
        await _reply(update, f"▶️ کمپین #{cid} فعال شد." if ok else f"❌ کمپین #{cid} یافت نشد.")

    async def ad_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delete an ad campaign (owner only). Usage: /ad_delete <id>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args:
            await _reply(update, "❌ استفاده: /ad_delete <شناسه_کمپین>")
            return
        try:
            cid = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        ok = AdManager.delete_campaign(cid)
        await _reply(update, f"🗑️ کمپین #{cid} حذف شد." if ok else f"❌ کمپین #{cid} یافت نشد.")

    async def ad_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show ad system statistics (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        stats = AdManager.get_stats(chat_id)
        await _reply(
            update,
            f"📊 آمار تبلیغات\n━━━━━━━━━━━━━━━━━━\n"
            f"📝 کل: {stats['total']}\n"
            f"🟢 فعال: {stats['active']}\n"
            f"⏸️ متوقف: {stats['paused']}\n"
            f"✅ تکمیل: {stats['completed']}",
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
        # Phase 2: Anonymous Chat
        CommandHandler("anon_start", anon_start_cmd),
        CommandHandler("anon_stop", anon_stop_cmd),
        CommandHandler("anon_report", anon_report_cmd),
        # Phase 3: Games
        CommandHandler("quiz", quiz_cmd),
        CommandHandler("leaderboard", leaderboard_cmd),
        CommandHandler("guess_start", guess_start_cmd),
        CommandHandler("guess_stop", guess_stop_cmd),
        CommandHandler("wordle", wordle_cmd),
        CommandHandler("wordle_stop", wordle_stop_cmd),
        CommandHandler("poll", poll_cmd),
        CallbackQueryHandler(quiz_callback, pattern=r"^quiz_"),
        CallbackQueryHandler(poll_callback, pattern=r"^poll"),
        # Phase 4: Utility Tools
        CommandHandler("remind", remind_cmd),
        CommandHandler("tr", tr_cmd),
        CommandHandler("convert", convert_cmd),
        CommandHandler("calc", calc_cmd),
        # Phase 5: Menu callbacks
        CallbackQueryHandler(menu_callback, pattern=r"^menu_"),
        CallbackQueryHandler(menu_callback, pattern=r"^chat_"),
        CallbackQueryHandler(menu_callback, pattern=r"^game_"),
        CallbackQueryHandler(menu_callback, pattern=r"^anon_"),
        CallbackQueryHandler(menu_callback, pattern=r"^ch_"),
        CallbackQueryHandler(menu_callback, pattern=r"^tool_"),
        CallbackQueryHandler(menu_callback, pattern=r"^set_"),
        # Phase 7: Owner Control
        CommandHandler("owner", owner_cmd),
        CommandHandler("system", system_cmd),
        CommandHandler("broadcast", broadcast_cmd),
        CommandHandler("broadcast_all", broadcast_all_cmd),
        CommandHandler("admin_logs", admin_logs_cmd),
        # Phase 8: Force Join
        CommandHandler("forcejoin_on", forcejoin_on_cmd),
        CommandHandler("forcejoin_off", forcejoin_off_cmd),
        CommandHandler("forcejoin_status", forcejoin_status_cmd),
        CommandHandler("forcejoin_message", forcejoin_message_cmd),
        CallbackQueryHandler(forcejoin_verify_callback, pattern=r"^forcejoin_verify$"),
        # Phase 9: Personality
        CommandHandler("personality", personality_cmd),
        # Phase 10: Engagement
        CommandHandler("engagement_on", engagement_on_cmd),
        CommandHandler("engagement_off", engagement_off_cmd),
        CommandHandler("challenge", challenge_cmd),
        CommandHandler("joke", joke_cmd),
        CommandHandler("event", event_cmd),
        # Phase 11: Viral Content Engine
        CommandHandler("viral_now", viral_now_cmd),
        CommandHandler("viral_preview", viral_preview_cmd),
        CommandHandler("viral_stats", viral_stats_cmd),
        CommandHandler("viral_post", viral_post_cmd),
        # Phase 12: Advertisement System
        CommandHandler("ad_create", ad_create_cmd),
        CommandHandler("ad_list", ad_list_cmd),
        CommandHandler("ad_pause", ad_pause_cmd),
        CommandHandler("ad_resume", ad_resume_cmd),
        CommandHandler("ad_delete", ad_delete_cmd),
        CommandHandler("ad_stats", ad_stats_cmd),
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
