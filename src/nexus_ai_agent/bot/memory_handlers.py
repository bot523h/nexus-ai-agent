"""``/memory`` and ``/forget_me`` — the user-facing side of opt-in AI memory.

* ``/memory``           → status + what the bot remembers
* ``/memory on``        → opt in (nothing is analysed before this)
* ``/memory off``       → opt out **and** wipe (same as ``/forget_me``)
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.features.ai_memory import AIMemoryEngine

_ON = frozenset({"on", "روشن", "فعال", "enable", "start"})
_OFF = frozenset({"off", "خاموش", "غیرفعال", "disable", "stop"})

_HELP = (
    "🧠 *حافظه بلندمدت*\n"
    "حافظه فقط با رضایت شما فعال می‌شود؛ تا وقتی روشن نکنید هیچ پیامی تحلیل نمی‌شود.\n\n"
    "`/memory on` — روشن کردن\n"
    "`/memory off` — خاموش کردن و پاک کردن\n"
    "`/memory` — وضعیت و آنچه به خاطر دارم"
)

_engine: AIMemoryEngine | None = None


def _get_engine() -> AIMemoryEngine:
    global _engine
    if _engine is None:
        _engine = AIMemoryEngine()
    return _engine


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show/toggle what the AI remembers about the user."""
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    message = update.message
    engine = _get_engine()
    arg = (context.args[0].lower() if context.args else "").strip()

    if arg in _ON:
        created = await engine.enable(user_id)
        await message.reply_text(
            "✅ حافظه بلندمدت روشن شد. از این پس نکات مهم گفتگوها "
            "(نام، علایق، شغل) را به خاطر می‌سپارم."
            if created
            else "ℹ️ حافظه از قبل روشن بود."
        )
        return
    if arg in _OFF:
        await engine.disable(user_id)
        await message.reply_text("✅ حافظه خاموش و تمام اطلاعات ذخیره‌شده پاک شد.")
        return
    if arg and arg not in {"status", "وضعیت"}:
        await message.reply_text(_HELP, parse_mode="Markdown")
        return

    if not await engine.is_enabled(user_id):
        await message.reply_text(
            "🧠 حافظه بلندمدت *خاموش* است؛ هیچ پیامی تحلیل نمی‌شود.\nبرای فعال‌سازی: `/memory on`",
            parse_mode="Markdown",
        )
        return

    ctx = await engine.get_context(user_id)
    if ctx:
        formatted_ctx = ctx.replace(" | ", "\n")
        await message.reply_text(
            f"🧠 *حافظه روشن است — آنچه من از شما می‌دانم:*\n\n{formatted_ctx}\n\n"
            "برای خاموش کردن و پاک کردن: `/memory off`",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "🧠 حافظه روشن است اما هنوز نکته خاصی ذخیره نشده.\nبرای خاموش کردن: `/memory off`",
            parse_mode="Markdown",
        )


async def forget_me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe user memory (and switch it off)."""
    if not update.effective_user or not update.message:
        return
    await _get_engine().forget_user(update.effective_user.id)
    await update.message.reply_text(
        "✅ تمامی اطلاعات حافظه بلندمدت شما پاک و حافظه خاموش شد. برای فعال‌سازی دوباره: /memory on"
    )
