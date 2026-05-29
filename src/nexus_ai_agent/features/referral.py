"""Referral viral loop system — exponential growth engine with tiered rewards."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine as _ce
from sqlmodel import Field, Session as _Session, SQLModel, select

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


# ── Database Models ──────────────────────────────────────────────────


class Referral(SQLModel, table=True):
    """Referral tracking — who referred whom."""

    id: int | None = Field(default=None, primary_key=True)
    referrer_id: int = Field(index=True)
    referee_id: int = Field(index=True, unique=True)  # each user can only be referred once
    referral_code: str = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | completed | rewarded
    reward_claimed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    completed_at: datetime | None = Field(default=None)


class ReferralCode(SQLModel, table=True):
    """Unique referral codes per user."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, unique=True)
    code: str = Field(index=True, unique=True)
    total_referrals: int = Field(default=0)
    successful_referrals: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Tiered Rewards ───────────────────────────────────────────────────

REFERRAL_REWARDS: list[dict[str, Any]] = [
    {"count": 1, "title": "🥉 دعوت‌کننده", "reward": "نشان دعوت", "xp": 50},
    {"count": 3, "title": "🥈 شبکه‌ساز", "reward": "۳ روز Premium", "xp": 150},
    {"count": 5, "title": "🥇 ستاره دعوت", "reward": "۷ روز Premium", "xp": 300},
    {"count": 10, "title": "💎 الماسی", "reward": "۳۰ روز Premium + AI بدون محدودیت", "xp": 500},
    {"count": 25, "title": "👑 افسانه‌ای", "reward": "VIP مادام‌العمر + نشان ویژه", "xp": 1000},
    {"count": 50, "title": "🚀 نابغه وایرال", "reward": "Co-Owner + تمام امکانات", "xp": 2500},
]


