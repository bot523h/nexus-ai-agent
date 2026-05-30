from __future__ import annotations

import gc
import logging
import os
import time
from typing import Any

import psutil  # type: ignore
from telegram import Bot

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.instrumentation import instrumented

logger = logging.getLogger(__name__)


class SelfMonitor:
    """System health monitor with instrumentation and owner notifications."""

    def __init__(self, bot: Bot | None = None, max_ram_mb: int = 1500) -> None:
        self.bot = bot
        self.settings = get_settings()
        self.max_ram_mb = max_ram_mb or self.settings.max_ram_mb
        self.start_time = time.time()

    @instrumented("agent.monitor.health")
    async def check_health(self) -> dict[str, Any]:
        """Check system health status and notify owner if critical."""
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / (1024 * 1024)
        disk = psutil.disk_usage("/")
        uptime = time.time() - self.start_time

        ram_percent = (ram_mb / self.max_ram_mb) * 100
        status = {
            "ram_mb": ram_mb,
            "ram_percent": ram_percent,
            "disk_free_gb": disk.free / (1024**3),
            "uptime_hours": uptime / 3600,
            "healthy": ram_mb < self.max_ram_mb,
        }

        # Notify owner if RAM is high (> 80%)
        if ram_percent > 80 and self.bot and self.settings.owner_telegram_id:
            try:
                await self.bot.send_message(
                    chat_id=self.settings.owner_telegram_id,
                    text=f"⚠️ *هشدار مصرف منابع*\nمصرف رم: {ram_percent:.1f}% ({ram_mb:.1f} MB)",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to notify owner of high RAM: {e}")

        return status

    @instrumented("agent.monitor.autofix")
    async def auto_fix(self, issue: str) -> None:
        """Basic auto-fix logic and notify owner."""
        logger.info(f"Attempting to fix: {issue}")

        if self.bot and self.settings.owner_telegram_id:
            try:
                await self.bot.send_message(
                    chat_id=self.settings.owner_telegram_id,
                    text=f"🔧 *تلاش برای رفع خودکار*\nمورد: {issue}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to notify owner of auto-fix: {e}")

        if "RAM" in issue:
            gc.collect()
        elif "DB" in issue:
            pass
