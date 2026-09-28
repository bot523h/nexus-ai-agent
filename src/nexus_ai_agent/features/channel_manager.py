"""Channel & group management for NEXUS AI Telegram bot.

Provides posting, scheduling, moderation, welcome messages, and admin helpers.
All Telegram API calls go through the bot instance stored in the application.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, desc, select
from telegram.error import TelegramError

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ChannelSchedule, User, ViralPost, WelcomeMessage

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for simple CRUD inside features.

    W1 Fix: engine is created per-call and must be disposed by caller.
    This helper is retained for backward compat but new code should use
    runtime-owned session factory. Caller must dispose via engine.dispose().
    """
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


def _session_scope() -> Any:
    """Context manager yielding Session with proper engine dispose."""
    from contextlib import contextmanager

    from sqlmodel import Session

    @contextmanager
    def _cm():
        engine = _sync_engine()
        try:
            with Session(engine) as session:
                yield session
                session.commit()
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

    return _cm()


class ChannelManager:
    """Manages channel/group operations: posts, schedules, bans, welcomes, and autonomous tasks."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._scheduled_tasks: dict[int, asyncio.Task[Any]] = {}
        self._welcome_cache: dict[int, str] = {}
        self._closed = False
        self.channel_id = -1003945319426  # Numerical ID for @nexus_ai_official
        self.viral_engine = ViralEngine(bot)

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ChannelManager")
        return self.bot

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

    async def schedule_post(self, chat_id: int, text: str, when: datetime) -> int:
        """Schedule a post for *when* (UTC). Returns the schedule DB id."""
        # W1 Fix: use _session_scope that disposes engine, and track task for closure
        with _session_scope() as session:
            schedule = ChannelSchedule(
                chat_id=chat_id,
                text=text,
                scheduled_at=when,
                status="pending",
            )
            session.add(schedule)
            session.commit()
            session.refresh(schedule)
            schedule_id: int = schedule.id if schedule.id is not None else 0

        delay = (when - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:

            async def _send() -> None:
                try:
                    await asyncio.sleep(delay)
                    await self.post_to_channel(chat_id, text)
                    with _session_scope() as s2:
                        obj = s2.get(ChannelSchedule, schedule_id)
                        if obj is not None:
                            obj.status = "sent"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("channel_schedule_send_failed", schedule_id=schedule_id)

            task = asyncio.create_task(_send())
            self._scheduled_tasks[schedule_id] = task
            # Auto-remove on completion to avoid unbounded dict growth
            def _done(_t: asyncio.Task[Any], sid: int = schedule_id) -> None:
                self._scheduled_tasks.pop(sid, None)

            task.add_done_callback(_done)

        return schedule_id

    async def aclose(self) -> None:
        """W1 True Runtime Closure: cancel all scheduled tasks and join (fail-safe)."""
        if self._closed:
            return
        self._closed = True
        tasks = list(self._scheduled_tasks.values())
        self._scheduled_tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        """Sync best-effort close (legacy). Schedules async close if loop running."""
        if self._closed:
            return
        self._closed = True
        tasks = list(self._scheduled_tasks.values())
        self._scheduled_tasks.clear()
        for t in tasks:
            t.cancel()
        # Best-effort: if loop running, schedule gather, else ignore
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(asyncio.gather(*tasks, return_exceptions=True))
        except RuntimeError:
            pass

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

    def set_welcome_message(self, chat_id: int, text: str) -> None:
        """Store a welcome message for *chat_id* (in DB + cache)."""
        self._welcome_cache[chat_id] = text
        with _session_scope() as session:
            existing = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if existing is not None:
                existing.text = text
            else:
                session.add(WelcomeMessage(chat_id=chat_id, text=text))

    def get_welcome_message(self, chat_id: int) -> str:
        """Return the welcome message for *chat_id*, or empty string."""
        if chat_id in self._welcome_cache:
            return self._welcome_cache[chat_id]
        with _session_scope() as session:
            obj = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if obj is not None:
                self._welcome_cache[chat_id] = obj.text
                return obj.text
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

    # ── Autonomous Channel Management (v3.4.0) ───────────────────

    async def post_top_users(self) -> bool:
        """Fetch top 10 users by XP/activity and post to channel."""
        bot = self._require_bot()
        with _session_scope() as session:
            stmt = select(User).order_by(desc(User.id)).limit(10)
            users = session.exec(stmt).all()

            if not users:
                return False

            text = "🏆 **برترین کاربران ۲۴ ساعت گذشته**\n\n"
            for i, user in enumerate(users, 1):
                username = f"@{user.username}" if user.username else f"User {user.telegram_id}"
                text += f"{i}. {username}\n"

            text += "\n🚀 شما هم می‌توانید با فعالیت در ربات به لیست برترین‌ها اضافه شوید!"

            try:
                await bot.send_message(chat_id=self.channel_id, text=text, parse_mode="Markdown")
                return True
            except TelegramError as e:
                logger.error(f"Failed to post top users: {e}")
                return False

    async def post_viral_content(self) -> int:
        """Post pending viral content to channel."""
        bot = self._require_bot()
        with _session_scope() as session:
            stmt = (
                select(ViralPost)
                .where(ViralPost.status == "pending")
                .order_by(desc(ViralPost.viral_score))
                .limit(10)
            )
            posts = session.exec(stmt).all()

            count = 0
            for post in posts:
                try:
                    await bot.send_message(chat_id=self.channel_id, text=post.text)
                    post.status = "posted"
                    post.posted_at = datetime.now(timezone.utc)
                    session.add(post)
                    count += 1
                except TelegramError as e:
                    logger.error(f"Failed to post viral content {post.id}: {e}")
                    post.status = "failed"
                    session.add(post)

            return count

    async def run_nightly_tasks(self) -> None:
        """Main entry point for nightly automation."""
        logger.info("Starting nightly channel management tasks...")

        # 1. Post Top Users
        await self.post_top_users()

        # 2. Generate new viral content if needed
        await self.viral_engine.generate_and_schedule(self.channel_id, count=10)

        # 3. Post viral content
        await self.post_viral_content()

        logger.info("Nightly tasks completed.")