class ReferralEngine:
    """Referral viral loop engine — track, reward, and grow."""

    def __init__(self, db_path: str = "data/app.sqlite") -> None:
        self._db_path = db_path
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create tables if they don't exist."""
        engine = _ce(f"sqlite:///{self._db_path}", echo=False)
        SQLModel.metadata.create_all(engine, tables=[Referral.__tablename__])

    def _sync_engine(self) -> Any:
        return _ce(f"sqlite:///{self._db_path}", echo=False)

    @staticmethod
    def generate_code(user_id: int) -> str:
        """Generate a unique referral code for a user."""
        raw = f"NEXUS-{user_id}-{hashlib.sha256(str(user_id).encode()).hexdigest()[:8]}"
        return raw.upper()

    def get_or_create_code(self, user_id: int) -> str:
        """Get existing referral code or create one."""
        eng = self._sync_engine()
        with _Session(eng) as s:
            existing = s.exec(
                select(ReferralCode).where(ReferralCode.user_id == user_id)
            ).first()
            if existing:
                return existing.code
            code = self.generate_code(user_id)
            rc = ReferralCode(user_id=user_id, code=code)
            s.add(rc)
            s.commit()
            return code

    def get_referral_link(self, user_id: int, bot_username: str = "nexus_ai_agent_bot") -> str:
        """Get full referral link for sharing."""
        code = self.get_or_create_code(user_id)
        return f"https://t.me/{bot_username}?start=ref_{code}"

    def process_referral(self, referee_id: int, start_param: str) -> dict[str, Any]:
        """Process a referral when a new user starts the bot with a referral code.

        Args:
            referee_id: The new user's telegram ID
            start_param: The start parameter (e.g., "ref_NEXUS-123-ABCDEF12")

        Returns:
            Dict with success, referrer_id, reward info.
        """
        if not start_param.startswith("ref_"):
            return {"success": False, "error": "invalid_code"}

        code = start_param[4:]
        eng = self._sync_engine()
        with _Session(eng) as s:
            # Find referrer by code
            rc = s.exec(select(ReferralCode).where(ReferralCode.code == code)).first()
            if rc is None:
                return {"success": False, "error": "code_not_found"}

            referrer_id = rc.user_id

            # Can't refer yourself
            if referrer_id == referee_id:
                return {"success": False, "error": "self_referral"}

            # Check if already referred
            existing = s.exec(
                select(Referral).where(Referral.referee_id == referee_id)
            ).first()
            if existing:
                return {"success": False, "error": "already_referred"}

            # Create referral
            ref = Referral(
                referrer_id=referrer_id,
                referee_id=referee_id,
                referral_code=code,
                status="completed",
                completed_at=datetime.utcnow(),
            )
            s.add(ref)

            # Update code stats
            rc.total_referrals += 1
            rc.successful_referrals += 1
            s.add(rc)
            s.commit()

            # Check for tier reward
            count = rc.successful_referrals
            next_reward = self._get_next_reward(count)
            current_reward = self._get_current_reward(count)

            return {
                "success": True,
                "referrer_id": referrer_id,
                "referee_id": referee_id,
                "total_referrals": count,
                "current_reward": current_reward,
                "next_reward": next_reward,
            }

    def _get_current_reward(self, count: int) -> dict[str, Any] | None:
        """Get the reward tier the user is currently at."""
        result = None
        for r in REFERRAL_REWARDS:
            if count >= r["count"]:
                result = r
        return result

    def _get_next_reward(self, count: int) -> dict[str, Any] | None:
        """Get the next reward tier the user can achieve."""
        for r in REFERRAL_REWARDS:
            if count < r["count"]:
                return r
        return None  # Max tier reached

    def get_referral_stats(self, user_id: int) -> dict[str, Any]:
        """Get referral statistics for a user."""
        eng = self._sync_engine()
        with _Session(eng) as s:
            rc = s.exec(select(ReferralCode).where(ReferralCode.user_id == user_id)).first()
            if rc is None:
                return {
                    "code": self.generate_code(user_id),
                    "total": 0,
                    "successful": 0,
                    "current_tier": None,
                    "next_tier": REFERRAL_REWARDS[0] if REFERRAL_REWARDS else None,
                }
            count = rc.successful_referrals
            return {
                "code": rc.code,
                "total": rc.total_referrals,
                "successful": count,
                "current_tier": self._get_current_reward(count),
                "next_tier": self._get_next_reward(count),
            }

    def get_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get top referrers."""
        eng = self._sync_engine()
        with _Session(eng) as s:
            codes = s.exec(
                select(ReferralCode).order_by(  # type: ignore[arg-type]
                    ReferralCode.successful_referrals.desc()
                )
            ).all()
            result = []
            for i, rc in enumerate(codes[:limit], 1):
                result.append({
                    "rank": i,
                    "user_id": rc.user_id,
                    "code": rc.code,
                    "successful": rc.successful_referrals,
                    "tier": self._get_current_reward(rc.successful_referrals),
                })
            return result

    def format_stats(self, user_id: int, bot_username: str = "nexus_ai_agent_bot") -> str:
        """Format referral stats for display."""
        stats = self.get_referral_stats(user_id)
        link = self.get_referral_link(user_id, bot_username)
        lines = [
            "🔗 سیستم دعوت وایرال",
            "━━━━━━━━━━━━━━━━━",
            f"📋 کد دعوت: `{stats['code']}`",
            f"🔗 لینک: {link}",
            f"👥 دعوت‌های موفق: {stats['successful']}",
            f"📊 کل دعوت‌ها: {stats['total']}",
        ]
        if stats["current_tier"]:
            t = stats["current_tier"]
            lines.append(f"🏆 سطح فعلی: {t['title']}")
            lines.append(f"🎁 جایزه: {t['reward']}")
        if stats["next_tier"]:
            n = stats["next_tier"]
            lines.append(f"⏭️ سطح بعدی: {n['title']} (با {n['count']} دعوت)")
            remaining = n["count"] - stats["successful"]
            lines.append(f"📊 تا سطح بعدی: {remaining} دعوت")
        lines.append("\n💡 لینک دعوتت رو به دوستات بفرست!")
        lines.append("هر کسی با لینک شما وارد بشه، هر دو جایزه می‌گیرید! 🎉")
        return "\n".join(lines)

    def format_leaderboard(self) -> str:
        """Format referral leaderboard."""
        board = self.get_leaderboard()
        if not board:
            return "📊 هنوز کسی دعوت نکرده. اولین شما باشید! 🔗"
        lines = [
            "🏆 جدول دعوت‌کنندگان برتر",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]
        for entry in board:
            tier = entry["tier"]
            tier_icon = tier["title"].split()[0] if tier else "👤"
            lines.append(
                f"  {entry['rank']}. {tier_icon} کاربر {entry['user_id']}: "
                f"{entry['successful']} دعوت"
            )
        return "\n".join(lines)
