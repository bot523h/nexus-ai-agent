"""NEXUS AI Telegram Bot — Application builder.

v2.1: All engines (Gemini, Image, Speech, Referral, Cloud, Queue)
are initialized here and passed to handlers via bot_data.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram.ext import Application, ApplicationBuilder

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import (
    build_handlers,
)

logger = get_logger(__name__)


def _build_default_storage(settings: Settings) -> AIStorageManager:
    from pathlib import Path

    return AIStorageManager(
        cache_dir=Path(settings.cache_dir),
        config=ProviderConfig(
            github_token=settings.github_token,
            github_repo=settings.github_repo,
            mega_email=settings.mega_email,
            mega_password=settings.mega_password,
            huggingface_token=settings.huggingface_token,
            rclone_remote=settings.rclone_remote,
            gdrive_bearer_token=settings.gdrive_bearer_token,
            r2_account_id=settings.r2_account_id,
            r2_access_key_id=settings.r2_access_key_id,
            r2_secret_access_key=settings.r2_secret_access_key,
            r2_bucket=settings.r2_bucket,
        ),
    )


def _bot_token(settings: Settings) -> str:
    """Resolve the bot token exactly like ``build_application`` does."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)


def _failure_class_line(status: Any, lang: str) -> str:
    """First line of a failure notification: retryable ≠ terminal (task-181).

    Both are *failure* notifications — never success — and they are visibly
    distinct so a user can tell "transient, may be retried" from "definitive
    failure".  Inline copy (this grandfathered file's convention) so the
    i18n catalog's key parity is untouched.
    """
    if status is JobStatus.FAILED_RETRYABLE:
        head = (
            "⚠️ شکست موقت (قابل تکرار)"
            if lang.startswith("fa")
            else "⚠️ Temporary failure (may be retried)"
        )
    else:
        head = "❌ شکست قطعی" if lang.startswith("fa") else "❌ Terminal failure"
    return head + "\n"
