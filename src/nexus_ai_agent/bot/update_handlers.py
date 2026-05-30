from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.agent.updater import AutoUpdater


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # In a real app, read from VERSION file
    current_version = "v3.0.0"
    await update.message.reply_text(f"🤖 نسخه فعلی: {current_version}")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    current_version = "v3.0.0"
    updater = AutoUpdater(current_version)

    await update.message.reply_text("🔄 در حال بررسی آپدیت...")
    needed, latest = await updater.check_for_update()

    if needed:
        await update.message.reply_text(f"🆕 نسخه جدید یافت شد: {latest}\nدر حال آپدیت...")
        success = await updater.do_update()
        if success:
            await update.message.reply_text(
                "✅ آپدیت با موفقیت انجام شد. لطفاً بات را ری‌استارت کنید."
            )
        else:
            await update.message.reply_text("❌ خطا در فرآیند آپدیت.")
    else:
        await update.message.reply_text("✨ شما از آخرین نسخه استفاده می‌کنید.")
