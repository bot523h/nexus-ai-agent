from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.self_monitor import SelfMonitor
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
    # system (perform_update still requires an explicit /approve even for
    # the owner) and of the settings.auto_update switch.
    if not is_owner(update.effective_user.id if update.effective_user else 0):
        await message.reply_text("⛔ Only the bot owner can trigger updates.")
        return

    current_version = "v3.0.0"
    monitor = SelfMonitor(bot=context.bot)
    approval_system = ApprovalSystem(bot=context.bot)

    await message.reply_text("🔄 در حال بررسی آپدیت...")
    check = await monitor.check_for_update(current_version)
    if check is None:
        await message.reply_text("⛔ Self-update is disabled. Set AUTO_UPDATE=true to enable it.")
        return
    needed, latest = check

    if not needed:
        await message.reply_text("✨ شما از آخرین نسخه استفاده می‌کنید.")
        return

    await message.reply_text(f"🆕 نسخه جدید یافت شد: {latest}\nدر حال درخواست تایید...")
    success, detail = await monitor.perform_update(current_version, approval_system)
    if success:
        await message.reply_text("✅ آپدیت با موفقیت انجام شد. لطفاً بات را ری‌استارت کنید.")
    else:
        await message.reply_text(f"⏳ {detail}")
