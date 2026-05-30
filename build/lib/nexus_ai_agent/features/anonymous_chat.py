"""Anonymous chat feature for NEXUS AI Telegram bot.

Connects two users for a private conversation without revealing identities.
Uses SQLite (AnonSession model) for persistence and a simple in-memory queue
for matching. The owner can inspect sessions for safety.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import AnonSession

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class AnonymousChatManager:
    """Manages anonymous 1-on-1 chat sessions."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._active: dict[int, int] = {}  # user_id → partner_user_id

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on AnonymousChatManager")
        return self.bot

    # ------------------------------------------------------------------
    # Queue & matching
    # ------------------------------------------------------------------

    async def join_queue(self, user_id: int) -> str:
        """Add *user_id* to the waiting queue. Returns status message."""
        # Already in an active session?
        if user_id in self._active:
            return "⚠️ شما قبلاً در یک چت ناشناس هستید. اول /anon_stop بزنید."

        # Try to match immediately
        if not self._queue.empty():
            try:
                partner_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                partner_id = None
            if partner_id is not None and partner_id != user_id and partner_id not in self._active:
                return await self._create_session(user_id, partner_id)

        # No match yet — add to queue
        await self._queue.put(user_id)
        return "⏳ در صف انتظار هستید... لطفاً صبر کنید تا کاربر دیگری متصل شود."

    async def _create_session(self, user1: int, user2: int) -> str:
        """Create an AnonSession in DB and wire up active map."""
        engine = _sync_engine()
        with Session(engine) as session:
            anon = AnonSession(
                user1_id=user1,
                user2_id=user2,
                started_at=datetime.now(timezone.utc),
                status="active",
            )
            session.add(anon)
            session.commit()

        self._active[user1] = user2
        self._active[user2] = user1

        # Notify both users
        bot = self._require_bot()
        try:
            await bot.send_message(
                chat_id=user1, text="🔗 به یک کاربر ناشناس وصل شدید! پیام بفرستید."
            )
            await bot.send_message(
                chat_id=user2, text="🔗 به یک کاربر ناشناس وصل شدید! پیام بفرستید."
            )
        except Exception:  # noqa: BLE001
            logger.exception("anon_notify_failed", user1=user1, user2=user2)

        return "🔗 وصل شدید! الان می‌تونید پیام ناشناس بفرستید."

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_anon_message(self, user_id: int, text: str) -> bool:
        """Forward *text* to the partner without revealing identity."""
        partner_id = self._active.get(user_id)
        if partner_id is None:
            return False
        bot = self._require_bot()
        try:
            await bot.send_message(
                chat_id=partner_id,
                text=f"📩 پیام ناشناس:\n{text}",
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception("anon_send_failed", user_id=user_id)
            return False

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def leave_chat(self, user_id: int) -> str:
        """End the active session for *user_id*."""
        partner_id = self._active.pop(user_id, None)
        if partner_id is None:
            return "⚠️ شما در هیچ چت ناشناسی نیستید."

        self._active.pop(partner_id, None)

        # Mark session as ended in DB
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = (
                select(AnonSession)
                .where(AnonSession.status == "active")
                .where((AnonSession.user1_id == user_id) | (AnonSession.user2_id == user_id))
            )
            results = session.exec(stmt).all()
            for s in results:
                s.status = "ended"
            session.commit()

        # Notify partner
        bot = self._require_bot()
        try:
            await bot.send_message(chat_id=partner_id, text="🔌 طرف مقابل چت ناشناس را ترک کرد.")
        except Exception:  # noqa: BLE001
            logger.exception("anon_leave_notify_failed", partner_id=partner_id)

        return "✅ چت ناشناس پایان یافت."

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    async def report_user(self, reporter_id: int, owner_id: int) -> str:
        """Report the current partner. Notify the owner for safety."""
        partner_id = self._active.get(reporter_id)
        if partner_id is None:
            return "⚠️ شما در هیچ چت ناشناسی نیستید که گزارش بدهید."

        # End the session
        await self.leave_chat(reporter_id)

        # Update DB status
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = (
                select(AnonSession)
                .where(AnonSession.status == "ended")
                .where(
                    (AnonSession.user1_id == reporter_id) | (AnonSession.user2_id == reporter_id)
                )
            )
            results = session.exec(stmt).all()
            for s in results:
                s.status = "reported"
            session.commit()

        # Notify owner
        if owner_id:
            bot = self._require_bot()
            try:
                await bot.send_message(
                    chat_id=owner_id,
                    text=(
                        f"🚨 گزارش سوءاستفاده:\nگزارش‌دهنده: {reporter_id}\nمورد گزارش: {partner_id}"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("anon_report_owner_notify_failed")

        return "🚨 کاربر گزارش شد. چت پایان یافت. ادمین مطلع شد."

    # ------------------------------------------------------------------
    # Owner inspection (safety)
    # ------------------------------------------------------------------

    def get_active_sessions(self) -> list[dict[str, Any]]:
        """Return active sessions for the owner to inspect."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnonSession).where(AnonSession.status == "active")
            results = session.exec(stmt).all()
            return [
                {
                    "id": s.id,
                    "user1_id": s.user1_id,
                    "user2_id": s.user2_id,
                    "started_at": str(s.started_at),
                }
                for s in results
            ]
