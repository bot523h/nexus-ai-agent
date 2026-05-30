"""Analytics Engine for NEXUS AI Telegram bot.

Provides metrics tracking: active users, retention, engagement rate,
peak hours, viral scores, and command usage statistics.

No paid packages — SQLite for persistence, event-driven tracking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import AnalyticsEvent

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class AnalyticsEngine:
    """Track, store, and query analytics events."""

    # ------------------------------------------------------------------
    # Event tracking
    # ------------------------------------------------------------------

    @staticmethod
    def track_event(
        chat_id: int,
        user_id: int,
        event_type: str,
        event_data: dict[str, Any] | None = None,
    ) -> int:
        """Record an analytics event. Returns event ID."""
        import json

        engine = _sync_engine()
        with Session(engine) as session:
            event = AnalyticsEvent(
                chat_id=chat_id,
                user_id=user_id,
                event_type=event_type,
                event_data=json.dumps(event_data) if event_data else None,
                created_at=datetime.now(timezone.utc),
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            return event.id if event.id is not None else 0

    @staticmethod
    def track_bulk_events(events: list[dict[str, Any]]) -> int:
        """Record multiple analytics events at once. Returns count."""
        import json

        engine = _sync_engine()
        count = 0
        with Session(engine) as session:
            for ev in events:
                event = AnalyticsEvent(
                    chat_id=ev.get("chat_id", 0),
                    user_id=ev.get("user_id", 0),
                    event_type=ev.get("event_type", "unknown"),
                    event_data=(json.dumps(ev["event_data"]) if "event_data" in ev else None),
                    created_at=ev.get("created_at", datetime.now(timezone.utc)),
                )
                session.add(event)
                count += 1
            session.commit()
        return count

    # ------------------------------------------------------------------
    # Active users
    # ------------------------------------------------------------------

    @staticmethod
    def get_active_users(chat_id: int = 0, hours: int = 24) -> list[dict[str, Any]]:
        """Get users active in the last N hours."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnalyticsEvent).where(AnalyticsEvent.created_at >= since)
            if chat_id:
                stmt = stmt.where(AnalyticsEvent.chat_id == chat_id)
            results = session.exec(stmt).all()

            # Group by user_id
            user_events: dict[int, int] = {}
            for r in results:
                if r.user_id not in user_events:
                    user_events[r.user_id] = 0
                user_events[r.user_id] += 1

            return [
                {"user_id": uid, "events": cnt}
                for uid, cnt in sorted(user_events.items(), key=lambda x: x[1], reverse=True)
            ]

    @staticmethod
    def get_active_user_count(chat_id: int = 0, hours: int = 24) -> int:
        """Count unique active users in the last N hours."""
        return len(AnalyticsEngine.get_active_users(chat_id, hours))

    # ------------------------------------------------------------------
    # Engagement rate
    # ------------------------------------------------------------------

    @staticmethod
    def get_engagement_rate(chat_id: int = 0, hours: int = 24) -> dict[str, float]:
        """Calculate engagement metrics for the last N hours.

        Returns dict with total_events, unique_users, events_per_user.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnalyticsEvent).where(AnalyticsEvent.created_at >= since)
            if chat_id:
                stmt = stmt.where(AnalyticsEvent.chat_id == chat_id)
            results = session.exec(stmt).all()

            total = len(results)
            unique_users = len({r.user_id for r in results})
            events_per_user = total / unique_users if unique_users else 0.0

            return {
                "total_events": total,
                "unique_users": unique_users,
                "events_per_user": round(events_per_user, 2),
            }

    # ------------------------------------------------------------------
    # Peak hours
    # ------------------------------------------------------------------

    @staticmethod
    def get_peak_hours(chat_id: int = 0, days: int = 7) -> list[dict[str, Any]]:
        """Get peak activity hours over the last N days."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnalyticsEvent).where(AnalyticsEvent.created_at >= since)
            if chat_id:
                stmt = stmt.where(AnalyticsEvent.chat_id == chat_id)
            results = session.exec(stmt).all()

            # Count events per hour
            hour_counts: dict[int, int] = {}
            for r in results:
                if r.created_at is not None:
                    hour = r.created_at.hour
                    hour_counts[hour] = hour_counts.get(hour, 0) + 1

            # Sort by count descending
            sorted_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)
            return [{"hour": h, "count": c, "label": f"{h:02d}:00"} for h, c in sorted_hours]

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    @staticmethod
    def get_retention(chat_id: int = 0, days: int = 7) -> dict[str, Any]:
        """Calculate user retention over N days.

        Returns day-by-day retention: users who were active on day 0
        and came back on subsequent days.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnalyticsEvent).where(AnalyticsEvent.created_at >= start)
            if chat_id:
                stmt = stmt.where(AnalyticsEvent.chat_id == chat_id)
            results = session.exec(stmt).all()

            # Group users by day
            day_users: dict[str, set[int]] = {}
            for r in results:
                if r.created_at is not None:
                    day_key = r.created_at.strftime("%Y-%m-%d")
                    if day_key not in day_users:
                        day_users[day_key] = set()
                    day_users[day_key].add(r.user_id)

            if not day_users:
                return {"days": days, "retention": [], "cohort_size": 0}

            # Use the earliest day as cohort
            sorted_days = sorted(day_users.keys())
            cohort_day = sorted_days[0]
            cohort_users = day_users[cohort_day]
            cohort_size = len(cohort_users)

            retention = []
            for day_key in sorted_days:
                retained = len(cohort_users & day_users[day_key])
                rate = (retained / cohort_size * 100) if cohort_size else 0
                retention.append({"date": day_key, "retained": retained, "rate": round(rate, 1)})

            return {
                "days": days,
                "retention": retention,
                "cohort_size": cohort_size,
            }

    # ------------------------------------------------------------------
    # Command usage
    # ------------------------------------------------------------------

    @staticmethod
    def get_command_usage(chat_id: int = 0, hours: int = 24) -> list[dict[str, Any]]:
        """Get command usage statistics."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(AnalyticsEvent).where(
                AnalyticsEvent.event_type == "command",
                AnalyticsEvent.created_at >= since,
            )
            if chat_id:
                stmt = stmt.where(AnalyticsEvent.chat_id == chat_id)
            results = session.exec(stmt).all()

            # Group by command name (from event_data)
            import json

            cmd_counts: dict[str, int] = {}
            for r in results:
                if r.event_data:
                    try:
                        data = json.loads(r.event_data)
                        cmd = data.get("command", "unknown")
                    except (json.JSONDecodeError, TypeError):
                        cmd = "unknown"
                else:
                    cmd = "unknown"
                cmd_counts[cmd] = cmd_counts.get(cmd, 0) + 1

            return [
                {"command": cmd, "count": cnt}
                for cmd, cnt in sorted(cmd_counts.items(), key=lambda x: x[1], reverse=True)
            ]

    # ------------------------------------------------------------------
    # Dashboard summary
    # ------------------------------------------------------------------

    @staticmethod
    def get_dashboard(chat_id: int = 0) -> dict[str, Any]:
        """Get a full analytics dashboard summary."""
        active_24h = AnalyticsEngine.get_active_user_count(chat_id, hours=24)
        active_7d = AnalyticsEngine.get_active_user_count(chat_id, hours=168)
        engagement = AnalyticsEngine.get_engagement_rate(chat_id, hours=24)
        peak_hours = AnalyticsEngine.get_peak_hours(chat_id, days=7)
        top_commands = AnalyticsEngine.get_command_usage(chat_id, hours=24)

        # Top 3 peak hours
        top_peak = peak_hours[:3] if peak_hours else []

        return {
            "active_users_24h": active_24h,
            "active_users_7d": active_7d,
            "engagement_24h": engagement,
            "peak_hours_top3": top_peak,
            "top_commands": top_commands[:5],
        }
