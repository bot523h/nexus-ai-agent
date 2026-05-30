from telegram import Update
from telegram.ext import ContextTypes
from nexus_ai_agent.agent.self_monitor import SelfMonitor

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    monitor = SelfMonitor()
    status = await monitor.check_health()
    
    response = (
        "🏥 وضعیت سلامت سیستم:\n\n"
        f"🐏 مصرف رم: {status['ram_mb']:.1f} MB ({status['ram_percent']:.1f}%)\n"
        f"💽 فضای دیسک آزاد: {status['disk_free_gb']:.1f} GB\n"
        f"⏱ آپ‌تایم: {status['uptime_hours']:.1f} ساعت\n"
        f"✅ وضعیت کلی: {'سالم' if status['healthy'] else 'نیازمند بررسی'}"
    )
    await update.message.reply_text(response)
