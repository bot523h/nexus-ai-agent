from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from nexus_ai_agent.features.ai_memory import CONSENT_UNSET, AIMemoryEngine

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]
ReplyFn = Callable[..., Awaitable[None]]

# ── P0-7: one-time consent question for LLM egress ─────────────────────
AIMEMORY_CONSENT_PROMPT = (
    "🔐 **رضایت برای حافظه هوشمند**\n\n"
    "برای اینکه پاسخ‌های من دقیق‌تر و شخصی‌سازی‌شده‌تر باشد، اطلاعات مهم را "
    "از پیام‌های شما در حافظه بلندمدت ذخیره می‌کنم. برای این کار، متن پیام‌های "
    "شما در صورت نیاز به یک مدل هوش مصنوعی ابری (Google Gemini) ارسال می‌شود.\n\n"
    "آیا موافق هستید؟ تا زمانی که تأیید نکنید، هیچ اطلاعاتی به بیرون ارسال "
    "نمی‌شود. هر زمان می‌توانید تصمیم خود را تغییر دهید."
)
AIMEMORY_CONSENT_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ قبول", callback_data="aimem:grant"),
            InlineKeyboardButton("🛑 رد", callback_data="aimem:deny"),
        ]
    ]
)


def build_memory_handlers(engine: AIMemoryEngine) -> tuple[Handler, Handler, Handler]:
    """Build the AI-memory handlers around the **shared** engine.

    Returns ``(memory_cmd, forget_me_cmd, consent_callback)``.

    P0-7: handlers must not construct their own ``AIMemoryEngine`` per call
    (that would fork the rate-limit state and bypass the single-instance
    contract) — the one owned by :class:`FeatureEngines` is injected here.
    """

    async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show what the AI remembers about the user."""
        if not update.effective_user or not update.message:
            return
        user_id = update.effective_user.id
        message = update.message
        ctx = await engine.get_context(user_id)

        if ctx:
            nl = "\n"
            formatted_ctx = ctx.replace(" | ", nl)
            await message.reply_text(
                f"🧠 *آنچه من از شما می‌دانم:*\n\n{formatted_ctx}\n\n"
                "این اطلاعات به من کمک می‌کند تا پاسخ‌های دقیق‌تری به شما بدهم.",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text("🧠 من هنوز اطلاعات خاصی از شما در حافظه بلندمدتم ندارم.")

    async def forget_me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Wipe user memory **and the consent record** (forget ⇒ revoke)."""
        if not update.effective_user or not update.message:
            return
        user_id = update.effective_user.id
        message = update.message
        await engine.forget_user(user_id)
        await message.reply_text(
            "✅ تمامی اطلاعات حافظه بلندمدت شما (به‌همراه اجازه‌ی "
            "به‌راورد متن پیام‌ها با مدل هوش مصنوعی) پاک شد."
        )

    async def consent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist the user's AI-memory egress vote (P0-7)."""
        query = update.callback_query
        if query is None or not update.effective_user:
            return
        user_id = int(update.effective_user.id)
        granted = query.data == "aimem:grant"
        await engine.set_consent(user_id, granted)
        await query.answer()
        try:
            await query.edit_message_text(
                "✅ اجازه ثبت در حافظه بلندمدت داده شد. متن پیام‌های شما در صورت "
                "نیاز به مدل هوش مصنوعی ابری (Gemini) ارسال می‌شود تا پاسخ‌ها "
                "دقیق‌تر شوند. هر زمان می‌توانید با /forget_me همه‌چیز را پاک کنید."
                if granted
                else "🛑 درخواست رد شد. متن پیام‌های شما هرگز به مدل هوش مصنوعی "
                "ابری ارسال نخواهد شد و حافظه بلندمدت فعال نیست."
            )
        except Exception:
            # The message may already be uneditable (age/edits limit);
            # the vote itself is persisted regardless.
            pass

    return memory_cmd, forget_me_cmd, consent_callback


async def ensure_consent_prompted(
    engine: AIMemoryEngine,
    enabled: bool,
    user_id: int,
    reply: ReplyFn,
) -> str:
    """P0-7 one-time consent question; returns the current consent state.

    * ``enabled=False`` → no prompt, no egress possible (master kill switch).
    * consent unset & never prompted → show the question once, mark prompted.
    * consent set (granted/denied) or already prompted → silent.

    Callers may egress only when the returned state is ``"granted"``.
    """
    if not enabled:
        return CONSENT_UNSET
    consent = await engine.get_consent(user_id)
    if consent == CONSENT_UNSET and not await engine.has_been_prompted(user_id):
        await engine.mark_prompted(user_id)
        await reply(AIMEMORY_CONSENT_PROMPT, reply_markup=AIMEMORY_CONSENT_KEYBOARD)
    return consent
