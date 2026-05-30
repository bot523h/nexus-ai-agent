from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.self_monitor import SelfMonitor
from nexus_ai_agent.features.owner_control import is_owner


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    monitor = SelfMonitor(bot=context.bot)
    status = await monitor.check_health()

    response = (
        "🏥 وضعیت سلامت سیستم:\n\n"
        f"🐏 مصرف رم: {status['ram_mb']:.1f} MB ({status['ram_percent']:.1f}%)\n"
        f"💽 فضای دیسک آزاد: {status['disk_free_gb']:.1f} GB\n"
        f"⏱ آپ‌تایم: {status['uptime_hours']:.1f} ساعت\n"
        f"✅ وضعیت کلی: {'سالم' if status['healthy'] else 'نیازمند بررسی'}"
    )
    await update.message.reply_text(response)


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a pending action (Owner only)."""
    if not is_owner(update.effective_user.id if update.effective_user else 0):
        return
    
    if not context.args:
        await update.message.reply_text("❌ استفاده: /approve <id>")
        return
    
    try:
        approval_id = int(context.args[0])
        system = ApprovalSystem(bot=context.bot)
        success = await system.approve(approval_id)
        if success:
            await update.message.reply_text(f"✅ درخواست {approval_id} تایید شد.")
        else:
            await update.message.reply_text(f"❌ درخواست {approval_id} یافت نشد یا قبلاً بررسی شده است.")
    except ValueError:
        await update.message.reply_text("❌ شناسه باید عدد باشد.")


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject a pending action (Owner only)."""
    if not is_owner(update.effective_user.id if update.effective_user else 0):
        return
    
    if not context.args:
        await update.message.reply_text("❌ استفاده: /reject <id>")
        return
    
    try:
        approval_id = int(context.args[0])
        system = ApprovalSystem(bot=context.bot)
        success = await system.reject(approval_id)
        if success:
            await update.message.reply_text(f"❌ درخواست {approval_id} رد شد.")
        else:
            await update.message.reply_text(f"❌ درخواست {approval_id} یافت نشد یا قبلاً بررسی شده است.")
    except ValueError:
        await update.message.reply_text("❌ شناسه باید عدد باشد.")
