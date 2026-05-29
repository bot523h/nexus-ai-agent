"""Gamification System for NEXUS AI Telegram bot.

Provides XP, leveling, streaks, daily rewards, achievements,
and leaderboard functionality.

No paid packages — SQLite for persistence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import UserXP

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


# ---------------------------------------------------------------------------
# Level configuration
# ---------------------------------------------------------------------------

# XP required for each level (cumulative)
_LEVEL_XP: list[int] = [
    0,      # Level 0 (newbie)
    50,     # Level 1
    150,    # Level 2
    300,    # Level 3
    600,    # Level 4
    1000,   # Level 5
    1500,   # Level 6
    2200,   # Level 7
    3000,   # Level 8
    4000,   # Level 9
    5500,   # Level 10
    7500,   # Level 11
    10000,  # Level 12
    13000,  # Level 13
    17000,  # Level 14
    22000,  # Level 15 (legend)
]

# Level titles (Persian)
_LEVEL_TITLES: dict[int, str] = {
    0: "🌱 تازه‌کار",
    1: "📋 کارآموز",
    2: "⚡ فعال",
    3: "🔥 حرفه‌ای",
    4: "💎 متخصص",
    5: "👑 استاد",
    6: "🦸 قهرمان",
    7: "🌟 افسانه‌ای",
    8: "🐉 اژدها",
    9: "⚡ نیمه‌خدا",
    10: "🔱 خدا",
    11: "🌟 ستاره",
    12: "🔮 جادوگر",
    13: "🌌 کهکشان",
    14: "♾️ بی‌نهایت",
    15: "🏆 اسطوره",
}

# Achievement definitions
_ACHIEVEMENTS: dict[str, dict[str, str]] = {
    "first_message": {"name": "💬 اولین قدم", "desc": "اولین پیامت رو فرستادی"},
    "chatter_100": {"name": "🗣ه صد‌گو", "desc": "۱۰۰ پیام فرستادی"},
    "level_5": {"name": "⭐ نیمه‌راه", "desc": "به سطح ۵ رسیدی"},
    "level_10": {"name": "🏆 استاد", "desc": "به سطح ۱۰ رسیدی"},
    "streak_7": {"name": "🔥 هفته‌آفرین", "desc": "۷ روز متوالی آنلاین بودی"},
    "streak_30": {"name": "💫 ماه‌آفرین", "desc": "۳۰ روز متوالی آنلاین بودی"},
    "quiz_master": {"name": "🧠 استاد کوییز", "desc": "در کوییز عالی بودی"},
    "helper": {"name": "🤝 یار دست‌آدم", "desc": "کمک به اعضای گروه"},
}

# XP rewards
XP_PER_MESSAGE = 1
XP_PER_COMMAND = 3
XP_PER_QUIZ_CORRECT = 10
XP_DAILY_BONUS = 25
XP_STREAK_BONUS = 5  # per streak day


class GamificationEngine:
    """XP, leveling, streaks, daily rewards, and achievements."""

    # ------------------------------------------------------------------
    # XP management
    # ------------------------------------------------------------------

    @staticmethod
    def _get_or_create_xp(user_id: int, chat_id: int) -> UserXP:
        """Get or create UserXP record."""
        engine = _sync_engine()
        with Session(engine) as session:
            xp = session.exec(
                select(UserXP).where(
                    UserXP.user_id == user_id, UserXP.chat_id == chat_id
                )
            ).first()
            if xp is None:
                xp = UserXP(
                    user_id=user_id,
                    chat_id=chat_id,
                    xp=0,
                    level=0,
                    streak=0,
                    achievements="[]",
                )
                session.add(xp)
                session.commit()
                session.refresh(xp)
            return xp

    @staticmethod
    def add_xp(user_id: int, chat_id: int, amount: int) -> dict[str, Any]:
        """Add XP to a user. Returns updated stats and level-up info."""
        engine = _sync_engine()
        with Session(engine) as session:
            xp = session.exec(
                select(UserXP).where(
                    UserXP.user_id == user_id, UserXP.chat_id == chat_id
                )
            ).first()
            if xp is None:
                xp = UserXP(
                    user_id=user_id,
                    chat_id=chat_id,
                    xp=0,
                    level=0,
                    streak=0,
                    achievements="[]",
                )
                session.add(xp)
                session.flush()

            old_level = xp.level
            xp.xp += amount

            # Check level up
            new_level = GamificationEngine._calculate_level(xp.xp)
            xp.level = new_level
            session.add(xp)
            session.commit()
            session.refresh(xp)

            leveled_up = new_level > old_level
            if leveled_up:
                logger.info(
                    "user_level_up",
                    user_id=user_id,
                    chat_id=chat_id,
                    new_level=new_level,
                )

            return {
                "xp": xp.xp,
                "level": xp.level,
                "leveled_up": leveled_up,
                "old_level": old_level,
                "title": GamificationEngine.get_level_title(xp.level),
            }

    @staticmethod
    def _calculate_level(total_xp: int) -> int:
        """Calculate level from total XP."""
        level = 0
        for i, required in enumerate(_LEVEL_XP):
            if total_xp >= required:
                level = i
            else:
                break
        return level

    @staticmethod
    def get_level_title(level: int) -> str:
        """Get the title for a level."""
        return _LEVEL_TITLES.get(level, f"🏅 سطح {level}")

    @staticmethod
    def get_xp_for_next_level(current_xp: int) -> int:
        """Get XP needed for the next level."""
        current_level = GamificationEngine._calculate_level(current_xp)
        next_level = current_level + 1
        if next_level >= len(_LEVEL_XP):
            return 0  # Max level
        return _LEVEL_XP[next_level] - current_xp

    # ------------------------------------------------------------------
    # Streaks
    # ------------------------------------------------------------------

    @staticmethod
    def update_streak(user_id: int, chat_id: int) -> dict[str, Any]:
        """Update user's daily streak. Call once per day per user."""
        engine = _sync_engine()
        with Session(engine) as session:
            xp = session.exec(
                select(UserXP).where(
                    UserXP.user_id == user_id, UserXP.chat_id == chat_id
                )
            ).first()
            if xp is None:
                xp = UserXP(
                    user_id=user_id,
                    chat_id=chat_id,
                    xp=0,
                    level=0,
                    streak=1,
                    achievements="[]",
                )
                session.add(xp)
                session.commit()
                return {"streak": 1, "streak_broken": False}

            now = datetime.now(timezone.utc)
            if xp.last_daily is not None:
                diff = (now - xp.last_daily).total_seconds() / 86400
                if diff < 1.0:
                    # Already claimed today
                    return {"streak": xp.streak, "streak_broken": False}
                if diff > 2.0:
                    # Streak broken (missed a day)
                    xp.streak = 1
                else:
                    xp.streak += 1

            xp.last_daily = now
            session.add(xp)
            session.commit()
            session.refresh(xp)
            return {"streak": xp.streak, "streak_broken": False}

    # ------------------------------------------------------------------
    # Daily rewards
    # ------------------------------------------------------------------

    @staticmethod
    def claim_daily(user_id: int, chat_id: int) -> dict[str, Any]:
        """Claim daily XP reward. Returns reward info."""
        now = datetime.now(timezone.utc)
        engine = _sync_engine()
        with Session(engine) as session:
            xp = session.exec(
                select(UserXP).where(
                    UserXP.user_id == user_id, UserXP.chat_id == chat_id
                )
            ).first()
            if xp is None:
                xp = UserXP(
                    user_id=user_id,
                    chat_id=chat_id,
                    xp=0,
                    level=0,
                    streak=1,
                    achievements="[]",
                    last_daily=now,
                )
                session.add(xp)
                session.flush()

            if xp.last_daily is not None:
                diff = (now - xp.last_daily).total_seconds() / 86400
                if diff < 1.0:
                    remaining = 1.0 - diff
                    hours = int(remaining * 24)
                    return {
                        "claimed": False,
                        "reason": "already_claimed",
                        "remaining_hours": hours,
                    }

            # Update streak
            streak_info = GamificationEngine.update_streak(user_id, chat_id)
            streak_bonus = min(streak_info["streak"], 10) * XP_STREAK_BONUS
            total_reward = XP_DAILY_BONUS + streak_bonus

            # Add XP
            old_level = xp.level
            xp.xp += total_reward
            xp.level = GamificationEngine._calculate_level(xp.xp)
            xp.last_daily = now
            session.add(xp)
            session.commit()
            session.refresh(xp)

            return {
                "claimed": True,
                "base_reward": XP_DAILY_BONUS,
                "streak_bonus": streak_bonus,
                "total_reward": total_reward,
                "streak": streak_info["streak"],
                "leveled_up": xp.level > old_level,
                "new_level": xp.level,
            }

    # ------------------------------------------------------------------
    # Achievements
    # ------------------------------------------------------------------

    @staticmethod
    def get_achievements(user_id: int, chat_id: int) -> list[str]:
        """Get user's unlocked achievements."""
        xp = GamificationEngine._get_or_create_xp(user_id, chat_id)
        try:
            return json.loads(xp.achievements) if xp.achievements else []
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def unlock_achievement(
        user_id: int, chat_id: int, achievement_id: str
    ) -> bool:
        """Unlock an achievement for a user. Returns True if newly unlocked."""
        engine = _sync_engine()
        with Session(engine) as session:
            xp = session.exec(
                select(UserXP).where(
                    UserXP.user_id == user_id, UserXP.chat_id == chat_id
                )
            ).first()
            if xp is None:
                return False

            try:
                achievements = json.loads(xp.achievements) if xp.achievements else []
            except (json.JSONDecodeError, TypeError):
                achievements = []

            if achievement_id in achievements:
                return False

            achievements.append(achievement_id)
            xp.achievements = json.dumps(achievements)
            session.add(xp)
            session.commit()
            logger.info(
                "achievement_unlocked",
                user_id=user_id,
                chat_id=chat_id,
                achievement=achievement_id,
            )
            return True

    @staticmethod
    def format_achievements(achievement_ids: list[str]) -> str:
        """Format achievement IDs into readable text."""
        lines = []
        for aid in achievement_ids:
            ach = _ACHIEVEMENTS.get(aid)
            if ach:
                lines.append(f"  {ach['name']} — {ach['desc']}")
            else:
                lines.append(f"  ❓ {aid}")
        return "\n".join(lines) if lines else "هنوز دستاوردی نداری!"

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    @staticmethod
    def get_leaderboard(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Get top users by XP in a chat."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = (
                select(UserXP)
                .where(UserXP.chat_id == chat_id)
                .order_by(col(UserXP.xp).desc())
                .limit(limit)
            )
            results = session.exec(stmt).all()
            return [
                {
                    "user_id": r.user_id,
                    "xp": r.xp,
                    "level": r.level,
                    "title": GamificationEngine.get_level_title(r.level),
                    "streak": r.streak,
                }
                for r in results
            ]

    # ------------------------------------------------------------------
    # User profile
    # ------------------------------------------------------------------

    @staticmethod
    def get_profile(user_id: int, chat_id: int) -> dict[str, Any]:
        """Get full gamification profile for a user."""
        xp = GamificationEngine._get_or_create_xp(user_id, chat_id)
        achievements = GamificationEngine.get_achievements(user_id, chat_id)
        return {
            "user_id": user_id,
            "xp": xp.xp,
            "level": xp.level,
            "title": GamificationEngine.get_level_title(xp.level),
            "streak": xp.streak,
            "xp_to_next": GamificationEngine.get_xp_for_next_level(xp.xp),
            "achievements": achievements,
            "achievement_count": len(achievements),
        }
