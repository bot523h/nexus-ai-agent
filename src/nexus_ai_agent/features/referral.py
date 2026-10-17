"""Referral viral loop system — exponential growth engine with tiered rewards."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session as _Session
from sqlmodel import SQLModel, select

from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import sync_engine
from nexus_ai_agent.storage.models import Referral, ReferralCode

log = get_logger(__name__)

#: Unambiguous upper-case alphanumerics (no 0/O/1/I) for human-typed codes.
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")


# The ``Referral`` / ``ReferralCode`` tables are defined once, in
# ``storage/models.py`` (audit A-09); this module only operates on them.

# ── Tiered Rewards ───────────────────────────────────────────────────

REFERRAL_REWARDS: list[dict[str, Any]] = [
    {"count": 1, "title": "🥉 دعوت‌کننده", "reward": "نشان دعوت", "xp": 50},
    {"count": 3, "title": "🥈 شبکه‌ساز", "reward": "۳ روز Premium", "xp": 150},
    {"count": 5, "title": "🥇 ستاره دعوت", "reward": "۷ روز Premium", "xp": 300},
    {"count": 10, "title": "💎 الماسی", "reward": "۳۰ روز Premium + AI بدون محدودیت", "xp": 500},
    {"count": 25, "title": "👑 افسانه‌ای", "reward": "VIP مادام‌العمر + نشان ویژه", "xp": 1000},
    {"count": 50, "title": "🚀 نابغه وایرال", "reward": "Co-Owner + تمام امکانات", "xp": 2500},
]


def _naive_utcnow() -> datetime:
    """Current UTC time as the naive value the DATETIME columns store.

    ``datetime.utcnow()`` is deprecated (Python 3.12) and silently mixes naive
    and aware values; this keeps the stored representation unchanged (DTZ003,
    audit A-12).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReferralEngine:
    """Referral viral loop engine — track, reward, and grow."""

    def __init__(self, db_path: str | None = None) -> None:
        """``db_path`` pins a SQLite file (tests); ``None`` follows the
        configured backend — ``NEXUS_DATABASE_URL`` or ``NEXUS_DB_PATH`` — via
        ``storage.db.sync_engine`` (audit A-02)."""
        self._db_path = db_path
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create the two referral tables when they are missing.

        Dialect-agnostic ``create_all`` over the canonical ``storage.models``
        definitions replaced the hand-written SQLite DDL (``AUTOINCREMENT``),
        which a PostgreSQL deployment would have rejected. On PostgreSQL the
        tables already exist from migration ``47903d282ede``; ``checkfirst``
        makes this a no-op there.
        """
        tables = [SQLModel.metadata.tables["referral"], SQLModel.metadata.tables["referralcode"]]
        SQLModel.metadata.create_all(self._sync_engine(), tables=tables, checkfirst=True)

    def _sync_engine(self) -> Any:
        """The process-wide synchronous engine (see ``storage.db.sync_engine``).

        P1-2: the referral call-sites (``/start`` deep-link, ``/referral``,
        ``/referral_board``) run in worker threads via ``asyncio.to_thread``
        at the handler layer; the central engine is thread-safe and cached.
        """
        return sync_engine(self._db_path)

    @staticmethod
    def generate_code(user_id: int | None = None) -> str:
        """Generate a fresh referral code.

        The code is *random* (``secrets``), never derived from the user id:
        the previous ``NEXUS-<id>-<sha256(id)[:8]>`` scheme leaked the Telegram
        id in the code and let anyone forge any user's code (audit A-12,
        2026-10-15). ``user_id`` is accepted for signature compatibility and
        ignored. Codes already stored keep working — lookup is by stored value.
        """
        return "NEXUS-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))

    def get_or_create_code(self, user_id: int) -> str:
        """Get existing referral code or create one (collision-checked)."""
        eng = self._sync_engine()
        with _Session(eng) as s:
            existing = s.exec(select(ReferralCode).where(ReferralCode.user_id == user_id)).first()
            if existing:
                return existing.code
            for _ in range(5):
                code = self.generate_code()
                if s.exec(select(ReferralCode).where(ReferralCode.code == code)).first() is None:
                    break
            else:  # pragma: no cover — 36**8 space; five collisions in a row is a broken RNG
                raise RuntimeError("could not allocate a unique referral code")
            rc = ReferralCode(user_id=user_id, code=code)
            s.add(rc)
            s.commit()
            return code

    @staticmethod
    def referral_link(code: str, bot_username: str = "nexus_ai_agent_bot") -> str:
        """Render the deep link for an *already resolved* referral code.

        Pure, so the renderer (:meth:`format_stats`) can build the link from
        the exact code it displays instead of resolving the code a second time
        (review finding, 2026-10-17).
        """
        return f"https://t.me/{bot_username}?start=ref_{code}"

    def get_referral_link(self, user_id: int, bot_username: str = "nexus_ai_agent_bot") -> str:
        """Get full referral link for sharing."""
        code = self.get_or_create_code(user_id)
        return self.referral_link(code, bot_username)

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
            existing = s.exec(select(Referral).where(Referral.referee_id == referee_id)).first()
            if existing:
                return {"success": False, "error": "already_referred"}

            # Create referral
            ref = Referral(
                referrer_id=referrer_id,
                referee_id=referee_id,
                referral_code=code,
                status="completed",
                completed_at=_naive_utcnow(),
                xp_awarded=True,
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
                "xp_bonus": 50,  # Both users get +50 XP
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
        """Get referral statistics for a user.

        The ``code`` is **the stored one**: before 2026-10-17 a user with no
        row yet was handed a freshly generated (never persisted) code here,
        while :meth:`get_referral_link` — called by :meth:`format_stats` a
        line later — generated and stored *another* one. The link the user
        shared therefore carried a different code from the code shown next to
        it, and the displayed code was not the one the referrer was credited
        for. A read that allocates the code keeps every surface on one
        persistent identity.
        """
        eng = self._sync_engine()
        with _Session(eng) as s:
            rc = s.exec(select(ReferralCode).where(ReferralCode.user_id == user_id)).first()
            if rc is None:
                return {
                    "code": self.get_or_create_code(user_id),
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
                select(ReferralCode).order_by(
                    ReferralCode.successful_referrals.desc()  # type: ignore[attr-defined]
                )
            ).all()
            result = []
            for i, rc in enumerate(codes[:limit], 1):
                result.append(
                    {
                        "rank": i,
                        "user_id": rc.user_id,
                        "code": rc.code,
                        "successful": rc.successful_referrals,
                        "tier": self._get_current_reward(rc.successful_referrals),
                    }
                )
            return result

    def format_stats(self, user_id: int, bot_username: str = "nexus_ai_agent_bot") -> str:
        """Format referral stats for display.

        The link is built from the *same* code the statistics report, and both
        resolve through :meth:`get_or_create_code` — so the code shown, the
        code stored in ``referralcode`` and the code inside the link are one
        persistent identity on every call, including the very first
        ``/referral`` (which is what allocates it).
        """
        stats = self.get_referral_stats(user_id)
        link = self.referral_link(str(stats["code"]), bot_username)
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
