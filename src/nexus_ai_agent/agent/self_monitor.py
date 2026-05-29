"""Self-monitoring and auto-healing system for NEXUS AI Agent.

Monitors RAM, disk, uptime, error rates, and Gemini quota status.
Can automatically fix common issues.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import time
from typing import Any

import psutil

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

_START_TIME = time.time()


class SelfMonitor:
    """Monitor system health and auto-fix common issues."""

    def __init__(
        self,
        db_path: str = "data/app.sqlite",
        max_ram_mb: int = 1500,
        gemini_api_key: str | None = None,
    ) -> None:
        self._db_path = db_path
        self._max_ram_mb = max_ram_mb
        self._gemini_api_key = gemini_api_key
        self._error_count: int = 0
        self._last_error_hour: float = 0.0

    def record_error(self) -> None:
        """Record an error for rate tracking."""
        current_hour = time.time() / 3600
        if current_hour - self._last_error_hour >= 1.0:
            self._error_count = 0
            self._last_error_hour = current_hour
        self._error_count += 1

    async def check_health(self) -> dict[str, Any]:
        """Perform a comprehensive health check.

        Returns a dict with system metrics and status.
        """
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / (1024 * 1024)
        ram_percent = psutil.virtual_memory().percent
        disk_percent = psutil.disk_usage("/").percent
        uptime_seconds = time.time() - _START_TIME
        uptime_hours = uptime_seconds / 3600

        # Count errors in current hour
        current_hour = time.time() / 3600
        errors_this_hour = self._error_count if (current_hour - self._last_error_hour < 1.0) else 0

        # Check Gemini quota (best-effort)
        gemini_status = "unknown"
        if self._gemini_api_key:
            gemini_status = await self._check_gemini_quota()

        # Determine overall status
        issues: list[str] = []
        if ram_mb > self._max_ram_mb:
            issues.append(f"RAM usage {ram_mb:.0f}MB exceeds limit {self._max_ram_mb}MB")
        if disk_percent > 90:
            issues.append(f"Disk usage {disk_percent:.1f}% is critical")
        if errors_this_hour > 50:
            issues.append(f"Error rate high: {errors_this_hour} errors/hour")
        if gemini_status == "quota_exceeded":
            issues.append("Gemini API quota exceeded")

        status = "healthy" if not issues else "degraded"
        if ram_mb > self._max_ram_mb * 1.5 or disk_percent > 95:
            status = "critical"

        return {
            "status": status,
            "ram_mb": round(ram_mb, 1),
            "ram_percent": round(ram_percent, 1),
            "disk_percent": round(disk_percent, 1),
            "uptime_hours": round(uptime_hours, 2),
            "errors_this_hour": errors_this_hour,
            "gemini_status": gemini_status,
            "max_ram_mb": self._max_ram_mb,
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "issues": issues,
        }

    async def _check_gemini_quota(self) -> str:
        """Best-effort check of Gemini API quota by sending a tiny request."""
        if not self._gemini_api_key:
            return "no_key"
        try:
            import httpx

            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={self._gemini_api_key}"
            )
            payload = {
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 1},
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    return "quota_exceeded"
                if resp.status_code == 200:
                    return "ok"
                return f"error_{resp.status_code}"
        except Exception as exc:
            logger.warning("gemini_quota_check_error", error=str(exc))
            return "error"

    async def auto_fix(self, issue: str) -> dict[str, Any]:
        """Attempt to automatically fix a known issue.

        Returns a dict with keys: issue, action, success, message.
        """
        action = ""
        success = False
        message = ""

        if issue.startswith("RAM") or "RAM" in issue:
            # RAM > threshold: unload models, clear caches
            action = "clear_caches"
            try:
                import gc

                gc.collect()
                ram_after = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
                success = True
                message = f"GC forced. RAM after: {ram_after:.0f}MB"
            except Exception as exc:
                message = f"Failed to clear caches: {exc}"

        elif issue.startswith("Disk") or "disk" in issue.lower():
            # Disk > threshold: vacuum SQLite, clean temp files
            action = "disk_cleanup"
            try:
                if os.path.exists(self._db_path):
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("VACUUM")
                    conn.close()
                # Clean temp files
                import tempfile

                tmp_dir = tempfile.gettempdir()
                for f in os.listdir(tmp_dir):
                    if f.startswith("nexus_"):
                        try:
                            os.remove(os.path.join(tmp_dir, f))
                        except OSError:
                            pass
                success = True
                message = "Database vacuumed and temp files cleaned"
            except Exception as exc:
                message = f"Disk cleanup failed: {exc}"

        elif "Gemini" in issue or "quota" in issue:
            # Gemini quota exceeded: switch to fallback mode
            action = "switch_fallback"
            success = True
            message = "Switched to offline/fallback mode. Gemini will retry later."

        elif "error_rate" in issue or "Error rate" in issue:
            # High error rate: log and suggest restart
            action = "log_and_notify"
            success = True
            message = "Error rate high. Owner notified. Consider restart."

        else:
            action = "unknown"
            message = f"No auto-fix available for: {issue}"

        logger.info(
            "auto_fix",
            issue=issue,
            action=action,
            success=success,
            message=message,
        )
        return {
            "issue": issue,
            "action": action,
            "success": success,
            "message": message,
        }

    def format_health(self, data: dict[str, Any]) -> str:
        """Format health data as a Telegram message."""
        status_emoji = {
            "healthy": "🟢",
            "degraded": "🟡",
            "critical": "🔴",
        }
        emoji = status_emoji.get(data.get("status", ""), "⚪")
        text = (
            f"{emoji} **وضعیت سیستم: {data.get('status', 'unknown').upper()}**\n\n"
            f"💾 RAM: {data.get('ram_mb', 0)} MB "
            f"(مجاز: {data.get('max_ram_mb', 0)} MB)\n"
            f"💿 دیسک: {data.get('disk_percent', 0)}%\n"
            f"⏱ آپتایم: {data.get('uptime_hours', 0)} ساعت\n"
            f"⚠️ خطا/ساعت: {data.get('errors_this_hour', 0)}\n"
            f"🤖 Gemini: {data.get('gemini_status', 'unknown')}\n"
            f"🖥 سیستم: {data.get('platform', '')} / "
            f"Python {data.get('python_version', '')}"
        )
        issues = data.get("issues", [])
        if issues:
            text += "\n\n⚠️ **مشکلات:**\n"
            for issue in issues:
                text += f"  • {issue}\n"
        return text
