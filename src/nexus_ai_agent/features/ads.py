"""Advertisement campaigns for the NEXUS AI Telegram bot.

Owner-gated campaigns are persisted in SQLite and delivered by one in-process
loop. The consistency goal is **at most once per process**: a due row is
claimed (``next_run`` advanced, or the campaign completed) before the Telegram
send. A crash between claim and send can skip one interval; it cannot emit the
same interval twice. This is not a multi-process exactly-once protocol — the
bot is one process, and inventing a distributed lease would not match the
architecture.

Sync SQLite work runs off the event loop via :func:`asyncio.to_thread`. The
engine is cached (``check_same_thread=False``) and disposed on
:meth:`AdManager.close`.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import AdCampaign

logger = get_logger(__name__)

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168
MAX_TEXT_CHARS = 3500
MAX_REPEATS = 10_000
MAX_CAMPAIGNS_PER_CHAT = 20
DEFAULT_POLL_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 30.0


class AdValidationError(ValueError):
    """User-facing validation failure. The message is safe to reply with."""


def _as_utc(dt: datetime) -> datetime:
    """Normalise a possibly-naive stored datetime to aware UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class AdManager:
    """Create, pause, and deliver ad campaigns for one process."""

    def __init__(self, bot: Any | None = None, db_path: str | None = None) -> None:
        self.bot = bot
        self._db_path = db_path
        self._engine: Any | None = None
        self._claim_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self.poll_seconds = DEFAULT_POLL_SECONDS
        self.delivered = 0
        self.failed = 0
        self.last_error = ""

    def bind(self, bot: Any) -> None:
        """Attach the Telegram bot used for delivery."""
        self.bot = bot

    @property
    def is_bound(self) -> bool:
        return self.bot is not None

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
        """Dispose the engine. Cancel the loop without awaiting it."""
        self._stop.set()
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    async def start(self) -> None:
        """Start the delivery loop. Idempotent."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="ad-delivery")

    async def stop(self) -> None:
        """Cancel the delivery loop and wait for it to finish."""
        self._stop.set()
        task = self._loop_task
        self._loop_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("ad_delivery_loop_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    def snapshot(self) -> dict[str, Any]:
        """In-process delivery counters. Not a substitute for the database."""
        running = self._loop_task is not None and not self._loop_task.done()
        return {
            "running": running,
            "bound": self.is_bound,
            "delivered": self.delivered,
            "failed": self.failed,
            "last_error": self.last_error,
        }

    # -- validation ----------------------------------------------------------

    def _validate(self, chat_id: int, text: str, interval_hours: int, max_repeats: int) -> str:
        cleaned = text.strip()
        if chat_id == 0:
            raise AdValidationError("❌ چت نامعتبر است.")
        if not cleaned:
            raise AdValidationError("❌ متن تبلیغ خالی است.")
        if len(cleaned) > MAX_TEXT_CHARS:
            raise AdValidationError(f"❌ متن تبلیغ حداکثر {MAX_TEXT_CHARS} نویسه است.")
        if interval_hours < MIN_INTERVAL_HOURS or interval_hours > MAX_INTERVAL_HOURS:
            raise AdValidationError(
                f"❌ فاصله باید بین {MIN_INTERVAL_HOURS} و {MAX_INTERVAL_HOURS} ساعت باشد."
            )
        if max_repeats < 0 or max_repeats > MAX_REPEATS:
            raise AdValidationError(f"❌ تعداد تکرار باید بین ۰ و {MAX_REPEATS} باشد.")
        return cleaned

    def create_campaign(
        self,
        chat_id: int,
        text: str,
        interval_hours: int = 24,
        max_repeats: int = 0,
        created_by: int = 0,
    ) -> int:
        """Persist a campaign and return its id. Raises :class:`AdValidationError`."""
        cleaned = self._validate(chat_id, text, interval_hours, max_repeats)
        now = datetime.now(timezone.utc)
        with self._claim_lock, Session(self._engine_ref()) as session:
            existing = session.exec(
                select(AdCampaign).where(
                    AdCampaign.chat_id == chat_id,
                    col(AdCampaign.status).in_(["active", "paused"]),
                )
            ).all()
            if len(existing) >= MAX_CAMPAIGNS_PER_CHAT:
                raise AdValidationError(
                    f"❌ سقف {MAX_CAMPAIGNS_PER_CHAT} کمپین برای این چت پر شده است."
                )
            campaign = AdCampaign(
                chat_id=chat_id,
                text=cleaned,
                interval_hours=float(interval_hours),
                status="active",
                repeat_count=0,
                max_repeats=max_repeats,
                next_run=now,
                created_by=created_by,
            )
            session.add(campaign)
            session.commit()
            session.refresh(campaign)
            campaign_id = campaign.id if campaign.id is not None else 0
        logger.info("ad_campaign_created", campaign_id=campaign_id, chat_id=chat_id)
        return campaign_id

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        with Session(self._engine_ref()) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return None
            return _campaign_dict(campaign, text_limit=None)

    def list_campaigns(self, chat_id: int, status: str | None = None) -> list[dict[str, Any]]:
        with Session(self._engine_ref()) as session:
            stmt = select(AdCampaign).where(AdCampaign.chat_id == chat_id)
            if status is not None:
                stmt = stmt.where(AdCampaign.status == status)
            stmt = stmt.order_by(col(AdCampaign.id).desc())
            return [_campaign_dict(row, text_limit=80) for row in session.exec(stmt).all()]

    def pause_campaign(self, campaign_id: int, chat_id: int | None = None) -> bool:
        return self._set_status(
            campaign_id, expected="active", new_status="paused", chat_id=chat_id
        )

    def resume_campaign(self, campaign_id: int, chat_id: int | None = None) -> bool:
        with self._claim_lock, Session(self._engine_ref()) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None or campaign.status != "paused":
                return False
            if chat_id is not None and campaign.chat_id != chat_id:
                return False
            campaign.status = "active"
            campaign.next_run = datetime.now(timezone.utc)
            session.add(campaign)
            session.commit()
            return True

    def delete_campaign(self, campaign_id: int, chat_id: int | None = None) -> bool:
        with self._claim_lock, Session(self._engine_ref()) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return False
            if chat_id is not None and campaign.chat_id != chat_id:
                return False
            session.delete(campaign)
            session.commit()
            return True

    def _set_status(
        self,
        campaign_id: int,
        *,
        expected: str,
        new_status: str,
        chat_id: int | None = None,
    ) -> bool:
        with self._claim_lock, Session(self._engine_ref()) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None or campaign.status != expected:
                return False
            if chat_id is not None and campaign.chat_id != chat_id:
                return False
            campaign.status = new_status
            session.add(campaign)
            session.commit()
            return True

    def get_stats(self, chat_id: int = 0) -> dict[str, int]:
        with Session(self._engine_ref()) as session:
            stmt = select(AdCampaign)
            if chat_id:
                stmt = stmt.where(AdCampaign.chat_id == chat_id)
            rows = session.exec(stmt).all()
        return {
            "total": len(rows),
            "active": sum(1 for row in rows if row.status == "active"),
            "paused": sum(1 for row in rows if row.status == "paused"),
            "completed": sum(1 for row in rows if row.status == "completed"),
        }

    def get_due_campaigns(self) -> list[dict[str, Any]]:
        """Read-only view of due campaigns. Does **not** claim them."""
        now = datetime.now(timezone.utc)
        with Session(self._engine_ref()) as session:
            stmt = (
                select(AdCampaign)
                .where(AdCampaign.status == "active")
                .order_by(col(AdCampaign.next_run).asc())
            )
            due = [
                row
                for row in session.exec(stmt).all()
                if row.next_run is not None and _as_utc(row.next_run) <= now
            ]
            return [_campaign_dict(row, text_limit=None) for row in due]

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Claim due campaigns. Safe to call concurrently inside one process."""
        return self._claim_due_sync(now or datetime.now(timezone.utc))

    def _claim_due_sync(self, now: datetime) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self._claim_lock, Session(self._engine_ref()) as session:
            stmt = (
                select(AdCampaign)
                .where(AdCampaign.status == "active")
                .order_by(col(AdCampaign.next_run).asc())
                .limit(100)
            )
            for campaign in session.exec(stmt).all():
                if campaign.next_run is None or _as_utc(campaign.next_run) > now:
                    continue
                campaign.repeat_count += 1
                if campaign.max_repeats > 0 and campaign.repeat_count >= campaign.max_repeats:
                    campaign.status = "completed"
                    campaign.next_run = None
                else:
                    hours = campaign.interval_hours if campaign.interval_hours > 0 else 24
                    campaign.next_run = now + timedelta(hours=hours)
                session.add(campaign)
                claimed.append(_campaign_dict(campaign, text_limit=None))
            if claimed:
                session.commit()
        return claimed

    def mark_delivered(self, campaign_id: int) -> str:
        """Advance one campaign. Prefer :meth:`claim_due` for the loop."""
        with self._claim_lock, Session(self._engine_ref()) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return "not_found"
            if campaign.status != "active":
                return campaign.status
            campaign.repeat_count += 1
            if campaign.max_repeats > 0 and campaign.repeat_count >= campaign.max_repeats:
                campaign.status = "completed"
                campaign.next_run = None
                session.add(campaign)
                session.commit()
                return "completed"
            hours = campaign.interval_hours if campaign.interval_hours > 0 else 24
            campaign.next_run = datetime.now(timezone.utc) + timedelta(hours=hours)
            session.add(campaign)
            session.commit()
            return "active"

    async def deliver_due(self) -> int:
        """Claim due campaigns and send them. Unbound bot claims nothing."""
        bot = self.bot
        if bot is None:
            self.last_error = "unbound"
            logger.error("ad_delivery_unbound")
            return 0
        claimed = await asyncio.to_thread(self.claim_due)
        sent = 0
        for item in claimed:
            campaign_id = int(item["id"] or 0)
            try:
                await asyncio.wait_for(
                    bot.send_message(chat_id=int(item["chat_id"]), text=str(item["text"])),
                    timeout=SEND_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 — one failed send must not kill the loop
                self.failed += 1
                self.last_error = type(exc).__name__
                logger.exception("ad_send_failed", campaign_id=campaign_id)
                continue
            self.delivered += 1
            self.last_error = ""
            sent += 1
        return sent


def _campaign_dict(campaign: AdCampaign, *, text_limit: int | None) -> dict[str, Any]:
    text = campaign.text
    if text_limit is not None:
        text = text[:text_limit]
    next_run = campaign.next_run
    return {
        "id": campaign.id,
        "chat_id": campaign.chat_id,
        "text": text,
        "interval_hours": campaign.interval_hours,
        "status": campaign.status,
        "repeat_count": campaign.repeat_count,
        "max_repeats": campaign.max_repeats,
        "next_run": _as_utc(next_run).isoformat() if next_run is not None else None,
        "created_by": campaign.created_by,
    }
