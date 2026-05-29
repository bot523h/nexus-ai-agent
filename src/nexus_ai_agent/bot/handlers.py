from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from sqlmodel import desc, select
from telegram import (
    Audio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    Voice,
)
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
# ── v2.0.0 imports ──
from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.analytics import AnalyticsEngine
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.channel_manager import ChannelManager
from nexus_ai_agent.features.engagement import EngagementEngine
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.features.games import NumberGuess, QuickPoll, QuizGame, WordleFA
from nexus_ai_agent.features.gamification import _ACHIEVEMENTS, GamificationEngine
from nexus_ai_agent.features.image_gen import ImageGenEngine
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.features.owner_control import OwnerControl, is_owner
from nexus_ai_agent.features.personality import PersonalityEngine
from nexus_ai_agent.features.referral import ReferralEngine
from nexus_ai_agent.features.speech import SpeechEngine
from nexus_ai_agent.features.summarizer import SummarizerEngine
from nexus_ai_agent.features.tools import Calculator, ReminderSystem, Translator, UnitConverter
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.models import (
    Chat,
    CloudFile,
    User,
    UserLanguage,
)
from nexus_ai_agent.storage.unified_cloud import UnifiedCloudStorage

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


async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: Any = None,
) -> None:
    message = _message(update)
    if message is not None:
        await message.reply_text(text, reply_markup=reply_markup)


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

    async def _get_user_lang(update: Update) -> str:
        """Retrieve stored language for the user, or auto-detect from Telegram."""
        uid = _user_id(update)
        if uid is not None:
            try:
                async with db_session_factory() as session:
                    from nexus_ai_agent.storage.models import UserLanguage

                    existing = (
                        await session.exec(select(UserLanguage).where(UserLanguage.user_id == uid))
                    ).first()
                    if existing is not None:
                        return existing.language
            except Exception:
                pass
        return i18n.detect_language(
            update.effective_user.language_code if update.effective_user else None
        )

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        user_id = _user_id(update)
        if user_id is not None:
            presence_store.mark_online(user_id)
        if update.effective_user:
            await _upsert_user(db_session_factory, update.effective_user)
        if update.effective_chat:
            await _upsert_chat(db_session_factory, _chat_id(update), f"tg:{_chat_id(update)}")

        # ── v2.1: Auto-detect user language ──
        user_lang = i18n.detect_language(
            update.effective_user.language_code if update.effective_user else None
        )
        is_new_user = False
        # Store detected language for this user
        if user_id is not None:
            try:
                async with db_session_factory() as session:
                    from sqlmodel import select as _sel

                    from nexus_ai_agent.storage.models import UserLanguage

                    existing = (
                        await session.exec(
                            _sel(UserLanguage).where(UserLanguage.user_id == user_id)
                        )
                    ).first()
                    if existing is None:
                        is_new_user = True
                        session.add(UserLanguage(user_id=user_id, language=user_lang))
                        await session.commit()
            except Exception:
                pass  # Non-critical: just skip if DB error

        # ── v2.1: Show onboarding for first-time users ──
        if is_new_user:
            try:
                from nexus_ai_agent.features.onboarding import send_onboarding

                await send_onboarding(update, context, user_lang)
                return
            except Exception:
                pass  # Fallback to normal menu if onboarding fails

        # ── v2.1: Main Menu with Inline Keyboard (i18n) ──
        keyboard = [
            [
                InlineKeyboardButton(i18n.t("menu.ai", lang=user_lang), callback_data="menu_ai"),
                InlineKeyboardButton(
                    i18n.t("menu.chat", lang=user_lang), callback_data="menu_chat"
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.image", lang=user_lang), callback_data="menu_image"
                ),
                InlineKeyboardButton(
                    i18n.t("menu.speech", lang=user_lang), callback_data="menu_speech"
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.cloud", lang=user_lang), callback_data="menu_cloud"
                ),
                InlineKeyboardButton(
                    i18n.t("menu.referral", lang=user_lang), callback_data="menu_referral"
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.games", lang=user_lang), callback_data="menu_games"
                ),
                InlineKeyboardButton(
                    i18n.t("menu.anon", lang=user_lang), callback_data="menu_anon"
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.tools", lang=user_lang), callback_data="menu_tools"
                ),
                InlineKeyboardButton(
                    i18n.t("menu.personality", lang=user_lang), callback_data="menu_personality"
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.gamification", lang=user_lang),
                    callback_data="menu_gamification",
                ),
                InlineKeyboardButton(
                    i18n.t("menu.analytics", lang=user_lang),
                    callback_data="menu_analytics",
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.moderation", lang=user_lang),
                    callback_data="menu_moderation",
                ),
                InlineKeyboardButton(
                    i18n.t("menu.language", lang=user_lang),
                    callback_data="menu_language",
                ),
            ],
            [
                InlineKeyboardButton(
                    i18n.t("menu.settings", lang=user_lang),
                    callback_data="menu_settings",
                ),
                InlineKeyboardButton(
                    i18n.t("menu.admin", lang=user_lang), callback_data="menu_admin"
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = _message(update)
        if msg is not None:
            welcome_text = i18n.t("start.welcome", lang=user_lang)
            await msg.reply_text(
                welcome_text,
                reply_markup=reply_markup,
            )

    async def online(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        presence_store.mark_online(user_id)
        context.application.bot_data.setdefault("heartbeat_user_ids", set()).add(user_id)
        lang = await _get_user_lang(update)
        await _reply(update, i18n.t("status.online", lang=lang))

    async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        presence_store.mark_offline(user_id)
        context.application.bot_data.setdefault("heartbeat_user_ids", set()).discard(user_id)
        lang = await _get_user_lang(update)
        await _reply(update, i18n.t("status.offline", lang=lang))

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
        lang = await _get_user_lang(update)
        help_text = (
            i18n.t("help.header", lang=lang)
            + i18n.t("help.ai", lang=lang)
            + "\n"
            + i18n.t("help.cloud", lang=lang)
            + "\n"
            + i18n.t("help.referral", lang=lang)
            + "\n"
            + i18n.t("help.settings", lang=lang)
        )
        await _reply(update, help_text)

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        lang = await _get_user_lang(update)
        user_id = _user_id(update)
        online_status = presence_store.is_online(user_id) if user_id is not None else False
        model_loaded = "yes" if settings.model_path and Path(settings.model_path).exists() else "no"

        # Gather engine status if available
        gemini_engine = context.application.bot_data.get("gemini_engine")
        engine_status = "not configured"
        rpm_info = ""
        daily_info = ""
        convos_info = ""
        if gemini_engine is not None:
            status_data = gemini_engine.get_status()
            engine_status = status_data.get("api_status", "unknown")
            rpm_info = str(status_data.get("rpm_remaining", "?"))
            daily_info = str(status_data.get("daily_remaining", "?"))
            convos_info = str(status_data.get("active_conversations", 0))

        status_text = i18n.t("ai.status", lang=lang).format(
            model=settings.gemini_model or "N/A",
            status=engine_status,
            rpm=rpm_info or "N/A",
            daily=daily_info or "N/A",
            convos=convos_info or "0",
        )
        status_text += (
            f"\n\n🌐 online: {online_status}"
            f"\n💾 model loaded: {model_loaded}"
            f"\n📁 db: {settings.db_path}"
        )
        await _reply(update, status_text)

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

    # ── v2.1: Feature engines (initialized in app.py, fallback here) ──
    from nexus_ai_agent.features.conversation_store import ConversationStore
    from nexus_ai_agent.features.request_queue import GeminiRequestQueue

    conv_store = ConversationStore(db_path=settings.db_path)
    request_queue = GeminiRequestQueue(
        max_rpm=settings.gemini_max_rpm,
        max_daily=settings.gemini_max_daily,
    )
    gemini_engine: GeminiEngine | None = None
    if settings.gemini_api_key:
        gemini_engine = GeminiEngine(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_rpm=settings.gemini_max_rpm,
            max_daily=settings.gemini_max_daily,
            conversation_store=conv_store,
            request_queue=request_queue,
        )
    image_engine = ImageGenEngine()
    speech_engine = SpeechEngine(output_dir="data/audio")
    summarizer_engine: SummarizerEngine | None = None
    if settings.gemini_api_key:
        summarizer_engine = SummarizerEngine(
            gemini_api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    referral_engine = ReferralEngine(db_path=settings.db_path)
    unified_cloud = UnifiedCloudStorage(
        dropbox_token=settings.dropbox_token,
        pcloud_token=settings.pcloud_token,
        internxt_token=settings.internxt_token,
    )

    # ── v2.1: i18n for handler strings ──
    from nexus_ai_agent.i18n import i18n

    # ── v2.0.0: /ai — AI Chat with Gemini ──
    async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI is not configured. Set GEMINI_API_KEY.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        if not rate_limiter.is_allowed(user_id):
            await _reply(update, "⏳ Rate limit reached.")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "🤖 Gemini AI Chat\n\nUsage: /ai <message>\n"
                "Other: /ask /code /translate /vision /summarize",
            )
            return
        conv_id = f"tg:{user_id}"
        result = await gemini_engine.chat(text, conv_id=conv_id, user_id=user_id)
        await _reply(update, f"🤖 {result}" if not result.startswith("❌") else result)

    # ── v2.0.0: /ask — One-shot AI question ──
    async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ Usage: /ask <question>")
            return
        result = await gemini_engine.ask(text, user_id=user_id)
        await _reply(update, f"💡 {result}")

    # ── v2.0.0: /code — AI Code Generation ──
    async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "❌ Usage: /code <description>\nExample: /code Python fibonacci function",
            )
            return
        result = await gemini_engine.code(text, user_id=user_id)
        await _reply(update, f"💻 {result}")

    # ── v2.0.0: /translate — AI-powered translation ──
    async def ai_translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "❌ Usage: /translate <text>\nSpecific target: /translate ja Hello world",
            )
            return
        target_lang = "fa"
        source_text = text
        if ":" in text and text.split(":")[0].isalpha():
            parts = text.split(":", 1)
            if len(parts) == 2 and len(parts[0]) <= 3:
                target_lang = parts[0]
                source_text = parts[1].strip()
        result = await gemini_engine.translate(
            source_text, target_lang=target_lang, user_id=user_id
        )
        await _reply(update, f"🌐 {result}")

    # ── v2.0.0: /vision — Image analysis via Gemini Vision ──
    async def vision_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        msg = _message(update)
        photo = None
        prompt = " ".join(context.args) if context.args else "Describe this image in detail."
        if msg and msg.reply_to_message and msg.reply_to_message.photo:
            photo = msg.reply_to_message.photo[-1]
        elif msg and msg.photo:
            photo = msg.photo[-1]
        if photo is None:
            await _reply(
                update,
                "❌ Reply to a photo with /vision\nExample: Reply to photo → /vision What is this?",
            )
            return
        try:
            file = await photo.get_file()
            image_bytes = await file.download_as_bytearray()

            result = await gemini_engine.vision(
                bytes(image_bytes),
                question=prompt,
                user_id=user_id,
                mime_type="image/jpeg",
            )
            await _reply(update, f"🤖 {result}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ Vision error: {exc}")

    # ── v2.0.0: /image — AI Image Generation via Pollinations.ai ──
    async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "🎨 Image Generation\n\n"
                "Usage: /image <description>\n"
                "With style: /image style:anime a cat samurai\n"
                "Styles: realistic, anime, digital, oil, "
                "watercolor, pixel, 3d, comic, minimal, fantasy",
            )
            return
        style = "realistic"
        prompt = text
        if text.lower().startswith("style:"):
            parts = text.split(None, 1)
            if len(parts) >= 2:
                style = parts[0].split(":")[1].lower()
                prompt = parts[1]
        result = await image_engine.generate(prompt, style=style, user_id=user_id)
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("file_path"):
            msg = _message(update)
            if msg is not None:
                await msg.reply_photo(
                    photo=open(result["file_path"], "rb"),
                    caption=f"🎨 {prompt[:100]}",
                )
        else:
            await _reply(update, "❌ Image generation failed.")

    # ── v2.0.0: /tts — Text to Speech ──
    async def tts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "🔊 Text to Speech\n\n"
                "Usage: /tts <text>\n"
                "With language: /tts en Hello world\n"
                "Languages: en, fa, ar, es, fr, de, ru, zh, "
                "ja, ko, pt, hi, tr, id, it",
            )
            return
        lang = "en"
        tts_text = text
        if context.args and len(context.args[0]) == 2 and context.args[0].isalpha():
            lang = context.args[0].lower()
            tts_text = " ".join(context.args[1:])
        if not tts_text:
            await _reply(update, "❌ Provide text after language code.")
            return
        result = await speech_engine.text_to_speech(tts_text, lang=lang)
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("file_path"):
            msg = _message(update)
            if msg is not None:
                await msg.reply_voice(voice=open(result["file_path"], "rb"))
        else:
            await _reply(update, "❌ TTS failed.")

    # ── v2.0.0: /stt — Speech to Text ──
    async def stt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        msg = _message(update)
        voice: Voice | Audio | None = None
        if msg and msg.reply_to_message and msg.reply_to_message.voice:
            voice = msg.reply_to_message.voice
        elif msg and msg.reply_to_message and msg.reply_to_message.audio:
            voice = msg.reply_to_message.audio
        elif msg and msg.voice:
            voice = msg.voice
        if voice is None:
            await _reply(update, "❌ Reply to a voice message with /stt")
            return
        try:
            file = await voice.get_file()
            audio_bytes = await file.download_as_bytearray()

            # Save audio to temp file for STT
            import tempfile

            suffix = ".ogg"
            if voice.mime_type:
                mime_ext = {
                    "audio/ogg": ".ogg",
                    "audio/mpeg": ".mp3",
                    "audio/mp4": ".m4a",
                    "audio/wav": ".wav",
                    "audio/webm": ".webm",
                }
                suffix = mime_ext.get(voice.mime_type, suffix)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(bytes(audio_bytes))
                tmp_path = tmp.name
            result = await speech_engine.speech_to_text(
                tmp_path,
                lang="fa",
                gemini_engine=gemini_engine,
            )
            import os

            os.unlink(tmp_path)
            if result.get("error"):
                await _reply(update, f"❌ {result['error']}")
            else:
                await _reply(update, f"🎤 Transcription:\n\n{result.get('text', '')}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ STT error: {exc}")

    # ── v2.0.0: /summarize — Smart Summarizer ──
    async def summarize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if summarizer_engine is None:
            await _reply(update, "❌ Summarizer not configured (requires GEMINI_API_KEY).")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "📝 Smart Summarizer\n\n"
                "Usage: /summarize <text or URL>\n"
                "Modes: /summarize mode:detailed <text>\n"
                "Modes: brief, detailed, key_points, eli5, academic",
            )
            return
        mode = "brief"
        content_text = text
        if text.lower().startswith("mode:"):
            parts = text.split(None, 1)
            if len(parts) >= 2:
                mode = parts[0].split(":")[1].lower()
                content_text = parts[1]
        if content_text.startswith("http://") or content_text.startswith("https://"):
            result = await summarizer_engine.summarize_url(content_text, mode=mode)
        else:
            result = await summarizer_engine.summarize_text(content_text, mode=mode)
        await _reply(update, SummarizerEngine.format_result(result))

    # ── v2.0.0: /cloud — Upload file to unified cloud ──
    async def cloud_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        msg = _message(update)
        doc = None
        if msg and msg.reply_to_message and msg.reply_to_message.document:
            doc = msg.reply_to_message.document
        elif msg and msg.document:
            doc = msg.document
        if doc is None:
            await _reply(
                update,
                "☁️ Unified Cloud Storage\n\n"
                "Upload: Reply to file → /cloud\n"
                "Files: /myfiles\n"
                "Download: /download <name>\n"
                "Status: /cloud_status",
            )
            return
        try:
            file = await doc.get_file()
            file_bytes = await file.download_as_bytearray()
            # Save to temp file for cloud upload
            from pathlib import Path as _Path

            tmp_dir = _Path("data/cloud_tmp")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / (doc.file_name or "unnamed")
            tmp_path.write_bytes(bytes(file_bytes))
            result = await unified_cloud.upload_file(
                tmp_path,
                remote_key=doc.file_name or "unnamed",
            )
            tmp_path.unlink(missing_ok=True)
            if result.get("success"):
                async with db_session_factory() as session:
                    cloud_file = CloudFile(
                        user_id=user_id,
                        file_name=doc.file_name or "unnamed",
                        provider=result.get("provider", "unknown"),
                        remote_path=result.get("remote_path", ""),
                        file_size=len(file_bytes),
                    )
                    session.add(cloud_file)
                    await session.commit()
                await _reply(
                    update,
                    f"☁️ Uploaded!\n📁 {doc.file_name}\n"
                    f"📦 {result.get('provider', 'N/A')}\n"
                    f"📊 {len(file_bytes) / 1024:.1f}KB",
                )
            else:
                await _reply(update, f"❌ Upload failed: {result.get('error', 'Unknown')}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ Cloud error: {exc}")

    # ── v2.0.0: /myfiles — List my cloud files ──
    async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        async with db_session_factory() as session:
            stmt = (
                select(CloudFile)
                .where(CloudFile.user_id == user_id)
                .order_by(desc(CloudFile.created_at))
            )
            files = (await session.exec(stmt)).all()
        if not files:
            await _reply(update, "📁 No files. Reply to file → /cloud to upload.")
            return
        lines = [f"📁 Your Cloud Files ({len(files)}):", "━" * 25]
        for f in files[:20]:
            lines.append(f"📄 {f.file_name} ({f.file_size / 1024:.1f}KB) [{f.provider}]")
        if len(files) > 20:
            lines.append(f"... and {len(files) - 20} more")
        await _reply(update, "\n".join(lines))

    # ── v2.0.0: /download — Download from cloud ──
    async def download_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        filename = " ".join(context.args) if context.args else ""
        if not filename:
            await _reply(update, "❌ Usage: /download <filename>")
            return
        async with db_session_factory() as session:
            stmt = select(CloudFile).where(
                CloudFile.user_id == user_id, CloudFile.file_name == filename
            )
            cloud_file = (await session.exec(stmt)).first()
        if cloud_file is None:
            await _reply(update, f"❌ File '{filename}' not found.")
            return
        from pathlib import Path as _Path2

        dl_dir = _Path2("data/cloud_downloads")
        dl_dir.mkdir(parents=True, exist_ok=True)
        local_path = dl_dir / filename
        result = await unified_cloud.download_file(
            cloud_file.remote_path or filename,
            local_path,
        )
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("data"):
            import io

            msg = _message(update)
            if msg is not None:
                await msg.reply_document(
                    document=io.BytesIO(result["data"]),
                    filename=filename,
                )
        elif local_path.exists():
            msg = _message(update)
            if msg is not None:
                await msg.reply_document(
                    document=open(local_path, "rb"),
                    filename=filename,
                )
            local_path.unlink(missing_ok=True)
        else:
            await _reply(update, "❌ Download failed.")

    # ── v2.0.0: /cloud_status — Cloud storage status ──
    async def cloud_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = await unified_cloud.get_status()
        await _reply(update, f"☁️ Cloud Storage Status\n\n{status}")

    # ── v2.0.0: /referral — Referral system ──
    async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        formatted = referral_engine.format_stats(
            user_id,
            settings.bot_username,
        )
        await _reply(update, formatted)

    # ── v2.0.0: /referral_board — Referral leaderboard ──
    async def referral_board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        formatted = referral_engine.format_leaderboard()
        await _reply(update, formatted)

    # ── v2.0.0: /language — Set language preference ──
    async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        lang = " ".join(context.args) if context.args else ""
        if not lang:
            from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES

            keyboard = []
            row = []
            for code, name in list(SUPPORTED_LANGUAGES.items())[:15]:
                row.append(InlineKeyboardButton(name, callback_data=f"lang_{code}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            async with db_session_factory() as session:
                stmt = select(UserLanguage).where(UserLanguage.user_id == user_id)
                ul = (await session.exec(stmt)).first()
            current = ul.language if ul else "en"
            await _reply(
                update,
                f"🌐 Language\n\nCurrent: {SUPPORTED_LANGUAGES.get(current, current)}\n\nSelect:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        lang = lang.lower()
        from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES

        if lang not in SUPPORTED_LANGUAGES:
            supported = ", ".join(SUPPORTED_LANGUAGES.keys())
            await _reply(
                update,
                f"❌ '{lang}' not supported.\nSupported: {supported}",
            )
            return
        async with db_session_factory() as session:
            stmt = select(UserLanguage).where(UserLanguage.user_id == user_id)
            ul = (await session.exec(stmt)).first()
            if ul:
                ul.language = lang
                from datetime import datetime as _dt

                ul.updated_at = _dt.utcnow()
                await session.commit()
            else:
                ul = UserLanguage(user_id=user_id, language=lang)
                session.add(ul)
                await session.commit()
        await _reply(update, f"✅ Language: {SUPPORTED_LANGUAGES[lang]}")

    # ── v2.0.0: Handle /start ref_XXX for referral tracking ──
    async def start_referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            return
        arg = context.args[0]
        if not arg.startswith("ref_"):
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        # process_referral expects (referee_id, start_param_with_ref_prefix)
        result = referral_engine.process_referral(user_id, arg)
        if result.get("success"):
            # Award +50 XP to both referrer and referee
            try:
                chat_id = _chat_id(update)
                from nexus_ai_agent.features.gamification import GamificationEngine

                GamificationEngine.add_xp(result["referrer_id"], chat_id, 50)
                GamificationEngine.add_xp(user_id, chat_id, 50)
            except Exception:
                pass  # Non-critical: XP award is best-effort
            lang = await _get_user_lang(update)
            await _reply(
                update,
                i18n.t("referral.welcome", lang=lang)
                + "\n\n🎉 Both you and your friend received +50 XP!",
            )

    # ── v2.1: /newchat — Clear conversation and start fresh ──
    async def newchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        conv_id = f"tg:{user_id}"
        if gemini_engine is not None:
            gemini_engine.clear_history(conv_id)
        await _reply(
            update,
            i18n.t(
                "chat.new_session",
                lang=i18n.detect_language(
                    update.effective_user.language_code if update.effective_user else None
                ),
            ),
        )

    # ── v3.0.0: Knowledge Commands ─────────────────────────────────────

    async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Learn about a topic from all sources (Wikipedia + Web + Gemini)."""
        query = " ".join(context.args) if context.args else ""
        if not query:
            await _reply(update, "📚 استفاده: /learn [موضوع]\nمثال: /learn هوش مصنوعی")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.knowledge.knowledge_manager import KnowledgeManager

        km = KnowledgeManager(
            wiki_cache_path=settings.wiki_cache_path,
            knowledge_cache_path=settings.knowledge_cache_path,
            gemini_api_key=settings.gemini_api_key,
        )
        try:
            result = await km.learn(query)
            sources_text = ""
            for s in result.get("sources", []):
                src_name = s.get("source", "")
                title = s.get("title", "")
                url = s.get("url", "")
                if url:
                    sources_text += f"\n🔹 [{title}]({url}) ({src_name})"
                else:
                    sources_text += f"\n🔹 {title} ({src_name})"
            cache_note = " (از کش)" if result.get("from_cache") else ""
            text = f"📚 **یادگیری: {query}**{cache_note}\n\n{result['summary']}"
            if sources_text:
                text += f"\n\n**منابع:**{sources_text}"
            await _reply(update, text)
        except Exception as exc:
            logger.error("learn_cmd_error", error=str(exc))
            await _reply(update, f"❌ خطا در یادگیری: {exc}")
        finally:
            await km.close()

    async def wiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch from Wikipedia (fa + en)."""
        query = " ".join(context.args) if context.args else ""
        if not query:
            await _reply(update, "📖 استفاده: /wiki [موضوع]\nمثال: /wiki Artificial Intelligence")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.knowledge.wikipedia_trainer import WikipediaTrainer

        wt = WikipediaTrainer(cache_path=settings.wiki_cache_path)
        try:
            result = await wt.fetch_both(query)
            parts: list[str] = []
            for lang_key in ("fa", "en"):
                data = result.get(lang_key, {})
                if data.get("summary"):
                    url = data.get("url", "")
                    title = data.get("title", query)
                    if url:
                        parts.append(
                            f"📖 **Wikipedia {lang_key.upper()}**: "
                            f"[{title}]({url})\n{data['summary']}"
                        )
                    else:
                        parts.append(
                            f"📖 **Wikipedia {lang_key.upper()}**: {title}\n{data['summary']}"
                        )
                else:
                    parts.append(f"📖 **Wikipedia {lang_key.upper()}**: نتیجه‌ای یافت نشد.")
            await _reply(update, "\n\n---\n\n".join(parts))
        except Exception as exc:
            logger.error("wiki_cmd_error", error=str(exc))
            await _reply(update, f"❌ خطا در ویکیپدیا: {exc}")

    async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Search the web via DuckDuckGo."""
        query = " ".join(context.args) if context.args else ""
        if not query:
            await _reply(update, "🔍 استفاده: /search [متن]\nمثال: /search بهترین زبان برنامه‌نویسی")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.knowledge.web_trainer import WebTrainer

        wt = WebTrainer()
        try:
            results = await wt.search(query, max_results=5)
            if not results:
                await _reply(update, "🔍 نتیجه‌ای یافت نشد.")
                return
            text = f"🔍 **نتایج جستجو: {query}**\n\n"
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                url = r.get("url", "")
                if url:
                    text += f"{i}. [{title}]({url})\n{snippet}\n\n"
                else:
                    text += f"{i}. **{title}**\n{snippet}\n\n"
            await _reply(update, text)
        except Exception as exc:
            logger.error("search_cmd_error", error=str(exc))
            await _reply(update, f"❌ خطا در جستجو: {exc}")
        finally:
            await wt.close()

    # ── v3.0.0: Free Tools Commands ────────────────────────────────────

    async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch weather for a city using wttr.in (free)."""
        city = " ".join(context.args) if context.args else ""
        if not city:
            await _reply(update, "🌤 استفاده: /weather [شهر]\nمثال: /weather تهران")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import WeatherTool

        wt = WeatherTool()
        data = await wt.get_weather(city)
        await _reply(update, wt.format_weather(data))

    async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show USD to IRR/Toman rate."""
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import CurrencyTool

        ct = CurrencyTool()
        data = await ct.get_usd_to_irr()
        await _reply(update, ct.format_rate(data))

    async def convert_cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Convert currency: /convert 100 usd irr."""
        args = context.args or []
        if len(args) < 3:
            await _reply(
                update,
                "💱 استفاده: /convert [مقدار] [از] [به]\nمثال: /convert 100 usd irr",
            )
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import CurrencyTool

        ct = CurrencyTool()
        try:
            amount = float(args[0])
        except ValueError:
            await _reply(update, "❌ مقدار باید عدد باشد.")
            return
        from_curr = args[1].upper()
        to_curr = args[2].upper()
        data = await ct.convert(amount, from_curr, to_curr)
        await _reply(update, ct.format_convert(data))

    async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch news for a topic."""
        query = " ".join(context.args) if context.args else ""
        if not query:
            await _reply(update, "📰 استفاده: /news [موضوع]\nمثال: /news هوش مصنوعی")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import NewsTool

        nt = NewsTool(news_api_key=settings.news_api_key)
        results = await nt.get_news(query, max_results=5)
        await _reply(update, nt.format_news(results, query))

    async def yt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Get YouTube video info from a URL."""
        url = " ".join(context.args) if context.args else ""
        if not url:
            await _reply(update, "🎬 استفاده: /yt [لینک یوتیوب]")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import YouTubeSearchTool

        yt = YouTubeSearchTool()
        data = await yt.get_video_info(url)
        await _reply(update, yt.format_video_info(data))

    async def youtube_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Search YouTube for a topic."""
        query = " ".join(context.args) if context.args else ""
        if not query:
            await _reply(update, "🎬 استفاده: /youtube [موضوع]\nمثال: /youtube Python tutorial")
            return
        await update.message.chat.send_action("typing")  # type: ignore[union-attr]
        from nexus_ai_agent.integrations.free_tools import YouTubeSearchTool

        yt = YouTubeSearchTool()
        results = await yt.search(query, max_results=5)
        await _reply(update, yt.format_search_results(results, query))

    async def onboarding_callback_handler(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle onboarding inline button presses."""
        from nexus_ai_agent.features.onboarding import handle_onboarding_callback

        lang = await _get_user_lang(update)
        await handle_onboarding_callback(update, context, lang)

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

        elif data == "menu_personality":
            keyboard = [
                [InlineKeyboardButton("📋 لیست شخصیت‌ها", callback_data="pers_list")],
                [InlineKeyboardButton("🔍 شخصیت فعلی", callback_data="pers_current")],
                [InlineKeyboardButton("🎭 تغییر شخصیت", callback_data="pers_set")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🎭 شخصیت‌های AI\n\nشخصیت ربات رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "pers_list":
            await query.edit_message_text(PersonalityEngine.list_personalities())

        elif data == "pers_current":
            chat_id = _chat_id(update)
            await query.edit_message_text(PersonalityEngine.current_personality(chat_id))

        elif data == "pers_set":
            await query.edit_message_text(
                "🎭 تغییر شخصیت\n\n/personality set <name>\n\nمثال:\n/personality set friendly\n"
                "/personality list — لیست شخصیت‌ها"
            )

        elif data == "menu_gamification":
            keyboard = [
                [InlineKeyboardButton("👤 پروفایل من", callback_data="gam_profile")],
                [InlineKeyboardButton("🎁 پاداش روزانه", callback_data="gam_daily")],
                [InlineKeyboardButton("🏆 جدول امتیازات", callback_data="gam_leaderboard")],
                [InlineKeyboardButton("🏅 دستاوردها", callback_data="gam_achievements")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🏆 گیمیفیکیشن\n\nXP کسب کن، سطح‌بندرو برو بالا!",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "gam_profile":
            user_id = query.from_user.id if query.from_user else 0
            chat_id = _chat_id(update)
            if user_id:
                profile = GamificationEngine.get_profile(user_id, chat_id)
                ach_t = GamificationEngine.format_achievements(profile["achievements"])
                await query.edit_message_text(
                    f"👤 پروفایل شما\n━━━━━━━━━━━━━━━━\n"
                    f"⭐ سطح {profile['level']}: {profile['title']}\n"
                    f"✨ XP: {profile['xp']}\n"
                    f"📊 تا سطح بعد: {profile['xp_to_next']} XP\n"
                    f"🔥 استریک: {profile['streak']} روز\n"
                    f"🏆 دستاوردها ({profile['achievement_count']}):\n{ach_t}"
                )
            else:
                await query.edit_message_text("❌ خطا: کاربر شناسایی نشد.")

        elif data == "gam_daily":
            await query.edit_message_text("🎁 پاداش روزانه\n\n/daily — دریافت پاداش روزانه")

        elif data == "gam_leaderboard":
            await query.edit_message_text("🏆 جدول امتیازات\n\n/xp_leaderboard")

        elif data == "gam_achievements":
            await query.edit_message_text("🏅 دستاوردها\n\n/achievements")

        elif data == "menu_analytics":
            keyboard = [
                [InlineKeyboardButton("📊 داشبورد", callback_data="an_dashboard")],
                [InlineKeyboardButton("👥 کاربران فعال", callback_data="an_active")],
                [InlineKeyboardButton("📈 بازگشت کاربران", callback_data="an_retention")],
                [InlineKeyboardButton("⚡ دستورات پرکاربرد", callback_data="an_commands")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "📊 تحلیل و آمار\n\nداده‌های جامعه رو ببین:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "an_dashboard":
            if not is_owner(query.from_user.id if query.from_user else 0):
                await query.edit_message_text("⛔ فقط مدیر")
                return
            chat_id = _chat_id(update)
            dashboard = AnalyticsEngine.get_dashboard(chat_id)
            eng = dashboard["engagement_24h"]
            peak_t = (
                ", ".join(f"{p['label']} ({p['count']})" for p in dashboard["peak_hours_top3"])
                or "ندارد"
            )
            await query.edit_message_text(
                f"📊 داشبورد تحلیلی\n━━━━━━━━━━━━━━━━\n"
                f"👤 فعال ۲۴ساعت: {dashboard['active_users_24h']}\n"
                f"👤 فعال ۷روز: {dashboard['active_users_7d']}\n"
                f"📈 رویداد ۲۴ساعت: {eng['total_events']}\n"
                f"📊 رویداد/کاربر: {eng['events_per_user']}\n"
                f"🕐 ساعات اوج: {peak_t}"
            )

        elif data == "an_active":
            await query.edit_message_text("👥 کاربران فعال\n\n/analytics_active [ساعت]")

        elif data == "an_retention":
            await query.edit_message_text("📈 بازگشت کاربران\n\n/analytics_retention [روز]")

        elif data == "an_commands":
            if not is_owner(query.from_user.id if query.from_user else 0):
                await query.edit_message_text("⛔ فقط مدیر")
                return
            chat_id = _chat_id(update)
            cmds = AnalyticsEngine.get_command_usage(chat_id)
            if not cmds:
                await query.edit_message_text("⚡ هنوز داده‌ای ثبت نشده.")
                return
            lines = ["⚡ دستورات پرکاربرد\n━━━━━━━━━━━━━━━━"]
            for c in cmds[:5]:
                lines.append(f"  /{c['command']} — {c['count']} بار")
            await query.edit_message_text("\n".join(lines))

        elif data == "menu_moderation":
            keyboard = [
                [InlineKeyboardButton("🟢 فعال‌سازی", callback_data="mod_on")],
                [InlineKeyboardButton("🔴 غیرفعال", callback_data="mod_off")],
                [InlineKeyboardButton("⚙️ تنظیمات نظارت", callback_data="mod_cfg")],
                [InlineKeyboardButton("👤 اعتبار کاربر", callback_data="mod_rep")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🛡️ نظارت هوشمند\n\nمحتوای گروه رو مدیریت کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "mod_on":
            await query.edit_message_text("🟢 فعال‌سازی نظارت\n\n/mod_on")

        elif data == "mod_off":
            await query.edit_message_text("🔴 غیرفعال‌سازی نظارت\n\n/mod_off")

        elif data == "mod_cfg":
            chat_id = _chat_id(update)
            cfg = ModerationEngine.get_config(chat_id)
            if cfg is None:
                await query.edit_message_text("🛡️ نظارت: غیرفعال (تنظیم نشده)")
                return
            si = lambda v: "✅" if v else "❌"  # noqa: E731
            await query.edit_message_text(
                f"🛡️ تنظیمات نظارت\n━━━━━━━━━━━━━━━━\n"
                f"آنتی‌اسپم: {si(cfg.anti_spam)}\n"
                f"آنتی‌فلاد: {si(cfg.anti_flood)}\n"
                f"فیلتر لینک: {si(cfg.link_filter)}\n"
                f"فیلتر کلمات: {si(cfg.profanity_filter)}\n"
                f"حداکثر هشدار: {cfg.max_warnings}\n"
                f"مدت میوت: {cfg.mute_duration_minutes} دقیقه"
            )

        elif data == "mod_rep":
            await query.edit_message_text("👤 اعتبار کاربر\n\n/reputation [user_id]")

        elif data == "menu_admin":
            if not is_owner(query.from_user.id if query.from_user else 0):
                await query.edit_message_text("⛔ فقط مدیر")
                return
            keyboard = [
                [InlineKeyboardButton("👑 مدیریت مالک", callback_data="adm_owner")],
                [InlineKeyboardButton("🔥 موتور وایرال", callback_data="adm_viral")],
                [InlineKeyboardButton("📢 تبلیغات", callback_data="adm_ads")],
                [InlineKeyboardButton("🛡️ نظارت", callback_data="adm_mod")],
                [InlineKeyboardButton("📊 تحلیل‌ها", callback_data="adm_analytics")],
                [InlineKeyboardButton("📢 عضویت اجباری", callback_data="adm_forcejoin")],
                [InlineKeyboardButton("💬 تعامل خودکار", callback_data="adm_engagement")],
                [InlineKeyboardButton("🖥️ وضعیت سیستم", callback_data="adm_system")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "👨‍💼 پنل مدیریت\n\nابزارهای مدیریتی:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "adm_owner":
            await query.edit_message_text(
                "👑 مدیریت مالک\n━━━━━━━━━━━━━━━━\n"
                "/owner — داشبورد مالک\n"
                "/system — وضعیت سیستم\n"
                "/broadcast <text> — پیام همگانی\n"
                "/admin_logs — لاگ‌های ادمین"
            )

        elif data == "adm_viral":
            await query.edit_message_text(
                "🔥 موتور وایرال\n━━━━━━━━━━━━━━━━\n"
                "/viral_now — تولید و ارسال\n"
                "/viral_preview — پیش‌نمایش\n"
                "/viral_stats — آمار\n"
                "/viral_post — پست‌های در انتظار"
            )

        elif data == "adm_ads":
            await query.edit_message_text(
                "📢 تبلیغات\n━━━━━━━━━━━━━━━━\n"
                "/ad_create <ساعت> <متن> — ساخت\n"
                "/ad_list — لیست کمپین‌ها\n"
                "/ad_pause <id> — توقف\n"
                "/ad_resume <id> — ادامه\n"
                "/ad_delete <id> — حذف\n"
                "/ad_stats — آمار"
            )

        elif data == "adm_mod":
            await query.edit_message_text(
                "🛡️ نظارت\n━━━━━━━━━━━━━━━━\n"
                "/mod_on — فعال‌سازی\n"
                "/mod_off — غیرفعال\n"
                "/mod_config — تنظیمات\n"
                "/warn <user_id> — هشدار\n"
                "/mute <user_id> [دقیقه] — میوت\n"
                "/unmute <user_id> — آنمیوت\n"
                "/reputation [user_id] — اعتبار"
            )

        elif data == "adm_analytics":
            await query.edit_message_text(
                "📊 تحلیل‌ها\n━━━━━━━━━━━━━━━━\n"
                "/analytics — داشبورد\n"
                "/analytics_active [ساعت] — کاربران فعال\n"
                "/analytics_retention [روز] — بازگشت\n"
                "/track <نوع> — ثبت رویداد"
            )

        elif data == "adm_forcejoin":
            await query.edit_message_text(
                "📢 عضویت اجباری\n━━━━━━━━━━━━━━━━\n"
                "/forcejoin_on — فعال‌سازی\n"
                "/forcejoin_off — غیرفعال\n"
                "/forcejoin_status — وضعیت\n"
                "/forcejoin_message <text> — پیام سفارشی"
            )

        elif data == "adm_engagement":
            await query.edit_message_text(
                "💬 تعامل خودکار\n━━━━━━━━━━━━━━━━\n"
                "/engagement_on [دقیقه] — فعال‌سازی\n"
                "/engagement_off — غیرفعال\n"
                "/challenge — چالش تصادفی\n"
                "/joke — جوک تصادفی\n"
                "/event — رویداد تصادفی"
            )

        elif data == "adm_system":
            status_text = OwnerControl.system_status()
            await query.edit_message_text(f"🖥️ وضعیت سیستم\n━━━━━━━━━━━━━━━━\n{status_text}")

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
                "ℹ️ راهنمای NEXUS AI v2.0.0\n\n"
                "💬 چت: فقط پیام بفرست\n"
                "🎮 بازی‌ها: /quiz /guess_start /wordle /poll\n"
                "👤 ناشناس: /anon_start /anon_stop /anon_report\n"
                "📢 کانال: /post /schedule /ban /unban /stats /welcome /pin\n"
                "🛠 ابزارها: /remind /tr /convert /calc\n"
                "🎭 شخصیت: /personality list|current|set\n"
                "🏆 گیمیفیکیشن: /profile /daily /xp_leaderboard /achievements\n"
                "🛡️ نظارت: /mod_on /mod_off /mod_config\n"
                "⚙️ تنظیمات: /online /disconnect /status /help"
            )

        # ── v2.0.0: AI Menu ──
        elif data == "menu_ai":
            keyboard = [
                [InlineKeyboardButton("💬 چت با AI", callback_data="ai_chat")],
                [InlineKeyboardButton("❓ سوال بپرس", callback_data="ai_ask")],
                [InlineKeyboardButton("💻 کدنویسی", callback_data="ai_code")],
                [InlineKeyboardButton("🌍 ترجمه هوشمند", callback_data="ai_translate")],
                [InlineKeyboardButton("👁️ تحلیل تصویر", callback_data="ai_vision")],
                [InlineKeyboardButton("📝 خلاصه‌سازی", callback_data="ai_summarize")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🤖 هوش مصنوعی\n\nقابلیت‌های AI رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "ai_chat":
            await query.edit_message_text(
                "💬 چت با AI\n\nاز /ai استفاده کن و پیامت رو بنویس:\nمثال: /ai سلام، چطوری؟"
            )

        elif data == "ai_ask":
            await query.edit_message_text(
                "❓ سوال بپرس\n\nاز /ask استفاده کن:\nمثال: /ask چرا آسمان آبی است؟"
            )

        elif data == "ai_code":
            await query.edit_message_text(
                "💻 کدنویسی\n\nاز /code استفاده کن:\nمثال: /code پایتون فاکتوریل بنویس"
            )

        elif data == "ai_translate":
            await query.edit_message_text(
                "🌍 ترجمه هوشمند\n\nاز /translate استفاده کن:\n"
                "مثال: /translate Hello, how are you? fa"
            )

        elif data == "ai_vision":
            await query.edit_message_text("👁️ تحلیل تصویر\n\nیک عکس بفرست و /vision رو ریپلی کن")

        elif data == "ai_summarize":
            await query.edit_message_text(
                "📝 خلاصه‌سازی\n\nاز /summarize استفاده کن:\n"
                "مثال: /summarize متن طولانی...\n"
                "مدل‌ها: brief, detailed, key_points, eli5, academic"
            )

        # ── v2.0.0: Image Gen Menu ──
        elif data == "menu_image":
            keyboard = [
                [InlineKeyboardButton("🎨 ساخت تصویر", callback_data="img_create")],
                [InlineKeyboardButton("📋 استایل‌ها", callback_data="img_styles")],
                [InlineKeyboardButton("📐 اندازه‌ها", callback_data="img_sizes")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🎨 تصویرسازی AI\n\nاز /image استفاده کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "img_create":
            await query.edit_message_text(
                "🎨 ساخت تصویر\n\nاز /image استفاده کن:\n"
                "مثال: /image a cat in space --style anime\n"
                "استایل‌ها: realistic, anime, digital-art, oil-painting,\n"
                "pixel-art, watercolor, cyberpunk, fantasy, 3d, sketch"
            )

        elif data == "img_styles":
            await query.edit_message_text(
                "📋 استایل‌های موجود:\n\n"
                "• realistic — واقع‌گرایانه\n"
                "• anime — انیمه‌ای\n"
                "• digital-art — هنر دیجیتال\n"
                "• oil-painting — نقاشی رنگ روغن\n"
                "• pixel-art — پیکسل آرت\n"
                "• watercolor — آبرنگ\n"
                "• cyberpunk — سایبرپانک\n"
                "• fantasy — فانتزی\n"
                "• 3d — سه‌بعدی\n"
                "• sketch — طرح‌نهایی\n\n"
                "استفاده: /image توصیف --style استایل"
            )

        elif data == "img_sizes":
            await query.edit_message_text(
                "📐 اندازه‌های موجود:\n\n"
                "• 1024x1024 — مربع (پیش‌فرض)\n"
                "• 1792x1024 — افقی\n"
                "• 1024x1792 — عمودی\n\n"
                "استفاده: /image توصیف --size 1792x1024"
            )

        # ── v2.0.0: Speech Menu ──
        elif data == "menu_speech":
            keyboard = [
                [InlineKeyboardButton("🔊 متن به صدا", callback_data="speech_tts")],
                [InlineKeyboardButton("🎤 صدا به متن", callback_data="speech_stt")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🔊 صدا\n\nقابلیت‌های صوتی:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "speech_tts":
            await query.edit_message_text(
                "🔊 متن به صدا\n\nاز /tts استفاده کن:\n"
                "مثال: /tts سلام دنیا fa\n"
                "کد زبان: en, fa, ar, fr, de, es, ru, zh, ja, ..."
            )

        elif data == "speech_stt":
            await query.edit_message_text("🎤 صدا به متن\n\nیک پیام صوتی بفرست و /stt رو ریپلی کن")

        # ── v2.0.0: Cloud Storage Menu ──
        elif data == "menu_cloud":
            keyboard = [
                [InlineKeyboardButton("📤 آپلود فایل", callback_data="cloud_upload")],
                [InlineKeyboardButton("📂 فایل‌های من", callback_data="cloud_files")],
                [InlineKeyboardButton("📥 دانلود فایل", callback_data="cloud_download")],
                [InlineKeyboardButton("📊 وضعیت ذخیره‌سازی", callback_data="cloud_status")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "☁️ فضای ابری\n\nذخیره‌سازی یکپارچه ۵+ سرویس:\n"
                "Google Drive + MEGA + Dropbox + pCloud + Internxt\n"
                "مجموعاً بیش از ۵۷ گیگابایت رایگان!",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "cloud_upload":
            await query.edit_message_text("📤 آپلود فایل\n\nیک فایل بفرست و /cloud رو ریپلی کن")

        elif data == "cloud_files":
            await query.edit_message_text("📂 فایل‌های من\n\nاز /myfiles استفاده کن")

        elif data == "cloud_download":
            await query.edit_message_text(
                "📥 دانلود فایل\n\nاز /download استفاده کن:\nمثال: /download myfile.pdf"
            )

        elif data == "cloud_status":
            await query.edit_message_text("📊 وضعیت ذخیره‌سازی\n\nاز /cloud_status استفاده کن")

        # ── v2.0.0: Referral Menu ──
        elif data == "menu_referral":
            keyboard = [
                [InlineKeyboardButton("🎁 لینک دعوت من", callback_data="ref_link")],
                [InlineKeyboardButton("🏆 جدول برترین‌ها", callback_data="ref_board")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🎁 دعوت دوستان\n\nدوستانت رو دعوت کن و جایزه بگیر!",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "ref_link":
            await query.edit_message_text("🎁 لینک دعوت\n\nاز /referral استفاده کن")

        elif data == "ref_board":
            await query.edit_message_text("🏆 جدول برترین‌ها\n\nاز /referral_board استفاده کن")

        # ── v2.0.0: Language Menu ──
        elif data == "menu_language":
            keyboard = [
                [
                    InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                    InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang_fa"),
                ],
                [
                    InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
                    InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
                ],
                [
                    InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr"),
                    InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de"),
                ],
                [
                    InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                    InlineKeyboardButton("🇨🇳 中文", callback_data="lang_zh"),
                ],
                [
                    InlineKeyboardButton("🇯🇵 日本語", callback_data="lang_ja"),
                    InlineKeyboardButton("🇰🇷 한국어", callback_data="lang_ko"),
                ],
                [
                    InlineKeyboardButton("🇧🇷 Português", callback_data="lang_pt"),
                    InlineKeyboardButton("🇮🇳 हिन्दी", callback_data="lang_hi"),
                ],
                [
                    InlineKeyboardButton("🇹🇷 Türkçe", callback_data="lang_tr"),
                    InlineKeyboardButton("🇮🇩 Indonesia", callback_data="lang_id"),
                ],
                [
                    InlineKeyboardButton("🇮🇹 Italiano", callback_data="lang_it"),
                ],
                [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_back")],
            ]
            await query.edit_message_text(
                "🌐 زبان / Language\n\nزبان مورد نظرت رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data.startswith("lang_"):
            lang_code = data[5:]
            user_id = query.from_user.id if query.from_user else 0
            if user_id:
                async with db_session_factory() as session:
                    stmt = select(UserLanguage).where(UserLanguage.user_id == user_id)
                    existing = (await session.exec(stmt)).first()
                    if existing:
                        existing.language = lang_code
                        existing.updated_at = datetime.now(timezone.utc)
                    else:
                        session.add(UserLanguage(user_id=user_id, language=lang_code))
                    await session.commit()
            lang_names = {
                "en": "English 🇬🇧",
                "fa": "فارسی 🇮🇷",
                "ar": "العربية 🇸🇦",
                "es": "Español 🇪🇸",
                "fr": "Français 🇫🇷",
                "de": "Deutsch 🇩🇪",
                "ru": "Русский 🇷🇺",
                "zh": "中文 🇨🇳",
                "ja": "日本語 🇯🇵",
                "ko": "한국어 🇰🇷",
                "pt": "Português 🇧🇷",
                "hi": "हिन्दी 🇮🇳",
                "tr": "Türkçe 🇹🇷",
                "id": "Indonesia 🇮🇩",
                "it": "Italiano 🇮🇹",
            }
            lang_display = lang_names.get(lang_code, lang_code)
            await query.edit_message_text(f"✅ زبان انتخابی: {lang_display}")

        elif data == "menu_back":
            keyboard = [
                [
                    InlineKeyboardButton("🤖 هوش مصنوعی", callback_data="menu_ai"),
                    InlineKeyboardButton("💬 چت هوشمند", callback_data="menu_chat"),
                ],
                [
                    InlineKeyboardButton("🎨 تصویرسازی", callback_data="menu_image"),
                    InlineKeyboardButton("🔊 صدا", callback_data="menu_speech"),
                ],
                [
                    InlineKeyboardButton("☁️ فضای ابری", callback_data="menu_cloud"),
                    InlineKeyboardButton("🎁 دعوت دوستان", callback_data="menu_referral"),
                ],
                [
                    InlineKeyboardButton("🎮 بازی‌ها", callback_data="menu_games"),
                    InlineKeyboardButton("👤 چت ناشناس", callback_data="menu_anon"),
                ],
                [
                    InlineKeyboardButton("🛠️ ابزارها", callback_data="menu_tools"),
                    InlineKeyboardButton("🎭 شخصیت", callback_data="menu_personality"),
                ],
                [
                    InlineKeyboardButton("🏆 گیمیفیکیشن", callback_data="menu_gamification"),
                    InlineKeyboardButton("📊 تحلیل", callback_data="menu_analytics"),
                ],
                [
                    InlineKeyboardButton("🛡️ نظارت", callback_data="menu_moderation"),
                    InlineKeyboardButton("🌐 زبان", callback_data="menu_language"),
                ],
                [
                    InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"),
                    InlineKeyboardButton("👨‍💼 پنل مدیریت", callback_data="menu_admin"),
                ],
            ]
            await query.edit_message_text(
                "🤖 NEXUS AI v2.0.0\n\nیکی از گزینه‌ها رو انتخاب کن:",
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
            await query.edit_message_text("❌ شما هنوز در کانال عضو نشدید. لطفاً اول عضو بشید.")

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
            result = PersonalityEngine.set_personality(chat_id, args[1], set_by=user_id)
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
        cid = AdManager.create_campaign(chat_id, text, interval_hours=interval, created_by=user_id)
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
            status_icon = {"active": "🟢", "paused": "⏸️", "completed": "✅"}.get(c["status"], "❓")
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

    # ── Phase 13: Smart Moderation ─────────────────────────────────────────────

    async def mod_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable smart moderation (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        ModerationEngine.set_config(
            chat_id,
            anti_spam=True,
            anti_flood=True,
            link_filter=True,
            profanity_filter=True,
        )
        await _reply(update, "🛡️ سیستم نظارت هوشمند فعال شد.")

    async def mod_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable smart moderation (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        ModerationEngine.set_config(
            chat_id,
            anti_spam=False,
            anti_flood=False,
            link_filter=False,
            profanity_filter=False,
        )
        await _reply(update, "🛡️ سیستم نظارت هوشمند غیرفعال شد.")

    async def mod_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show moderation settings."""
        chat_id = _chat_id(update)
        cfg = ModerationEngine.get_config(chat_id)
        if cfg is None:
            await _reply(update, "🛡️ نظارت: غیرفعال (تنظیم نشده)")
            return
        status_icon = lambda v: "✅" if v else "❌"  # noqa: E731
        await _reply(
            update,
            f"🛡️ تنظیمات نظارت\n━━━━━━━━━━━━━━━━━━\n"
            f"آنتی‌اسپم: {status_icon(cfg.anti_spam)}\n"
            f"آنتی‌فلاد: {status_icon(cfg.anti_flood)}\n"
            f"فیلتر لینک: {status_icon(cfg.link_filter)}\n"
            f"فیلتر کلمات: {status_icon(cfg.profanity_filter)}\n"
            f"حداکثر هشدار: {cfg.max_warnings}\n"
            f"مدت میوت: {cfg.mute_duration_minutes} دقیقه",
        )

    async def mod_warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Warn a user manually. Reply to their message or give user_id."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        target_id: int | None = None
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
            await _reply(update, "❌ ریپلای روی پیام کاربر یا /warn <user_id>")
            return
        warnings = ModerationEngine.add_warning(target_id, chat_id)
        await _reply(update, f"⚠️ کاربر {target_id} هشدار دریافت کرد ({warnings} از 3)")

    async def mod_mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mute a user (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        if not context.args:
            await _reply(update, "❌ استفاده: /mute <user_id> [دقیقه]")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        duration = 30
        if len(context.args) > 1:
            try:
                duration = int(context.args[1])
            except ValueError:
                pass
        ModerationEngine.mute_user(target_id, chat_id, duration)
        await _reply(update, f"🔇 کاربر {target_id} میوت شد ({duration} دقیقه)")

    async def mod_unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Unmute a user (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        if not context.args:
            await _reply(update, "❌ استفاده: /unmute <user_id>")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return
        ModerationEngine.unmute_user(target_id, chat_id)
        await _reply(update, f"🔊 کاربر {target_id} آنمیوت شد.")

    async def mod_reputation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show user reputation."""
        chat_id = _chat_id(update)
        target_id: int | None = None
        if context.args:
            try:
                target_id = int(context.args[0])
            except ValueError:
                pass
        if target_id is None:
            target_id = _user_id(update)
        if target_id is None:
            return
        rep = ModerationEngine.get_reputation(target_id, chat_id)
        if rep is None:
            await _reply(update, f"👤 کاربر {target_id}: اعتبار ۰ | هشدار ۰")
            return
        await _reply(
            update,
            f"👤 کاربر {target_id}\n━━━━━━━━━━━━━━━━━━\n"
            f"⭐ اعتبار: {rep.reputation}\n"
            f"⚠️ هشدارها: {rep.warnings}\n"
            f"🔇 میوت: {'بله' if rep.is_muted else 'خیر'}",
        )

    # ── Phase 14: Gamification ─────────────────────────────────────────────────

    async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show user gamification profile."""
        user_id = _user_id(update)
        if user_id is None:
            return
        chat_id = _chat_id(update)
        profile = GamificationEngine.get_profile(user_id, chat_id)
        ach_text = GamificationEngine.format_achievements(profile["achievements"])
        await _reply(
            update,
            f"👤 پروفایل شما\n━━━━━━━━━━━━━━━━━━\n"
            f"⭐ سطح {profile['level']}: {profile['title']}\n"
            f"✨ XP: {profile['xp']}\n"
            f"📊 تا سطح بعد: {profile['xp_to_next']} XP\n"
            f"🔥 استریک: {profile['streak']} روز\n"
            f"🏆 دستاوردها ({profile['achievement_count']}):\n{ach_text}",
        )

    async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Claim daily XP reward."""
        user_id = _user_id(update)
        if user_id is None:
            return
        chat_id = _chat_id(update)
        result = GamificationEngine.claim_daily(user_id, chat_id)
        if not result["claimed"]:
            await _reply(
                update,
                f"⏰ امروز پاداش رو گرفتی!\n⏳ {result['remaining_hours']} ساعت تا پاداش بعدی",
            )
            return
        level_up_msg = ""
        if result.get("leveled_up"):
            level_up_msg = f"\n🎉 سطح جدید: {result['new_level']}!"
        await _reply(
            update,
            f"🎁 پاداش روزانه!\n━━━━━━━━━━━━━━━━━━\n"
            f"💰 پایه: +{result['base_reward']} XP\n"
            f"🔥 استریک ×{result['streak']}: +{result['streak_bonus']} XP\n"
            f"✅ مجموع: +{result['total_reward']} XP{level_up_msg}",
        )

    async def xp_leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show XP leaderboard for the chat."""
        chat_id = _chat_id(update)
        board = GamificationEngine.get_leaderboard(chat_id, limit=10)
        if not board:
            await _reply(update, "🏆 هنوز کسی XP نگرفته!")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 جدول امتیازات\n━━━━━━━━━━━━━━━━━━"]
        for i, entry in enumerate(board):
            medal = medals[i] if i < 3 else f"  {i + 1}."
            lines.append(f"{medal} کاربر {entry['user_id']} — {entry['title']} | {entry['xp']} XP")
        await _reply(update, "\n".join(lines))

    async def achievements_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show all available achievements and user progress."""
        user_id = _user_id(update)
        if user_id is None:
            return
        chat_id = _chat_id(update)
        unlocked = GamificationEngine.get_achievements(user_id, chat_id)
        lines = ["🏆 دستاوردها\n━━━━━━━━━━━━━━━━━━"]
        for aid, ach in _ACHIEVEMENTS.items():
            status = "✅" if aid in unlocked else "🔒"
            lines.append(f"{status} {ach['name']} — {ach['desc']}")
        lines.append(f"\n📊 {len(unlocked)}/{len(_ACHIEVEMENTS)} باز شده")
        await _reply(update, "\n".join(lines))

    # ── Phase 15: Analytics ────────────────────────────────────────────────────

    async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show analytics dashboard (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        dashboard = AnalyticsEngine.get_dashboard(chat_id)
        eng = dashboard["engagement_24h"]
        peak_text = (
            ", ".join(f"{p['label']} ({p['count']})" for p in dashboard["peak_hours_top3"])
            or "ندارد"
        )
        cmds_text = (
            ", ".join(f"/{c['command']} ({c['count']})" for c in dashboard["top_commands"])
            or "ندارد"
        )
        await _reply(
            update,
            f"📊 داشبورد تحلیلی\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 فعال ۲۴ ساعت: {dashboard['active_users_24h']}\n"
            f"👥 فعال ۷ روز: {dashboard['active_users_7d']}\n"
            f"📈 رویداد ۲۴ ساعت: {eng['total_events']}\n"
            f"📊 رویداد/کاربر: {eng['events_per_user']}\n"
            f"🕐 ساعات اوج: {peak_text}\n"
            f"⚡ دستورات پرکاربرد: {cmds_text}",
        )

    async def analytics_active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show active users (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        hours = 24
        if context.args:
            try:
                hours = int(context.args[0])
            except ValueError:
                pass
        users = AnalyticsEngine.get_active_users(chat_id, hours=hours)
        if not users:
            await _reply(update, f"👥 کاربر فعال در {hours} ساعت اخیر: ۰")
            return
        lines = [f"👥 کاربران فعال ({hours} ساعت اخیر):\n━━━━━━━━━━━━━━━━━━"]
        for u in users[:15]:
            lines.append(f"  👤 کاربر {u['user_id']}: {u['events']} رویداد")
        await _reply(update, "\n".join(lines))

    async def analytics_retention_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show retention data (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        days = 7
        if context.args:
            try:
                days = int(context.args[0])
            except ValueError:
                pass
        retention = AnalyticsEngine.get_retention(chat_id, days=days)
        if not retention["retention"]:
            await _reply(update, "📊 داده بازگشت کافی نیست.")
            return
        lines = [
            f"📊 بازگشت کاربران ({days} روز)\n"
            f"👤 سایز کوهورت: {retention['cohort_size']}\n"
            f"━━━━━━━━━━━━━━━━━━"
        ]
        for r in retention["retention"]:
            lines.append(f"  {r['date']}: {r['retained']} نفر ({r['rate']}%)")
        await _reply(update, "\n".join(lines))

    async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manually track an event (owner only). Usage: /track <event_type>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args:
            await _reply(update, "❌ استفاده: /track <نوع_رویداد>")
            return
        event_type = context.args[0]
        user_id = _user_id(update) or 0
        chat_id = _chat_id(update)
        eid = AnalyticsEngine.track_event(chat_id, user_id, event_type)
        await _reply(update, f"✅ رویداد ثبت شد (id={eid})")

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
        # Phase 5+16: Menu callbacks
        CallbackQueryHandler(menu_callback, pattern=r"^menu_"),
        CallbackQueryHandler(menu_callback, pattern=r"^chat_"),
        CallbackQueryHandler(menu_callback, pattern=r"^game_"),
        CallbackQueryHandler(menu_callback, pattern=r"^anon_"),
        CallbackQueryHandler(menu_callback, pattern=r"^ch_"),
        CallbackQueryHandler(menu_callback, pattern=r"^tool_"),
        CallbackQueryHandler(menu_callback, pattern=r"^set_"),
        CallbackQueryHandler(menu_callback, pattern=r"^pers_"),
        CallbackQueryHandler(menu_callback, pattern=r"^gam_"),
        CallbackQueryHandler(menu_callback, pattern=r"^an_"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_o(?:n|ff)$"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_c(?:fg)?$"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_rep$"),
        CallbackQueryHandler(menu_callback, pattern=r"^adm_"),
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
        # Phase 13: Smart Moderation
        CommandHandler("mod_on", mod_on_cmd),
        CommandHandler("mod_off", mod_off_cmd),
        CommandHandler("mod_config", mod_config_cmd),
        CommandHandler("warn", mod_warn_cmd),
        CommandHandler("mute", mod_mute_cmd),
        CommandHandler("unmute", mod_unmute_cmd),
        CommandHandler("reputation", mod_reputation_cmd),
        # Phase 14: Gamification
        CommandHandler("profile", profile_cmd),
        CommandHandler("daily", daily_cmd),
        CommandHandler("xp_leaderboard", xp_leaderboard_cmd),
        CommandHandler("achievements", achievements_cmd),
        # Phase 15: Analytics
        CommandHandler("analytics", analytics_cmd),
        CommandHandler("analytics_active", analytics_active_cmd),
        CommandHandler("analytics_retention", analytics_retention_cmd),
        CommandHandler("track", track_cmd),
        # ── v2.0.0: AI Commands ──
        CommandHandler("ai", ai_cmd),
        CommandHandler("ask", ask_cmd),
        CommandHandler("code", code_cmd),
        CommandHandler("translate", ai_translate_cmd),
        CommandHandler("vision", vision_cmd),
        CommandHandler("summarize", summarize_cmd),
        # ── v2.0.0: Image Generation ──
        CommandHandler("image", image_cmd),
        # ── v2.0.0: Speech ──
        CommandHandler("tts", tts_cmd),
        CommandHandler("stt", stt_cmd),
        # ── v2.0.0: Cloud Storage ──
        CommandHandler("cloud", cloud_cmd),
        CommandHandler("myfiles", myfiles_cmd),
        CommandHandler("download", download_cmd),
        CommandHandler("cloud_status", cloud_status_cmd),
        # ── v2.0.0: Referral ──
        CommandHandler("referral", referral_cmd),
        CommandHandler("ref", referral_cmd),  # alias for /referral
        CommandHandler("referral_board", referral_board_cmd),
        # ── v2.0.0: Language ──
        CommandHandler("language", language_cmd),
        # ── v2.1: New Chat ──
        CommandHandler("newchat", newchat_cmd),
        # ── v3.0.0: Knowledge ──────────────────────────────────
        CommandHandler("learn", learn_cmd),
        CommandHandler("wiki", wiki_cmd),
        CommandHandler("search", search_cmd),
        # ── v3.0.0: Free Tools ──────────────────────────────────
        CommandHandler("weather", weather_cmd),
        CommandHandler("rate", rate_cmd),
        CommandHandler("convert", convert_cmd_new),
        CommandHandler("news", news_cmd),
        CommandHandler("yt", yt_cmd),
        CommandHandler("youtube", youtube_cmd),
        # ── v2.1: Onboarding callbacks ──
        CallbackQueryHandler(onboarding_callback_handler, pattern=r"^onboarding_"),
        CallbackQueryHandler(menu_callback, pattern=r"^lang_"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_ai$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_image$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_cloud$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_speech$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_referral$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_language$"),
        # ── v2.0.0: Referral deep-link ──
        CommandHandler("start", start_referral_handler),
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
