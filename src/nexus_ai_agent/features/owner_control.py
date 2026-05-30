"""Owner control system for NEXUS AI Telegram bot.

Restricts sensitive commands to the bot owner. Provides decorator-based
protection, admin logging, broadcast capability, and system status reporting.
All protected commands (ads, broadcasts, analytics, forcejoin, moderation
override, channel automation, viral posting, campaign management) are gated
through the owner_only decorator.
"""

from __future__ import annotations

import functools
import platform
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import AdminLog

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Singleton owner ID — cached after first read from settings
# ---------------------------------------------------------------------------

_owner_id: int | None = None


def _get_owner_id() -> int:
    """Return the configured owner Telegram user ID."""
    global _owner_id  # noqa: PLW0603
    if _owner_id is None:
        _owner_id = get_settings().owner_telegram_id
    return _owner_id


# ═══════════════════════════════════════════════════════════════════════
# Core owner control
# ═══════════════════════════════════════════════════════════════════════


def is_owner(user_id: int) -> bool:
    """Check whether *user_id* is the bot owner."""
    return user_id == _get_owner_id()


def owner_only(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: only allow the owner to execute the wrapped handler.

    If an unauthorized user triggers the command, the handler returns
    ``"⛔ Access denied"`` instead of executing.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Extract user_id from Update (first positional arg in PTB handlers)
        update = kwargs.get("update") or (args[0] if args else None)
        user_id = 0
        if update is not None and hasattr(update, "effective_user") and update.effective_user:
            user_id = update.effective_user.id
        if not is_owner(user_id):
            if update is not None and hasattr(update, "effective_chat") and update.effective_chat:
                await update.effective_chat.send_message("⛔ Access denied")
            return None
        return await func(*args, **kwargs)

    return wrapper


# ═══════════════════════════════════════════════════════════════════════
# Admin logging
# ═══════════════════════════════════════════════════════════════════════


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


def log_admin_action(
    user_id: int,
    action: str,
    target: str = "",
    details: str = "",
) -> None:
    """Persist an admin action to the ``admin_logs`` table."""
    engine = _sync_engine()
    with Session(engine) as session:
        entry = AdminLog(
            user_id=user_id,
            action=action,
            target=target,
            details=details,
        )
        session.add(entry)
        session.commit()


class OwnerControl:
    """High-level owner control API used by command handlers."""

    # -------------------------------------------------------------------
    # System status
    # -------------------------------------------------------------------

    @staticmethod
    def system_status() -> str:
        """Return a formatted system status string for the owner."""
        return (
            "🖥️ **System Status**\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🐍 Python: {sys.version.split()[0]}\n"
            f"🖥️ OS: {platform.system()} {platform.release()}\n"
            f"⚡ CPU: {platform.machine()}\n"
            f"🕐 UTC: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}\n"
            f"👤 Owner ID: {_get_owner_id()}\n"
            f"🔑 Bot version: 3.4.0\n"
        )

    # -------------------------------------------------------------------
    # Broadcast
    # -------------------------------------------------------------------

    @staticmethod
    async def owner_broadcast(bot: Any, chat_ids: list[int], text: str) -> dict[str, int]:
        """Broadcast *text* to every chat in *chat_ids*.

        Returns ``{"success": n, "failed": m}``.
        """
        success = 0
        failed = 0
        for cid in chat_ids:
            try:
                await bot.send_message(chat_id=cid, text=text)
                success += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.warning("broadcast_failed", chat_id=cid)
        log_admin_action(
            user_id=_get_owner_id(),
            action="broadcast",
            target=f"{len(chat_ids)} chats",
            details=f"success={success} failed={failed}",
        )
        return {"success": success, "failed": failed}

    # -------------------------------------------------------------------
    # Admin logs retrieval
    # -------------------------------------------------------------------

    @staticmethod
    def admin_logs(limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent admin log entries."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AdminLog).order_by(col(AdminLog.id).desc()).limit(limit)
            results = session.exec(stmt).all()
            return [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "action": r.action,
                    "target": r.target,
                    "details": r.details,
                    "timestamp": str(r.timestamp),
                }
                for r in results
            ]

    # -------------------------------------------------------------------
    # Protected command check
    # -------------------------------------------------------------------

    @staticmethod
    def protected_command(user_id: int) -> bool:
        """Return True if *user_id* is allowed to run protected commands.

        Currently this is synonymous with ``is_owner`` but the indirection
        allows future expansion to multiple admins.
        """
        return is_owner(user_id)
