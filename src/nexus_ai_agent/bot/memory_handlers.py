from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from nexus_ai_agent.features.ai_memory import CONSENT_UNSET, AIMemoryEngine

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]
ReplyFn = Callable[..., Awaitable[None]]

# ── P0-7: one-time consent question for LLM egress ─────────────────────
# Policy A (docs/DECISION_LOG.md 2026-09-21 P0-7): the consent gates the
# AI-memory feature (extraction/storage of personal facts) only.  Explicitly
# requested AI commands (/ai and chat answers) send message text to the cloud
# model by product design and are NOT gated here — the copy below must say so,
# never promise a global "nothing leaves this device" it cannot keep.
AIMEMORY_CONSENT_PROMPT = (
    "🔐 **رضایت برای حافظه هوشمند**\n\n"
    "ویژگی «حافظه هوشمند» می‌تواند اطلاعات مهم را از پیام‌های شما استخراج و در "
    "حافظهٔ بلندمدت شما ذخیره کند تا پاسخ‌ها دقیق‌تر شوند. فقط برای همین "
    "استخراجِ حافظه، متن پیام‌های شما در صورت نیاز به یک مدل هوش مصنوعی ابری "
    "(Google Gemini) ارسال می‌شود.\n\n"
    "آیا موافق هستید؟ تا زمانی که تأیید نکنید، هیچ متنی برای استخراج حافظه به "
    "بیرون ارسال نمی‌شود. توجه: فرمان‌های صریح هوش مصنوعی (مانند /ai) همچنان "
    "پیام‌های شما را برای تولید پاسخ به مدل ابری می‌فرستند. حافظهٔ شخصی فقط در "
    "گفتگوهای خصوصی به پاسخ‌ها اضافه می‌شود و هرگز بخشی از پاسخ‌های گروهی "
    "نیست. هر زمان می‌توانید تصمیم خود را تغییر دهید."
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
                "✅ اجازهٔ استخراج حافظه داده شد. متن پیام‌های شما در صورت نیاز "
                "برای استخراج و ذخیره در حافظهٔ بلندمدت به مدل هوش مصنوعی ابری "
                "(Gemini) ارسال می‌شود تا پاسخ‌ها دقیق‌تر شوند. هر زمان "
                "می‌توانید با /forget_me همه‌چیز را پاک کنید."
                if granted
                else "🛑 درخواست رد شد. استخراج حافظهٔ هوشمند غیرفعال می‌ماند و "
                "متن پیام‌های شما برای حافظه به مدل هوش مصنوعی ابری ارسال "
                "نخواهد شد. پاسخ‌های درخواست‌شده با فرمان‌های هوش مصنوعی "
                "(مانند /ai) طبق روال قبلی کار می‌کنند."
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
