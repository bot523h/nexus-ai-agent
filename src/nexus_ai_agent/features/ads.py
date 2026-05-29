"""Advertisement System for NEXUS AI Telegram bot.

Provides scheduled ad campaigns with repeat intervals,
campaign management, and owner-only control.

No paid packages — SQLite for persistence, async-compatible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import AdCampaign

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class AdManager:
    """Manage ad campaigns: create, schedule, track, delete."""

    # ------------------------------------------------------------------
    # Campaign CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def create_campaign(
        chat_id: int,
        text: str,
        interval_hours: int = 24,
        max_repeats: int = 0,
        created_by: int = 0,
    ) -> int:
        """Create a new ad campaign. Returns campaign ID.

        Args:
            chat_id: Target chat for the ad.
            text: Ad text content.
            interval_hours: Hours between each ad run.
            max_repeats: Max times to repeat (0 = unlimited).
            created_by: User ID who created the campaign.
        """
        now = datetime.now(timezone.utc)
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = AdCampaign(
                chat_id=chat_id,
                text=text,
                interval_hours=interval_hours,
                status="active",
                repeat_count=0,
                max_repeats=max_repeats,
                next_run=now,
                created_by=created_by,
            )
            session.add(campaign)
            session.commit()
            session.refresh(campaign)
            logger.info(
                "ad_campaign_created",
                campaign_id=campaign.id,
                chat_id=chat_id,
                interval_hours=interval_hours,
            )
            return campaign.id if campaign.id is not None else 0

    @staticmethod
    def get_campaign(campaign_id: int) -> dict[str, Any] | None:
        """Get a single campaign by ID."""
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return None
            return {
                "id": campaign.id,
                "chat_id": campaign.chat_id,
                "text": campaign.text[:200],
                "interval_hours": campaign.interval_hours,
                "status": campaign.status,
                "repeat_count": campaign.repeat_count,
                "max_repeats": campaign.max_repeats,
                "next_run": (
                    campaign.next_run.isoformat() if campaign.next_run else None
                ),
                "created_by": campaign.created_by,
            }

    @staticmethod
    def list_campaigns(
        chat_id: int, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List ad campaigns, optionally filtered by status."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AdCampaign).where(AdCampaign.chat_id == chat_id)
            if status is not None:
                stmt = stmt.where(AdCampaign.status == status)
            stmt = stmt.order_by(col(AdCampaign.id).desc())
            results = session.exec(stmt).all()
            return [
                {
                    "id": r.id,
                    "text": r.text[:80],
                    "interval_hours": r.interval_hours,
                    "status": r.status,
                    "repeat_count": r.repeat_count,
                    "max_repeats": r.max_repeats,
                }
                for r in results
            ]

    @staticmethod
    def pause_campaign(campaign_id: int) -> bool:
        """Pause an active campaign."""
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return False
            campaign.status = "paused"
            session.add(campaign)
            session.commit()
            return True

    @staticmethod
    def resume_campaign(campaign_id: int) -> bool:
        """Resume a paused campaign."""
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return False
            campaign.status = "active"
            campaign.next_run = datetime.now(timezone.utc)
            session.add(campaign)
            session.commit()
            return True

    @staticmethod
    def delete_campaign(campaign_id: int) -> bool:
        """Delete a campaign entirely."""
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return False
            session.delete(campaign)
            session.commit()
            return True

    # ------------------------------------------------------------------
    # Ad scheduling / delivery
    # ------------------------------------------------------------------

    @staticmethod
    def get_due_campaigns() -> list[dict[str, Any]]:
        """Return campaigns that are due to be posted now."""
        now = datetime.now(timezone.utc)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = (
                select(AdCampaign)
                .where(
                    AdCampaign.status == "active",
                )
                .order_by(col(AdCampaign.next_run).asc())
            )
            now = datetime.now(timezone.utc)
            results = [
                r for r in session.exec(stmt).all()
                if r.next_run is not None and r.next_run <= now
            ]
            return [
                {
                    "id": r.id,
                    "chat_id": r.chat_id,
                    "text": r.text,
                    "interval_hours": r.interval_hours,
                    "repeat_count": r.repeat_count,
                    "max_repeats": r.max_repeats,
                }
                for r in results
            ]

    @staticmethod
    def mark_delivered(campaign_id: int) -> str:
        """Mark a campaign as delivered and schedule the next run.

        Returns the new status: "active", "completed", or "not_found".
        """
        engine = _sync_engine()
        with Session(engine) as session:
            campaign = session.get(AdCampaign, campaign_id)
            if campaign is None:
                return "not_found"

            campaign.repeat_count += 1

            # Check if max repeats reached
            if campaign.max_repeats > 0 and campaign.repeat_count >= campaign.max_repeats:
                campaign.status = "completed"
                session.add(campaign)
                session.commit()
                return "completed"

            # Schedule next run
            from datetime import timedelta

            campaign.next_run = datetime.now(timezone.utc) + timedelta(
                hours=campaign.interval_hours
            )
            session.add(campaign)
            session.commit()
            return "active"

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    @staticmethod
    def get_stats(chat_id: int = 0) -> dict[str, int]:
        """Return ad system statistics."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AdCampaign)
            if chat_id:
                stmt = stmt.where(AdCampaign.chat_id == chat_id)
            all_campaigns = session.exec(stmt).all()
            return {
                "total": len(all_campaigns),
                "active": sum(1 for c in all_campaigns if c.status == "active"),
                "paused": sum(1 for c in all_campaigns if c.status == "paused"),
                "completed": sum(
                    1 for c in all_campaigns if c.status == "completed"
                ),
            }
