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
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes, MessageHandler
from telegram.ext.filters import MessageFilter

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ai_memory import AIMemoryEngine
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.features.games import NumberGuess, QuickPoll, QuizGame, WordleFA
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
    }


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
