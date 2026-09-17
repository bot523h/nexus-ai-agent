from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.updater import AutoUpdater
from nexus_ai_agent.features.owner_control import is_owner


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # In a real app, read from VERSION file
    current_version = "v3.0.0"
    await update.message.reply_text(f"🤖 نسخه فعلی: {current_version}")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    message = update.message

    # Owner-only gate: second, independent layer in front of the approval
    # system (do_update still requires an explicit /approve even for owner).
    if not is_owner(update.effective_user.id if update.effective_user else 0):
        await message.reply_text("⛔ Only the bot owner can trigger updates.")
        return

    current_version = "v3.0.0"
    updater = AutoUpdater(current_version)
    approval_system = ApprovalSystem(bot=context.bot)

    await message.reply_text("🔄 در حال بررسی آپدیت...")
    needed, latest = await updater.check_for_update()

    if not needed:
        await message.reply_text("✨ شما از آخرین نسخه استفاده می‌کنید.")
        return

    await message.reply_text(f"🆕 نسخه جدید یافت شد: {latest}\nدر حال درخواست تایید...")
    success, detail = await updater.do_update(approval_system)
    if success:
        await message.reply_text("✅ آپدیت با موفقیت انجام شد. لطفاً بات را ری‌استارت کنید.")
    else:
        await message.reply_text(f"⏳ {detail}")
