"""Telegram surface for the feature engines (feature-wiring batch).

This module is the single wiring point for the engines that existed in
``features/`` but were unreachable dead code: the tool engines
(Calculator, ReminderSystem, Translator, UnitConverter), the game
engines (QuizGame, NumberGuess, WordleFA, QuickPoll), the referral loop
(``ReferralEngine.process_referral``), force-join (real membership check
via the injected bot) and anonymous chat (bot injection + a live
delivery path for paired users).

Design notes
------------
- Engines are constructed **once** (:func:`build_feature_engines`) and
  shared by every command. The previous "new instance per handler call"
  pattern silently dropped game state (quiz progress, wordle rounds,
  number-guess targets) between messages.
- The Telegram bot only exists at runtime, so engines that need it
  (reminders, force-join, anonymous chat) are **bindable**: startup
  binds the bot once; :func:`_ensure_bot` additionally binds lazily on
  first use as a safety net for handler-level construction (tests).
- Handlers are returned as a name-keyed dict of closures bound to the
  shared :class:`FeatureEngines`, so ``build_handlers`` stays a thin
  registry and every command is unit-testable without a live bot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.ext.filters import MessageFilter

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ads import AdManager, AdValidationError
from nexus_ai_agent.features.ai_memory import AIMemoryEngine
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.channel_manager import (
    ChannelManager,
    ChannelValidationError,
    parse_schedule_when,
)
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.features.games import NumberGuess, QuickPoll, QuizGame, WordleFA
from nexus_ai_agent.features.onboarding import (
    OnboardingStore,
    handle_onboarding_callback,
    maybe_onboard,
)
from nexus_ai_agent.features.owner_control import _get_owner_id
from nexus_ai_agent.features.referral import ReferralEngine
from nexus_ai_agent.features.tools import Calculator, ReminderSystem, Translator, UnitConverter
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

# Persian/Arabic-Indic digits → ASCII, for numeric command arguments.
_FA_DIGITS = str.maketrans(
    "".join(chr(c) for c in range(0x06F0, 0x06FA))  # Persian digits
    + "".join(chr(c) for c in range(0x0660, 0x066A)),  # Arabic-Indic digits
    "01234567890123456789",
)

_POLL_PREFIX = "pollvote_"
_QUIZ_PREFIX = "quiz_"


def _to_float(raw: str) -> float:
    """Parse a numeric argument, accepting Persian/Arabic digits."""
    return float(raw.translate(_FA_DIGITS).replace(",", "."))


# Commands whose real implementation is registered before the leased
# ``handlers.py`` stubs. ``start`` is already the shared closure inside
# ``build_handlers`` and is intentionally absent here.
OPS_COMMANDS: tuple[str, ...] = (
    "post",
    "schedule",
    "schedule_cancel",
    "schedules",
    "ban",
    "unban",
    "stats",
    "welcome",
    "pin",
    "ad_create",
    "ad_list",
    "ad_pause",
    "ad_resume",
    "ad_delete",
    "ad_stats",
)


def _is_ops_owner(user_id: int) -> bool:
    """Fail closed when the owner id was never configured.

    ``owner_telegram_id`` defaults to 0. Treating 0 as a real owner would make
    a missing user — and any update whose user id is 0 — an operator.
    """
    owner = _get_owner_id()
    return owner != 0 and user_id == owner


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw.translate(_FA_DIGITS))
    except ValueError:
        return None


def _deny(update: Update) -> Any:
    return _reply(update, "⛔ Access denied")


def _ensure_bot(manager: Any, bot: Any | None) -> None:
    """Lazily bind the runtime bot to a bindable engine (idempotent)."""
    if bot is None or manager is None:
        return
    is_bound = getattr(manager, "is_bound", manager.bot is not None)
    if not is_bound:
        manager.bind(bot)


async def _reply(update: Update, text: str, **kwargs: Any) -> None:
    message = update.message or update.edited_message
    if message is not None:
        await message.reply_text(text, **kwargs)


# ═══════════════════════════════════════════════════════════════════════
# Engine container
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class FeatureEngines:
    """One shared instance of every wired feature engine."""

    reminders: ReminderSystem
    calculator: Calculator
    translator: Translator
    converter: UnitConverter
    quiz: QuizGame
    number_guess: NumberGuess
    wordle: WordleFA
    poll: QuickPoll
    referral: ReferralEngine
    force_join: ForceJoinManager
    anon: AnonymousChatManager
    ai_memory: AIMemoryEngine
    ads: AdManager
    channel: ChannelManager
    onboarding: OnboardingStore


def build_feature_engines(
    settings: Settings, referral: ReferralEngine | None = None
) -> FeatureEngines:
    """Construct the shared engine container.

    *referral* may be the application-owned ``ReferralEngine`` (kept in
    ``bot_data``) so that only one referral instance exists per process.
    """
    return FeatureEngines(
        reminders=ReminderSystem(db_path=settings.db_path),
        calculator=Calculator(),
        translator=Translator(),
        converter=UnitConverter(),
        quiz=QuizGame(),
        number_guess=NumberGuess(),
        wordle=WordleFA(),
        poll=QuickPoll(),
        referral=referral or ReferralEngine(db_path=settings.db_path),
        force_join=ForceJoinManager(),
        anon=AnonymousChatManager(),
        # Single shared instance (task-102 acceptance): the P0-7 consent
        # gate lives inside the engine, so every call site — /memory,
        # /forget_me and the main message handler — shares one gate state.
        ai_memory=AIMemoryEngine(),
        ads=AdManager(db_path=settings.db_path),
        channel=ChannelManager(db_path=settings.db_path),
        onboarding=OnboardingStore(db_path=settings.db_path),
    )


# ═══════════════════════════════════════════════════════════════════════
# Anonymous-chat delivery filter
# ═══════════════════════════════════════════════════════════════════════


class ActiveAnonSessionFilter(MessageFilter):
    """Matches plain text from users who currently have an anon partner.

    Registered **before** the catch-all ``on_message`` handler: while the
    filter matches, the message is routed to the anonymous partner
    instead of the AI graph, and PTB's blocking stops further handlers.
    """

    def __init__(self, has_partner: Callable[[int], bool]) -> None:
        super().__init__()
        self._has_partner = has_partner

    def filter(self, message: Message) -> bool:
        user = message.from_user
        if user is None or not message.text:
            return False
        return self._has_partner(int(user.id))


# ═══════════════════════════════════════════════════════════════════════
# Command factory
# ═══════════════════════════════════════════════════════════════════════


def build_feature_command_handlers(engines: FeatureEngines, settings: Settings) -> dict[str, Any]:
    """Return ``{name: handler}`` closures bound to the shared engines.

    Names match the command strings registered in ``build_handlers``.
    """

    # ── Utility tools ──────────────────────────────────────────────────

    async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        expr = " ".join(context.args or [])
        if not expr:
            await _reply(update, "🧮 استفاده: /calc 2+2*3\nمثال: /calc sqrt(144) یا /calc ۲^۰")
            return
        await _reply(update, engines.calculator.evaluate(expr))

    async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.effective_chat is None:
            return
        _ensure_bot(engines.reminders, context.bot)
        args = context.args or []
        if not args:
            await _reply(
                update, "⏰ استفاده: /remind 30m متن یادآوری\nمثال: /remind 2h جلسه ساعت ۵"
            )
            return
        time_str = args[0]
        text = " ".join(args[1:]) or time_str
        result = await engines.reminders.set_reminder(
            int(user.id), int(update.effective_chat.id), time_str, text
        )
        await _reply(update, result)

    async def cancel_remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        args = context.args or []
        if not args:
            await _reply(update, "❌ استفاده: /cancel_remind <id>\nلیست: /reminds")
            return
        try:
            reminder_id = int(args[0].translate(_FA_DIGITS))
        except ValueError:
            await _reply(update, "❌ شناسه باید عدد باشد. لیست: /reminds")
            return
        await _reply(update, await engines.reminders.cancel_reminder(reminder_id, int(user.id)))

    async def reminds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        await _reply(update, await engines.reminders.list_reminders(int(user.id)))

    async def tr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        source, target = "fa", "en"
        if (
            len(args) >= 3
            and len(args[0]) == 2
            and len(args[1]) == 2
            and args[0].isalpha()
            and args[1].isalpha()
        ):
            source, target = args[0].lower(), args[1].lower()
            text = " ".join(args[2:])
        else:
            text = " ".join(args)
        if not text:
            await _reply(
                update,
                "🌐 استفاده: /tr <متن>\nیا: /tr <از> <به> <متن> — مثال: /tr en fa سلام",
            )
            return
        result = await engines.translator.translate(text, source=source, target=target)
        await _reply(update, f"🌐 {text} → {result}")

    async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 3:
            await _reply(
                update,
                "💱 استفاده: /convert <مقدار> <از> <به>\n"
                "مثال: /convert 100 usd irt یا /convert 5 c f",
            )
            return
        try:
            amount = _to_float(args[0])
        except ValueError:
            await _reply(update, f"❌ مقدار عددی نیست: {args[0]}")
            return
        await _reply(update, engines.converter.convert(amount, args[1], args[2]))

    # ── Games ──────────────────────────────────────────────────────────

    async def guess_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        await _reply(update, engines.number_guess.start(int(user.id)))

    async def guess_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        await _reply(update, engines.number_guess.stop(int(user.id)))

    async def guess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        args = context.args or []
        if not args:
            await _reply(update, "🔢 استفاده: /guess <عدد> — یا /guess_start برای شروع")
            return
        try:
            number = int(args[0].translate(_FA_DIGITS))
        except ValueError:
            await _reply(update, f"❌ عدد نیست: {args[0]}")
            return
        await _reply(update, engines.number_guess.guess(int(user.id), number))

    async def wordle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        args = context.args or []
        if len(args) == 1 and len(args[0]) == 5 and engines.wordle.is_active(int(user.id)):
            await _reply(update, engines.wordle.guess(int(user.id), args[0].lower()))
            return
        await _reply(update, engines.wordle.start(int(user.id)))

    async def wordle_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        await _reply(update, engines.wordle.stop(int(user.id)))

    async def poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        raw = " ".join(context.args or "")
        if not raw or "|" not in raw:
            await _reply(
                update,
                "📊 استفاده: /poll <سوال> | <گزینه ۱> | <گزینه ۲> [ | <گزینه ۳> ...]",
            )
            return
        parts = [p.strip() for p in raw.split("|")]
        question = parts[0]
        options = [p for p in parts[1:] if p]
        if not question or len(options) < 2:
            await _reply(update, "❌ حداقل یک سوال و دو گزینه لازم است.")
            return
        poll_id = engines.poll.create(question, options)
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"{_POLL_PREFIX}{poll_id}_{i}")]
            for i, opt in enumerate(options)
        ]
        await _reply(
            update,
            f"📊 {question}\n\nرای بدهید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        if not data.startswith(_POLL_PREFIX):
            await query.answer("Vote counted!")
            return
        payload = data[len(_POLL_PREFIX) :]
        poll_id, _, idx = payload.rpartition("_")
        user = query.from_user
        user_id = int(user.id) if user is not None else 0
        try:
            option_idx = int(idx)
        except ValueError:
            await query.answer()
            return
        accepted = engines.poll.vote(poll_id, option_idx, user_id)
        results = engines.poll.get_results(poll_id)
        await query.answer("ثبت شد!" if accepted else "قبلاً رای داده‌اید")
        if query.message is not None and results is not None:
            try:
                await query.edit_message_text(results)
            except Exception:  # noqa: BLE001 — stale-message edits are expected
                logger.debug("poll_edit_failed", poll_id=poll_id)

    async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        question = engines.quiz.get_question(int(user.id))
        if question is None:
            await _reply(update, "❓ No question available right now.")
            return
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"{_QUIZ_PREFIX}{i}")]
            for i, opt in enumerate(question["options"])
        ]
        await _reply(
            update,
            f"❓ **Quiz Time!**\n\n{question['q']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        user = query.from_user
        user_id = int(user.id) if user is not None else 0
        chat_id = int(update.effective_chat.id) if update.effective_chat else 0
        try:
            choice = int(data.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            await query.answer()
            return
        correct = engines.quiz.check_answer(user_id, choice)
        score = engines.quiz.update_score(user_id, chat_id, correct)
        verdict = "✅ درست بود!" if correct else f"❌ اشتباه بود! امتیاز شما: {score}"
        try:
            await query.answer(verdict)
            await query.edit_message_text(verdict)
        except Exception:  # noqa: BLE001
            logger.debug("quiz_edit_failed", user_id=user_id)

    # ── Anonymous chat (bot injection + delivery path) ─────────────────

    async def anon_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        _ensure_bot(engines.anon, context.bot)
        await _reply(update, await engines.anon.join_queue(int(user.id)))

    async def anon_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        _ensure_bot(engines.anon, context.bot)
        await _reply(update, await engines.anon.leave_chat(int(user.id)))

    async def anon_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        _ensure_bot(engines.anon, context.bot)
        await _reply(
            update, await engines.anon.report_user(int(user.id), settings.owner_telegram_id)
        )

    async def anon_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Deliver a plain message to the user's anonymous partner."""
        message = update.message
        user = update.effective_user
        if message is None or user is None:
            return
        _ensure_bot(engines.anon, context.bot)
        sent = await engines.anon.send_anon_message(int(user.id), message.text or "")
        if sent:
            await message.reply_text("✅ پیام شما به‌صورت ناشناس ارسال شد.")
        else:
            await message.reply_text("⚠️ دیگر در چت ناشناسی نیستید. /anon_start")

    # ── Force join (real membership check via injected bot) ───────────

    async def forcejoin_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        _ensure_bot(engines.force_join, context.bot)
        user_id = int(query.from_user.id)
        is_member = await engines.force_join.check_membership(user_id)
        if is_member:
            engines.force_join.invalidate_cache(user_id)
            await query.edit_message_text("✅ عضویت شما تأیید شد! می‌تونید از ربات استفاده کنید.")
        else:
            await query.edit_message_text("❌ شما هنوز در کانال عضو نشدید. لطفاً اول عضو بشید.")

    # ── /start with referral deep-link (P0-4: single handler) ─────────

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(
            update,
            "👋 Welcome to NEXUS AI Agent!\n\n"
            "I am a multi-agent system designed for power users.\n"
            "Use /help to see what I can do.",
        )
        # Additive: first-run tour. Failures stay inside maybe_onboard so the
        # referral contract below cannot regress.
        await maybe_onboard(update, context, engines.onboarding)
        user = update.effective_user
        if user is None:
            return
        start_param = " ".join(context.args or [])
        if not start_param.startswith("ref_"):
            return
        # P1-2: sync SQLite work off the event loop (call-site offload).
        result = await asyncio.to_thread(
            engines.referral.process_referral, int(user.id), start_param
        )
        if result.get("success"):
            link = await asyncio.to_thread(
                engines.referral.get_referral_link, int(user.id), settings.bot_username
            )
            await _reply(
                update,
                f"🎁 خوش آمدید! شما از طریق یک دوست شروع کردید (+50 XP).\nلینک دعوت شما: {link}",
            )
        elif result.get("error") == "self_referral":
            logger.info("referral_self_ignored", user_id=int(user.id))
        else:
            await _reply(update, "⚠️ کد دعوت نامعتبر یا منقضی است.")

    # ── Ads and channel ops (registered before the leased stubs) ─────

    async def _guard(update: Update) -> tuple[int, int] | None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return None
        user_id = int(user.id)
        if not _is_ops_owner(user_id):
            await _deny(update)
            return None
        return user_id, int(chat.id)

    async def ad_create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        user_id, chat_id = gated
        args = context.args or []
        if len(args) < 3:
            await _reply(update, "❌ استفاده: /ad_create <ساعت> <تکرار> <متن>")
            return
        interval = _parse_int(args[0])
        repeats = _parse_int(args[1])
        if interval is None or repeats is None:
            await _reply(update, "❌ ساعت و تعداد تکرار باید عدد باشند.")
            return
        text = " ".join(args[2:])
        try:
            campaign_id = await asyncio.to_thread(
                engines.ads.create_campaign, chat_id, text, interval, repeats, user_id
            )
        except AdValidationError as exc:
            await _reply(update, str(exc))
            return
        await _reply(update, f"✅ کمپین #{campaign_id} ذخیره شد و در همین چت منتشر می‌شود.")

    async def ad_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        rows = await asyncio.to_thread(engines.ads.list_campaigns, chat_id)
        if not rows:
            await _reply(update, "📢 کمپینی در این چت نیست.")
            return
        lines = ["📢 کمپین‌های این چت:"]
        for row in rows[:20]:
            cap = row["max_repeats"] or "∞"
            hours = row["interval_hours"]
            lines.append(
                f"#{row['id']} [{row['status']}] هر {hours:g}س "
                f"×{row['repeat_count']}/{cap} — {row['text']}"
            )
        await _reply(update, "\n".join(lines))

    async def _ad_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
        args = context.args or []
        if not args:
            await _reply(update, "❌ شناسه کمپین را بدهید.")
            return None
        campaign_id = _parse_int(args[0])
        if campaign_id is None or campaign_id <= 0:
            await _reply(update, "❌ شناسه باید عدد باشد.")
            return None
        return campaign_id

    async def ad_pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        campaign_id = await _ad_id(update, context)
        if campaign_id is None:
            return
        ok = await asyncio.to_thread(engines.ads.pause_campaign, campaign_id)
        await _reply(update, "⏸ کمپین متوقف شد." if ok else "❌ کمپین فعال پیدا نشد.")

    async def ad_resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        campaign_id = await _ad_id(update, context)
        if campaign_id is None:
            return
        ok = await asyncio.to_thread(engines.ads.resume_campaign, campaign_id)
        await _reply(update, "▶️ کمپین دوباره فعال شد." if ok else "❌ کمپین متوقف پیدا نشد.")

    async def ad_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        campaign_id = await _ad_id(update, context)
        if campaign_id is None:
            return
        ok = await asyncio.to_thread(engines.ads.delete_campaign, campaign_id)
        await _reply(update, "🗑 کمپین حذف شد." if ok else "❌ کمپین پیدا نشد.")

    async def ad_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        stats = await asyncio.to_thread(engines.ads.get_stats, chat_id)
        await _reply(
            update,
            "📊 تبلیغات این چت: "
            f"کل {stats['total']} · فعال {stats['active']} · "
            f"متوقف {stats['paused']} · تمام‌شده {stats['completed']}",
        )

    async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        text = " ".join(context.args or []).strip()
        if not text:
            await _reply(update, "❌ استفاده: /post <متن>")
            return
        _ensure_bot(engines.channel, context.bot)
        try:
            await engines.channel.post_to_channel(chat_id, text)
        except ChannelValidationError as exc:
            await _reply(update, str(exc))
            return
        except Exception:  # noqa: BLE001
            logger.exception("post_failed", chat_id=chat_id)
            await _reply(update, "❌ ارسال پیام ناموفق بود.")
            return
        await _reply(update, "✅ پیام در همین چت ارسال شد.")

    async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        args = context.args or []
        if len(args) < 3:
            await _reply(update, "❌ استفاده: /schedule YYYY-MM-DD HH:MM <متن>")
            return
        try:
            when = parse_schedule_when(args[0], args[1], now=datetime.now(timezone.utc))
        except ChannelValidationError as exc:
            await _reply(update, str(exc))
            return
        text = " ".join(args[2:])
        _ensure_bot(engines.channel, context.bot)
        try:
            schedule_id = await engines.channel.schedule_post(chat_id, text, when)
        except ChannelValidationError as exc:
            await _reply(update, str(exc))
            return
        stamp = when.strftime("%Y-%m-%d %H:%M")
        await _reply(update, f"✅ پست #{schedule_id} برای {stamp} UTC زمان‌بندی شد.")

    async def schedule_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        args = context.args or []
        schedule_id = _parse_int(args[0]) if args else None
        if schedule_id is None or schedule_id <= 0:
            await _reply(update, "❌ استفاده: /schedule_cancel <id>")
            return
        ok = await engines.channel.cancel_schedule(schedule_id, chat_id=chat_id)
        await _reply(update, "✅ زمان‌بندی لغو شد." if ok else "❌ زمان‌بندی در انتظار پیدا نشد.")

    async def schedules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        rows = await asyncio.to_thread(engines.channel.list_pending, chat_id)
        if not rows:
            await _reply(update, "🗓 پست زمان‌بندی‌شده‌ای در این چت نیست.")
            return
        lines = ["🗓 پست‌های در انتظار:"]
        for row in rows[:20]:
            lines.append(f"#{row['id']} {row['scheduled_at']} — {row['text']}")
        await _reply(update, "\n".join(lines))

    async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        args = context.args or []
        user_id = _parse_int(args[0]) if args else None
        if user_id is None or user_id <= 0:
            await _reply(update, "❌ استفاده: /ban <user_id> [دلیل]")
            return
        reason = " ".join(args[1:])[:200]
        _ensure_bot(engines.channel, context.bot)
        ok = await engines.channel.ban_user(chat_id, user_id, reason=reason)
        await _reply(update, "✅ کاربر مسدود شد." if ok else "❌ مسدودسازی ناموفق بود.")

    async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        args = context.args or []
        user_id = _parse_int(args[0]) if args else None
        if user_id is None or user_id <= 0:
            await _reply(update, "❌ استفاده: /unban <user_id>")
            return
        _ensure_bot(engines.channel, context.bot)
        ok = await engines.channel.unban_user(chat_id, user_id)
        await _reply(update, "✅ مسدودیت برداشته شد." if ok else "❌ رفع مسدودیت ناموفق بود.")

    async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        _ensure_bot(engines.channel, context.bot)
        try:
            count = await engines.channel.get_members_count(chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("member_count_failed", chat_id=chat_id)
            await _reply(update, "❌ دریافت آمار ناموفق بود.")
            return
        await _reply(update, f"📊 اعضای این چت: {count}")

    async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        args = context.args or []
        if not args:
            current = engines.channel.get_welcome_message(chat_id)
            if not current:
                await _reply(
                    update,
                    "👋 خوشامد تنظیم نشده. استفاده: /welcome <متن> — {name} نام عضو است.",
                )
            else:
                await _reply(update, f"👋 خوشامد فعلی:\n{current}")
            return
        if len(args) == 1 and args[0].lower() == "clear":
            engines.channel.clear_welcome_message(chat_id)
            await _reply(update, "✅ پیام خوشامد پاک شد.")
            return
        try:
            engines.channel.set_welcome_message(chat_id, " ".join(args))
        except ChannelValidationError as exc:
            await _reply(update, str(exc))
            return
        await _reply(update, "✅ پیام خوشامد ذخیره شد.")

    async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        gated = await _guard(update)
        if gated is None:
            return
        _, chat_id = gated
        message = update.message
        reply = getattr(message, "reply_to_message", None) if message is not None else None
        message_id = None
        if reply is not None and getattr(reply, "message_id", None):
            message_id = int(reply.message_id)
        elif context.args:
            message_id = _parse_int(context.args[0])
        if message_id is None or message_id <= 0:
            await _reply(update, "❌ روی یک پیام ریپلای کنید یا /pin <message_id>")
            return
        _ensure_bot(engines.channel, context.bot)
        try:
            await engines.channel.pin_message(chat_id, message_id)
        except ChannelValidationError as exc:
            await _reply(update, str(exc))
            return
        except Exception:  # noqa: BLE001
            logger.exception("pin_failed", chat_id=chat_id)
            await _reply(update, "❌ سنجاق کردن ناموفق بود.")
            return
        await _reply(update, "📌 پیام سنجاق شد.")

    async def onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        user = query.from_user
        user_id = int(user.id) if user is not None else 0
        lang = engines.onboarding.language_for(user_id)
        await handle_onboarding_callback(update, context, lang)

    async def new_member_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or not getattr(message, "new_chat_members", None):
            return
        chat = update.effective_chat
        if chat is None:
            return
        chat_id = int(chat.id)
        _ensure_bot(engines.channel, context.bot)
        for member in message.new_chat_members:
            if getattr(member, "is_bot", False):
                continue
            name = (
                getattr(member, "full_name", None)
                or getattr(member, "first_name", None)
                or "friend"
            )
            try:
                sent = await engines.channel.welcome_new_member(chat_id, str(name))
            except Exception:  # noqa: BLE001
                logger.exception("welcome_failed", chat_id=chat_id)
                continue
            if sent is None:
                await _reply(update, f"Welcome {name} to the group!")

    # ── Anonymous message routing (must precede the catch-all) ────────

    anon_message_handler: Any = MessageHandler(
        ActiveAnonSessionFilter(engines.anon.has_active_session), anon_message_cmd
    )

    return {
        "calc": calc_cmd,
        "remind": remind_cmd,
        "cancel_remind": cancel_remind_cmd,
        "reminds": reminds_cmd,
        "tr": tr_cmd,
        "convert": convert_cmd,
        "guess_start": guess_start_cmd,
        "guess_stop": guess_stop_cmd,
        "guess": guess_cmd,
        "wordle": wordle_cmd,
        "wordle_stop": wordle_stop_cmd,
        "poll": poll_cmd,
        "poll_callback": poll_callback,
        "quiz": quiz_cmd,
        "quiz_callback": quiz_callback,
        "anon_start": anon_start_cmd,
        "anon_stop": anon_stop_cmd,
        "anon_report": anon_report_cmd,
        "anon_message_handler": anon_message_handler,
        "forcejoin_verify": forcejoin_verify_callback,
        "start": start_cmd,
        "post": post_cmd,
        "schedule": schedule_cmd,
        "schedule_cancel": schedule_cancel_cmd,
        "schedules": schedules_cmd,
        "ban": ban_cmd,
        "unban": unban_cmd,
        "stats": stats_cmd,
        "welcome": welcome_cmd,
        "pin": pin_cmd,
        "ad_create": ad_create_cmd,
        "ad_list": ad_list_cmd,
        "ad_pause": ad_pause_cmd,
        "ad_resume": ad_resume_cmd,
        "ad_delete": ad_delete_cmd,
        "ad_stats": ad_stats_cmd,
        "onboarding_callback": onboarding_callback,
        "new_member": new_member_cmd,
    }


def register_ops_handlers(application: Any, cmds: dict[str, Any]) -> list[str]:
    """Register real ops handlers before the leased ``handlers.py`` stubs.

    ``bot/handlers.py`` is fenced by the live ``pr47-feature-wiring-continuation``
    lease (other branch, ``active_in_review`` until 2026-09-23T17:30Z). PTB
    runs the first matching handler in a group and does not continue
    (``block=True``). Registering the real callbacks first makes the commands
    reachable without editing the leased file. Delete this prepend when that
    lease is released and the stubs in ``handlers.py`` are removed.
    """
    registered: list[str] = []
    for name in OPS_COMMANDS:
        handler = cmds.get(name)
        if handler is None:
            continue
        application.add_handler(CommandHandler(name, handler, block=True))
        registered.append(name)
    callback = cmds.get("onboarding_callback")
    if callback is not None:
        application.add_handler(CallbackQueryHandler(callback, pattern=r"^onboarding_", block=True))
        registered.append("onboarding_callback")
    new_member = cmds.get("new_member")
    if new_member is not None:
        application.add_handler(
            MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member, block=True)
        )
        registered.append("new_member")
    return registered


def forcejoin_gate(engines: FeatureEngines) -> Callable[[int], Any]:
    """Return an async predicate *user_id → block message | None*.

    Used by the catch-all ``on_message``: when force-join is enabled for
    any chat and the user is not a channel member, the reply carries the
    join keyboard. Force-join is disabled by default, so this is a
    no-op unless the owner turns it on.
    """

    async def _gate(user_id: int) -> str | None:
        if await engines.force_join.should_block(user_id, ""):
            return (
                "📢 برای استفاده از ربات ابتدا در کانال رسمی عضو شوید:\n\n"
                "«عضویت در کانال» برای عضویت و سپس «تأیید عضویت» را بزنید."
            )
        return None

    return _gate


def join_keyboard() -> InlineKeyboardMarkup:
    """The force-join join/verify keyboard (shared by gate replies)."""
    return ForceJoinManager.get_join_keyboard()
