"""Smart Moderation System for NEXUS AI Telegram bot.

Provides anti-spam, anti-flood, link filtering, profanity filtering,
warning system, and user reputation tracking.

No paid packages — SQLite for persistence, heuristic-based filtering.
"""

from __future__ import annotations

import re
import time
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ModerationConfig, UserReputation

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


# ---------------------------------------------------------------------------
# Persian profanity list (common patterns)
# ---------------------------------------------------------------------------

_PROFANITY_PATTERNS: list[str] = [
    r"خرف",
    r"احمق",
    r"دیوانه",
    r"مغز",
    r"کثیف",
    r"حقیر",
    r"نادان",
    r"ابله",
    r"رید",
    r"خر",
    r"گوساله",
    r"سگ",
]

_PROFANITY_RE = re.compile("|".join(_PROFANITY_PATTERNS), re.IGNORECASE)

# Link detection regex
_LINK_RE = re.compile(
    r"https?://[^\s<>\"]+|t\.me/[^\s<>\"]+|www\.[^\s<>\"]+", re.IGNORECASE
)


class ModerationEngine:
    """Smart moderation: anti-spam, flood, link filter, profanity, warnings."""

    # In-memory flood tracking: user_id -> list of timestamps
    _flood_tracker: dict[int, list[float]] = {}

    # Rate limits
    FLOOD_WINDOW_SECONDS = 5
    FLOOD_MAX_MESSAGES = 5

    # ------------------------------------------------------------------
    # Config CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def get_config(chat_id: int) -> ModerationConfig | None:
        """Get moderation config for a chat."""
        engine = _sync_engine()
        with Session(engine) as session:
            return session.exec(
                select(ModerationConfig).where(ModerationConfig.chat_id == chat_id)
            ).first()

    @staticmethod
    def set_config(
        chat_id: int,
        *,
        anti_spam: bool | None = None,
        anti_flood: bool | None = None,
        link_filter: bool | None = None,
        profanity_filter: bool | None = None,
        max_warnings: int | None = None,
        mute_duration_minutes: int | None = None,
    ) -> ModerationConfig:
        """Create or update moderation config for a chat."""
        engine = _sync_engine()
        with Session(engine) as session:
            cfg = session.exec(
                select(ModerationConfig).where(ModerationConfig.chat_id == chat_id)
            ).first()
            if cfg is None:
                cfg = ModerationConfig(
                    chat_id=chat_id,
                    anti_spam=anti_spam if anti_spam is not None else True,
                    anti_flood=anti_flood if anti_flood is not None else True,
                    link_filter=link_filter if link_filter is not None else True,
                    profanity_filter=(
                        profanity_filter if profanity_filter is not None else True
                    ),
                    max_warnings=max_warnings if max_warnings is not None else 3,
                    mute_duration_minutes=(
                        mute_duration_minutes if mute_duration_minutes is not None else 30
                    ),
                )
            else:
                if anti_spam is not None:
                    cfg.anti_spam = anti_spam
                if anti_flood is not None:
                    cfg.anti_flood = anti_flood
                if link_filter is not None:
                    cfg.link_filter = link_filter
                if profanity_filter is not None:
                    cfg.profanity_filter = profanity_filter
                if max_warnings is not None:
                    cfg.max_warnings = max_warnings
                if mute_duration_minutes is not None:
                    cfg.mute_duration_minutes = mute_duration_minutes
            session.add(cfg)
            session.commit()
            session.refresh(cfg)
            return cfg

    # ------------------------------------------------------------------
    # Content analysis
    # ------------------------------------------------------------------

    @staticmethod
    def has_profanity(text: str) -> bool:
        """Check if text contains profanity."""
        return bool(_PROFANITY_RE.search(text))

    @staticmethod
    def has_links(text: str) -> bool:
        """Check if text contains links."""
        return bool(_LINK_RE.search(text))

    @staticmethod
    def is_spam(text: str) -> bool:
        """Check if text looks like spam.

        Heuristics: repeated characters, excessive caps, very short + emoji.
        """
        # Excessive repeated characters (e.g., "aaaaaaa")
        if re.search(r"(.)\1{6,}", text):
            return True
        # Excessive ALL CAPS (>70% uppercase)
        alpha_chars = [c for c in text if c.isalpha()]
        if alpha_chars:
            upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
            if upper_ratio > 0.7 and len(alpha_chars) > 10:
                return True
        # Very short with many emojis (likely spam)
        emoji_count = len(re.findall(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF]", text))
        if len(text) < 10 and emoji_count >= 4:
            return True
        return False

    @staticmethod
    def is_flooding(user_id: int) -> bool:
        """Check if user is flooding (too many messages in short time)."""
        now = time.time()
        if user_id not in ModerationEngine._flood_tracker:
            ModerationEngine._flood_tracker[user_id] = [now]
            return False

        timestamps = ModerationEngine._flood_tracker[user_id]
        # Remove old timestamps outside the window
        timestamps = [t for t in timestamps if now - t < ModerationEngine.FLOOD_WINDOW_SECONDS]
        timestamps.append(now)
        ModerationEngine._flood_tracker[user_id] = timestamps

        return len(timestamps) > ModerationEngine.FLOOD_MAX_MESSAGES

    # ------------------------------------------------------------------
    # Reputation & warnings
    # ------------------------------------------------------------------

    @staticmethod
    def get_reputation(user_id: int, chat_id: int) -> UserReputation | None:
        """Get user reputation record."""
        engine = _sync_engine()
        with Session(engine) as session:
            return session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()

    @staticmethod
    def add_warning(user_id: int, chat_id: int, reason: str = "") -> int:
        """Add a warning to a user. Returns total warning count."""
        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is None:
                rep = UserReputation(
                    user_id=user_id,
                    chat_id=chat_id,
                    reputation=0,
                    warnings=1,
                    is_muted=False,
                )
            else:
                rep.warnings += 1
                rep.reputation = max(0, rep.reputation - 5)
            session.add(rep)
            session.commit()
            session.refresh(rep)
            logger.info(
                "user_warned",
                user_id=user_id,
                chat_id=chat_id,
                warnings=rep.warnings,
                reason=reason,
            )
            return rep.warnings

    @staticmethod
    def clear_warnings(user_id: int, chat_id: int) -> None:
        """Clear all warnings for a user."""
        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is not None:
                rep.warnings = 0
                rep.is_muted = False
                session.add(rep)
                session.commit()

    @staticmethod
    def mute_user(user_id: int, chat_id: int, duration_minutes: int = 30) -> None:
        """Mute a user (set is_muted flag and mute_until timestamp)."""
        from datetime import datetime, timedelta, timezone

        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is None:
                rep = UserReputation(
                    user_id=user_id,
                    chat_id=chat_id,
                    reputation=0,
                    warnings=0,
                    is_muted=True,
                    mute_until=datetime.now(timezone.utc)
                    + timedelta(minutes=duration_minutes),
                )
            else:
                rep.is_muted = True
                rep.mute_until = datetime.now(timezone.utc) + timedelta(
                    minutes=duration_minutes
                )
            session.add(rep)
            session.commit()
            logger.info(
                "user_muted",
                user_id=user_id,
                chat_id=chat_id,
                duration_minutes=duration_minutes,
            )

    @staticmethod
    def unmute_user(user_id: int, chat_id: int) -> None:
        """Unmute a user."""
        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is not None:
                rep.is_muted = False
                rep.mute_until = None
                session.add(rep)
                session.commit()

    @staticmethod
    def is_muted(user_id: int, chat_id: int) -> bool:
        """Check if a user is currently muted."""
        from datetime import datetime, timezone

        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is None or not rep.is_muted:
                return False
            # Check if mute has expired
            if rep.mute_until is not None and rep.mute_until <= datetime.now(
                timezone.utc
            ):
                rep.is_muted = False
                rep.mute_until = None
                session.add(rep)
                session.commit()
                return False
            return True

    @staticmethod
    def adjust_reputation(user_id: int, chat_id: int, delta: int) -> int:
        """Adjust user reputation by delta. Returns new reputation."""
        engine = _sync_engine()
        with Session(engine) as session:
            rep = session.exec(
                select(UserReputation).where(
                    UserReputation.user_id == user_id,
                    UserReputation.chat_id == chat_id,
                )
            ).first()
            if rep is None:
                rep = UserReputation(
                    user_id=user_id,
                    chat_id=chat_id,
                    reputation=max(0, delta),
                    warnings=0,
                )
            else:
                rep.reputation = max(0, rep.reputation + delta)
            session.add(rep)
            session.commit()
            session.refresh(rep)
            return rep.reputation

    # ------------------------------------------------------------------
    # Full moderation check
    # ------------------------------------------------------------------

    @staticmethod
    def check_message(
        user_id: int,
        chat_id: int,
        text: str,
    ) -> dict[str, Any]:
        """Run all moderation checks on a message.

        Returns dict with:
            allowed: bool - whether the message should be allowed
            reasons: list[str] - list of violation reasons
            action: str - "allow", "warn", "mute"
        """
        cfg = ModerationEngine.get_config(chat_id)
        if cfg is None:
            # No config = moderation not enabled
            return {"allowed": True, "reasons": [], "action": "allow"}

        reasons: list[str] = []

        # Anti-spam check
        if cfg.anti_spam and ModerationEngine.is_spam(text):
            reasons.append("spam")

        # Anti-flood check
        if cfg.anti_flood and ModerationEngine.is_flooding(user_id):
            reasons.append("flood")

        # Link filter
        if cfg.link_filter and ModerationEngine.has_links(text):
            reasons.append("links")

        # Profanity filter
        if cfg.profanity_filter and ModerationEngine.has_profanity(text):
            reasons.append("profanity")

        # Check if muted
        if ModerationEngine.is_muted(user_id, chat_id):
            return {"allowed": False, "reasons": ["muted"], "action": "block"}

        if not reasons:
            return {"allowed": True, "reasons": [], "action": "allow"}

        # Determine action
        warnings = ModerationEngine.add_warning(user_id, chat_id, reason=", ".join(reasons))
        if warnings >= cfg.max_warnings:
            ModerationEngine.mute_user(user_id, chat_id, cfg.mute_duration_minutes)
            return {"allowed": False, "reasons": reasons, "action": "mute"}

        return {"allowed": False, "reasons": reasons, "action": "warn"}
