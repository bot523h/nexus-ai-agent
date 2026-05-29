"""Channel & group management for NEXUS AI Telegram bot.

Provides posting, scheduling, moderation, welcome messages, and admin helpers.
All Telegram API calls go through the bot instance stored in the application.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ChannelSchedule, WelcomeMessage

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for simple CRUD inside features."""
    settings = get_settings()
    from sqlalchemy import create_engine as _ce  # noqa: WPS433

    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class ChannelManager:
    """Manages channel/group operations: posts, schedules, bans, welcomes."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._scheduled_tasks: dict[int, asyncio.Task[Any]] = {}  # schedule_id → task
        self._welcome_cache: dict[int, str] = {}  # chat_id → welcome text

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ChannelManager")
        return self.bot

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    async def post_to_channel(self, chat_id: int, text: str, *, pin: bool = False) -> Any:
        """Send *text* to *chat_id* and optionally pin it."""
        bot = self._require_bot()
        msg = await bot.send_message(chat_id=chat_id, text=text)
        if pin and msg is not None:
            await self.pin_message(chat_id, msg.message_id)
        return msg

    async def pin_message(self, chat_id: int, message_id: int) -> None:
        """Pin a message in a chat."""
        bot = self._require_bot()
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message from a chat."""
        bot = self._require_bot()
        await bot.delete_message(chat_id=chat_id, message_id=message_id)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    async def schedule_post(self, chat_id: int, text: str, when: datetime) -> int:
        """Schedule a post for *when* (UTC). Returns the schedule DB id."""
        engine = _sync_engine()
        with Session(engine) as session:
            schedule = ChannelSchedule(
                chat_id=chat_id,
                text=text,
                scheduled_at=when,
                status="pending",
            )
            session.add(schedule)
            session.commit()
            session.refresh(schedule)
            schedule_id = schedule.id  # type: ignore[assignment]

        delay = (when - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:

            async def _send() -> None:
                await asyncio.sleep(delay)
                await self.post_to_channel(chat_id, text)
                engine2 = _sync_engine()
                with Session(engine2) as s2:
                    obj = s2.get(ChannelSchedule, schedule_id)
                    if obj is not None:
                        obj.status = "sent"  # type: ignore[union-attr]
                        s2.commit()

            task = asyncio.create_task(_send())
            self._scheduled_tasks[schedule_id] = task

        return int(schedule_id)

    # ------------------------------------------------------------------
    # Moderation
    # ------------------------------------------------------------------

    async def ban_user(self, chat_id: int, user_id: int, *, reason: str = "") -> bool:
        """Ban *user_id* from *chat_id*. Returns True on success."""
        bot = self._require_bot()
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            logger.info("ban_user", chat_id=chat_id, user_id=user_id, reason=reason)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("ban_user_failed", chat_id=chat_id, user_id=user_id)
            return False

    async def unban_user(self, chat_id: int, user_id: int) -> bool:
        """Unban *user_id* from *chat_id*."""
        bot = self._require_bot()
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("unban_user_failed", chat_id=chat_id, user_id=user_id)
            return False

    # ------------------------------------------------------------------
    # Group info
    # ------------------------------------------------------------------

    async def get_members_count(self, chat_id: int) -> int:
        """Return the member count for *chat_id*."""
        bot = self._require_bot()
        return await bot.get_chat_member_count(chat_id=chat_id)

    async def get_admins(self, chat_id: int) -> list[dict[str, Any]]:
        """Return a list of admin dicts for *chat_id*."""
        bot = self._require_bot()
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        return [
            {"user_id": a.user.id, "username": a.user.username, "status": a.status} for a in admins
        ]

    # ------------------------------------------------------------------
    # Welcome messages
    # ------------------------------------------------------------------

    def set_welcome_message(self, chat_id: int, text: str) -> None:
        """Store a welcome message for *chat_id* (in DB + cache)."""
        self._welcome_cache[chat_id] = text
        engine = _sync_engine()
        with Session(engine) as session:
            existing = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if existing is not None:
                existing.text = text  # type: ignore[union-attr]
            else:
                session.add(WelcomeMessage(chat_id=chat_id, text=text))
            session.commit()

    def get_welcome_message(self, chat_id: int) -> str:
        """Return the welcome message for *chat_id*, or empty string."""
        if chat_id in self._welcome_cache:
            return self._welcome_cache[chat_id]
        engine = _sync_engine()
        with Session(engine) as session:
            obj = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if obj is not None:
                self._welcome_cache[chat_id] = obj.text  # type: ignore[union-attr]
                return obj.text  # type: ignore[return-value]
        return ""

    async def welcome_new_member(self, chat_id: int, user_name: str) -> str | None:
        """Send welcome if configured. Returns the welcome text or None."""
        text = self.get_welcome_message(chat_id)
        if not text:
            return None
        formatted = text.replace("{name}", user_name)
        bot = self._require_bot()
        await bot.send_message(chat_id=chat_id, text=formatted)
        return formatted
