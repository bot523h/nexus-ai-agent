"""Channel and group operations for the NEXUS AI Telegram bot.

User-facing commands post to the chat they were issued in. Scheduled posts are
persisted and restored after a restart. The consistency goal matches ads:
**at most once per process**. A pending row is moved to ``sending`` before the
Telegram call, so a second restore cannot deliver it again. A crash during
send can skip the post; it cannot duplicate it.

Nightly autonomous helpers (top users, viral posts) keep their previous
behaviour and now share the cached engine instead of opening a new one per call.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlmodel import Session, col, desc, select
from telegram.error import TelegramError

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ChannelSchedule, User, ViralPost, WelcomeMessage

logger = get_logger(__name__)

MAX_POST_CHARS = 3500
MAX_WELCOME_CHARS = 1000
MAX_PENDING_PER_CHAT = 50
MAX_SCHEDULE_HORIZON = timedelta(days=30)
MAX_PAST_SKEW = timedelta(seconds=120)
SEND_TIMEOUT_SECONDS = 30.0


class ChannelValidationError(ValueError):
    """User-facing validation failure. The message is safe to reply with."""


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def parse_schedule_when(raw_date: str, raw_time: str, *, now: datetime) -> datetime:
    """Parse ``YYYY-MM-DD HH:MM`` as UTC. Raises :class:`ChannelValidationError`."""
    token = f"{raw_date.strip()} {raw_time.strip()}"
    try:
        parsed = datetime.strptime(token, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ChannelValidationError(
            "❌ فرمت زمان نامعتبر است. مثال: /schedule 2026-09-23 18:30 متن"
        ) from exc
    when = parsed.replace(tzinfo=timezone.utc)
    if when < now - MAX_PAST_SKEW:
        raise ChannelValidationError("❌ زمان زمان‌بندی در گذشته است.")
    if when > now + MAX_SCHEDULE_HORIZON:
        raise ChannelValidationError("❌ زمان‌بندی بیشتر از ۳۰ روز مجاز نیست.")
    return when


def _validate_text(text: str, *, limit: int, empty_message: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ChannelValidationError(empty_message)
    if len(cleaned) > limit:
        raise ChannelValidationError(f"❌ متن حداکثر {limit} نویسه است.")
    return cleaned


class ChannelManager:
    """Posts, schedules, bans, and welcomes for one bot process."""

    def __init__(self, bot: Any | None = None, db_path: str | None = None) -> None:
        self.bot = bot
        self._db_path = db_path
        self._engine: Any | None = None
        self._lock = threading.Lock()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._welcome_cache: dict[int, str] = {}
        # Numerical id used only by the pre-existing nightly helpers.
        self.channel_id = -1003945319426
        self.viral_engine = ViralEngine(bot)

    def bind(self, bot: Any) -> None:
        self.bot = bot
        self.viral_engine.bot = bot

    @property
    def is_bound(self) -> bool:
        return self.bot is not None

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ChannelManager")
        return self.bot

    def _engine_ref(self) -> Any:
        if self._engine is None:
            path = self._db_path or get_settings().db_path
            self._engine = create_engine(
                f"sqlite:///{path}",
                echo=False,
                connect_args={"check_same_thread": False},
            )
        return self._engine

    def close(self) -> None:
        """Cancel scheduled tasks and dispose the engine."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    async def shutdown(self) -> None:
        """Cancel scheduled tasks and wait, then dispose the engine."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    async def post_to_channel(self, chat_id: int, text: str, *, pin: bool = False) -> Any:
        cleaned = _validate_text(text, limit=MAX_POST_CHARS, empty_message="❌ متن پیام خالی است.")
        bot = self._require_bot()
        msg = await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=cleaned),
            timeout=SEND_TIMEOUT_SECONDS,
        )
        if pin and msg is not None:
            await self.pin_message(chat_id, int(msg.message_id))
        return msg

    async def pin_message(self, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            raise ChannelValidationError("❌ شناسه پیام نامعتبر است.")
        bot = self._require_bot()
        await asyncio.wait_for(
            bot.pin_chat_message(chat_id=chat_id, message_id=message_id),
            timeout=SEND_TIMEOUT_SECONDS,
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        bot = self._require_bot()
        await bot.delete_message(chat_id=chat_id, message_id=message_id)

    async def schedule_post(self, chat_id: int, text: str, when: datetime) -> int:
        cleaned = _validate_text(text, limit=MAX_POST_CHARS, empty_message="❌ متن پیام خالی است.")
        when = _as_utc(when)
        schedule_id = await asyncio.to_thread(self._insert_schedule_sync, chat_id, cleaned, when)
        delay = (when - datetime.now(timezone.utc)).total_seconds()
        self._schedule(schedule_id, chat_id, cleaned, max(delay, 0.0))
        return schedule_id

    def _insert_schedule_sync(self, chat_id: int, text: str, when: datetime) -> int:
        with self._lock, Session(self._engine_ref()) as session:
            pending = session.exec(
                select(ChannelSchedule).where(
                    ChannelSchedule.chat_id == chat_id,
                    ChannelSchedule.status == "pending",
                )
            ).all()
            if len(pending) >= MAX_PENDING_PER_CHAT:
                raise ChannelValidationError(
                    f"❌ سقف {MAX_PENDING_PER_CHAT} پست زمان‌بندی‌شده برای این چت پر شده است."
                )
            schedule = ChannelSchedule(
                chat_id=chat_id,
                text=text,
                scheduled_at=when,
                status="pending",
            )
            session.add(schedule)
            session.commit()
            session.refresh(schedule)
            return schedule.id if schedule.id is not None else 0

    async def cancel_schedule(self, schedule_id: int, *, chat_id: int | None = None) -> bool:
        cancelled = await asyncio.to_thread(self._cancel_schedule_sync, schedule_id, chat_id)
        if not cancelled:
            return False
        task = self._tasks.pop(schedule_id, None)
        if task is not None and not task.done():
            task.cancel()
        return True

    def _cancel_schedule_sync(self, schedule_id: int, chat_id: int | None) -> bool:
        with self._lock, Session(self._engine_ref()) as session:
            obj = session.get(ChannelSchedule, schedule_id)
            if obj is None or obj.status != "pending":
                return False
            if chat_id is not None and obj.chat_id != chat_id:
                return False
            obj.status = "cancelled"
            session.add(obj)
            session.commit()
            return True

    def list_pending(self, chat_id: int) -> list[dict[str, Any]]:
        with Session(self._engine_ref()) as session:
            rows = session.exec(
                select(ChannelSchedule)
                .where(ChannelSchedule.chat_id == chat_id, ChannelSchedule.status == "pending")
                .order_by(col(ChannelSchedule.scheduled_at))
            ).all()
        return [
            {
                "id": row.id,
                "text": row.text[:80],
                "scheduled_at": _as_utc(row.scheduled_at).isoformat(),
                "status": row.status,
            }
            for row in rows
        ]

    async def restore_pending(self) -> int:
        """Reschedule persisted pending posts. Already-running ids are skipped."""
        now = datetime.now(timezone.utc)
        rows = await asyncio.to_thread(self._pending_rows_sync)
        restored = 0
        for schedule_id, chat_id, text, when in rows:
            existing = self._tasks.get(schedule_id)
            if existing is not None and not existing.done():
                continue
            delay = (when - now).total_seconds()
            self._schedule(schedule_id, chat_id, text, max(delay, 0.0))
            restored += 1
        if restored:
            logger.info("channel_schedules_restored", count=restored)
        return restored

    def _pending_rows_sync(self) -> list[tuple[int, int, str, datetime]]:
        with Session(self._engine_ref()) as session:
            rows = session.exec(
                select(ChannelSchedule).where(ChannelSchedule.status == "pending")
            ).all()
            return [
                (row.id, row.chat_id, row.text, _as_utc(row.scheduled_at))
                for row in rows
                if row.id is not None
            ]

    def _schedule(self, schedule_id: int, chat_id: int, text: str, delay_seconds: float) -> None:
        old = self._tasks.get(schedule_id)
        if old is not None and not old.done():
            old.cancel()
        self._tasks[schedule_id] = asyncio.create_task(
            self._fire(schedule_id, chat_id, text, delay_seconds)
        )

    async def _fire(self, schedule_id: int, chat_id: int, text: str, delay_seconds: float) -> None:
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            claimed = await asyncio.to_thread(self._claim_schedule_sync, schedule_id)
            if not claimed:
                return
            await self._deliver_scheduled(schedule_id, chat_id, text)
        except asyncio.CancelledError:
            raise
        finally:
            self._tasks.pop(schedule_id, None)

    def _claim_schedule_sync(self, schedule_id: int) -> bool:
        with self._lock, Session(self._engine_ref()) as session:
            obj = session.get(ChannelSchedule, schedule_id)
            if obj is None or obj.status != "pending":
                return False
            obj.status = "sending"
            session.add(obj)
            session.commit()
            return True

    async def _deliver_scheduled(self, schedule_id: int, chat_id: int, text: str) -> None:
        status = "failed"
        try:
            bot = self._require_bot()
            await asyncio.wait_for(
                bot.send_message(chat_id=chat_id, text=text),
                timeout=SEND_TIMEOUT_SECONDS,
            )
            status = "sent"
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_post_failed", schedule_id=schedule_id)
        await asyncio.to_thread(self._mark_schedule_sync, schedule_id, status)

    def _mark_schedule_sync(self, schedule_id: int, status: str) -> None:
        try:
            with Session(self._engine_ref()) as session:
                obj = session.get(ChannelSchedule, schedule_id)
                if obj is not None:
                    obj.status = status
                    session.add(obj)
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("schedule_status_update_failed", schedule_id=schedule_id)

    async def ban_user(self, chat_id: int, user_id: int, *, reason: str = "") -> bool:
        if user_id <= 0:
            return False
        bot = self._require_bot()
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            logger.info("ban_user", chat_id=chat_id, user_id=user_id, reason=reason[:200])
            return True
        except Exception:  # noqa: BLE001
            logger.exception("ban_user_failed", chat_id=chat_id, user_id=user_id)
            return False

    async def unban_user(self, chat_id: int, user_id: int) -> bool:
        if user_id <= 0:
            return False
        bot = self._require_bot()
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("unban_user_failed", chat_id=chat_id, user_id=user_id)
            return False

    async def get_members_count(self, chat_id: int) -> int:
        bot = self._require_bot()
        return int(await bot.get_chat_member_count(chat_id=chat_id))

    async def get_admins(self, chat_id: int) -> list[dict[str, Any]]:
        bot = self._require_bot()
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        return [
            {"user_id": admin.user.id, "username": admin.user.username, "status": admin.status}
            for admin in admins
        ]

    def set_welcome_message(self, chat_id: int, text: str) -> None:
        cleaned = _validate_text(
            text, limit=MAX_WELCOME_CHARS, empty_message="❌ متن خوشامد خالی است."
        )
        self._welcome_cache[chat_id] = cleaned
        with Session(self._engine_ref()) as session:
            existing = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if existing is not None:
                existing.text = cleaned
                session.add(existing)
            else:
                session.add(WelcomeMessage(chat_id=chat_id, text=cleaned))
            session.commit()

    def clear_welcome_message(self, chat_id: int) -> None:
        self._welcome_cache.pop(chat_id, None)
        with Session(self._engine_ref()) as session:
            existing = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if existing is not None:
                session.delete(existing)
                session.commit()

    def get_welcome_message(self, chat_id: int) -> str:
        cached = self._welcome_cache.get(chat_id)
        if cached is not None:
            return cached
        with Session(self._engine_ref()) as session:
            obj = session.exec(
                select(WelcomeMessage).where(WelcomeMessage.chat_id == chat_id)
            ).first()
            if obj is not None and obj.text:
                self._welcome_cache[chat_id] = obj.text
                return obj.text
        return ""

    async def welcome_new_member(self, chat_id: int, user_name: str) -> str | None:
        """Send the stored welcome. Returns the text, or None if none is stored.

        ``{name}`` is substituted by replacement, never by ``str.format``, so a
        welcome cannot evaluate other placeholders.
        """
        text = self.get_welcome_message(chat_id)
        if not text:
            return None
        safe_name = user_name.replace("\n", " ").strip()[:64] or "friend"
        formatted = text.replace("{name}", safe_name)
        bot = self._require_bot()
        await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=formatted),
            timeout=SEND_TIMEOUT_SECONDS,
        )
        return formatted

    # ── Autonomous channel helpers (pre-existing nightly path) ──────────

    async def post_top_users(self) -> bool:
        bot = self._require_bot()
        with Session(self._engine_ref()) as session:
            users = session.exec(select(User).order_by(desc(User.id)).limit(10)).all()
            if not users:
                return False
            text = "🏆 **برترین کاربران ۲۴ ساعت گذشته**\n\n"
            for index, user in enumerate(users, 1):
                username = f"@{user.username}" if user.username else f"User {user.telegram_id}"
                text += f"{index}. {username}\n"
            text += "\n🚀 شما هم می‌توانید با فعالیت در ربات به لیست برترین‌ها اضافه شوید!"
            try:
                await bot.send_message(chat_id=self.channel_id, text=text, parse_mode="Markdown")
                return True
            except TelegramError as exc:
                logger.error("top_users_post_failed", error=str(exc))
                return False

    async def post_viral_content(self) -> int:
        bot = self._require_bot()
        with Session(self._engine_ref()) as session:
            posts = session.exec(
                select(ViralPost)
                .where(ViralPost.status == "pending")
                .order_by(desc(ViralPost.viral_score))
                .limit(10)
            ).all()
            count = 0
            for post in posts:
                try:
                    await bot.send_message(chat_id=self.channel_id, text=post.text)
                    post.status = "posted"
                    post.posted_at = datetime.now(timezone.utc)
                    session.add(post)
                    count += 1
                except TelegramError as exc:
                    logger.error("viral_post_failed", post_id=post.id, error=str(exc))
                    post.status = "failed"
                    session.add(post)
            session.commit()
            return count

    async def run_nightly_tasks(self) -> None:
        logger.info("channel_nightly_start")
        await self.post_top_users()
        await self.viral_engine.generate_and_schedule(self.channel_id, count=10)
        await self.post_viral_content()
        logger.info("channel_nightly_done")
