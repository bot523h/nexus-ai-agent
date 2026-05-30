from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.features.ai_memory import AIMemoryEngine


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what the AI remembers about the user."""
    user_id = update.effective_user.id
    engine = AIMemoryEngine()
    ctx = await engine.get_context(user_id)
    
    if ctx:
        await update.message.reply_text(
            f"🧠 *آنچه من از شما می‌دانم:*\n\n{ctx.replace(' | ', '\n')}\n\n"
            "این اطلاعات به من کمک می‌کند تا پاسخ‌های دقیق‌تری به شما بدهم.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("🧠 من هنوز اطلاعات خاصی از شما در حافظه بلندمدتم ندارم.")


async def forget_me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe user memory."""
    user_id = update.effective_user.id
    engine = AIMemoryEngine()
    await engine.forget_user(user_id)
    await update.message.reply_text("✅ تمامی اطلاعات حافظه بلندمدت شما پاک شد.")
