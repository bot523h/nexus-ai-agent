"""Auto-update system — check, pull, and notify about new versions.

Compares local version with the latest GitHub release,
optionally auto-updates, and sends Telegram notifications.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

REPO_API = "https://api.github.com/repos/bot523h/nexus-ai-agent/releases/latest"
VERSION_FILE = Path(__file__).resolve().parent.parent.parent.parent / "VERSION"


@dataclass
class UpdateInfo:
    """Result of an update check."""

    current_version: str = "0.0.0"
    latest_version: str = "0.0.0"
    update_available: bool = False
    release_notes: str = ""
    release_url: str = ""


class AutoUpdater:
    """Check for updates and optionally auto-update."""

    def __init__(
        self,
        *,
        repo_api: str = REPO_API,
        project_dir: str | None = None,
    ) -> None:
        self._repo_api = repo_api
        self._project_dir = project_dir or str(Path(__file__).resolve().parent.parent.parent.parent)

    @staticmethod
    def get_current_version() -> str:
        """Read the current version from VERSION file or pyproject.toml."""
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text().strip()
        # Fallback: parse pyproject.toml
        pyproject = Path(__file__).resolve().parent.parent.parent.parent / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text().splitlines():
                if line.startswith("version"):
                    return line.split("=")[1].strip().strip('"').strip("'")
        return "0.0.0"

    async def check_update(self) -> UpdateInfo:
        """Check GitHub for the latest release."""
        current = self.get_current_version()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    self._repo_api,
                    headers={"Accept": "application/vnd.github+json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.error("update_check_failed", error=str(exc))
            return UpdateInfo(current_version=current)

        latest = data.get("tag_name", "v0.0.0").lstrip("v")
        return UpdateInfo(
            current_version=current,
            latest_version=latest,
            update_available=latest != current,
            release_notes=data.get("body", "")[:2000],
            release_url=data.get("html_url", ""),
        )

    def perform_update(self) -> bool:
        """Pull latest code from main branch and reinstall."""
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=self._project_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.error("git_pull_failed", stderr=result.stderr)
                return False

            # Reinstall
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", "."],
                cwd=self._project_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error("pip_install_failed", stderr=result.stderr)
                return False

            logger.info("update_completed", version=self.get_current_version())
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("update_failed", error=str(exc))
            return False

    @staticmethod
    def format_update(info: UpdateInfo) -> str:
        """Format update info as a Persian Telegram message."""
        if info.update_available:
            text = (
                f"🆕 **آپدیت جدید موجود!**\n\n"
                f"📦 نسخه فعلی: `{info.current_version}`\n"
                f"🚀 نسخه جدید: `{info.latest_version}`\n\n"
            )
            if info.release_notes:
                text += f"📝 **تغییرات:**\n{info.release_notes[:500]}\n\n"
            text += f"🔗 [دانلود نسخه جدید]({info.release_url})\n\nبرای آپدیت: `/update`"
        else:
            text = f"✅ **شما آخرین نسخه را دارید.**\n\n📦 نسخه: `{info.current_version}`"
        return text
