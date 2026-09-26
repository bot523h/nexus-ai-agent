"""Smart Moderation System for NEXUS AI Telegram bot.

Provides anti-spam, anti-flood, link filtering, profanity filtering,
warning system, and user reputation tracking.

No paid packages — SQLite for persistence, heuristic-based filtering.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any, ClassVar

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.timeutil import as_naive_utc, utcnow
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ModerationConfig, UserReputation

logger = get_logger(__name__)

# One engine per database path, not one per call.  Every public method here is
# a short synchronous CRUD hop; building a fresh SQLAlchemy engine (and with it
# a fresh connection pool) for each of them meant the moderation path paid a
# pool setup+teardown per message.  ``get_settings()`` is itself cached, but the
# key is kept explicit so a test that repoints ``db_path`` gets its own engine
# instead of silently reusing the previous database.
_ENGINES: dict[str, Any] = {}
_ENGINE_LOCK = threading.Lock()


def _sync_engine() -> Any:
    """Return the process-wide synchronous engine for the configured database."""
    from sqlalchemy import create_engine as _ce

    db_path = str(get_settings().db_path)
    with _ENGINE_LOCK:
        engine = _ENGINES.get(db_path)
        if engine is None:
            engine = _ce(f"sqlite:///{db_path}", echo=False)
            _ENGINES[db_path] = engine
        return engine


# ---------------------------------------------------------------------------
# Persian profanity list (common patterns)
# ---------------------------------------------------------------------------
#
# Two defects were fixed here and are frozen by
# ``tests/unit/test_moderation_engine.py``:
#
# 1. **Substring matching punished innocent words.**  The patterns were joined
#    into one bare alternation and matched with ``re.search``, so the entry
#    ``خر`` fired on ``خرید`` (to buy), ``آخر`` (last), ``خروج`` (exit) and
#    ``مخرب``; ``رید`` fired on ``بگیرید``/``گردید``/``خرید``.  Every one of
#    those is an ordinary word, and a hit costs the author a warning and — at
#    ``max_warnings`` — a mute.  Each term is now anchored between
#    non-word/non-ZWNJ boundaries.  ``\b`` alone is not enough for Persian:
#    U+200C ZERO WIDTH NON-JOINER glues ``می‌خرید`` together while counting as
#    a non-word character, so it has to be named explicitly.
# 2. **``مغز`` ("brain") is not profanity in any context** and was removed; the
#    remaining terms are insults on their own as whole words.
_PROFANITY_TERMS: tuple[str, ...] = (
    "خرف",
    "احمق",
    "دیوانه",
    "کثیف",
    "حقیر",
    "نادان",
    "ابله",
    "رید",
    "خر",
    "گوساله",
    "سگ",
)

#: ZERO WIDTH NON-JOINER — inside a Persian word, but not a ``\w`` character.
_ZWNJ = "\u200c"


def _whole_word(term: str) -> str:
    """Anchor *term* so it only matches as a standalone word."""
    return rf"(?<![\w{_ZWNJ}]){re.escape(term)}(?![\w{_ZWNJ}])"


_PROFANITY_RE = re.compile(
    "|".join(_whole_word(term) for term in _PROFANITY_TERMS),
    re.IGNORECASE,
)

# Link detection regex
_LINK_RE = re.compile(r"https?://[^\s<>\"]+|t\.me/[^\s<>\"]+|www\.[^\s<>\"]+", re.IGNORECASE)


class ModerationEngine:
    """Smart moderation: anti-spam, flood, link filter, profanity, warnings."""

    #: In-memory flood tracking: user_id -> timestamps inside the window.
    #: **Bounded** (LRU-evicted at :data:`FLOOD_TRACKER_MAX_USERS`): the old
    #: plain ``dict`` grew one entry per user id forever, so a long-running
    #: process in a busy group leaked memory that nothing ever reclaimed.
    #: Eviction is safe by construction — a dropped user simply starts a new
    #: window, which is exactly what an expired window means.
    _flood_tracker: ClassVar[OrderedDict[int, list[float]]] = OrderedDict()
    _flood_lock: ClassVar[threading.Lock] = threading.Lock()

    # Rate limits
    FLOOD_WINDOW_SECONDS = 5
    FLOOD_MAX_MESSAGES = 5
    #: Upper bound on tracked users (see :attr:`_flood_tracker`).
    FLOOD_TRACKER_MAX_USERS = 10_000

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
                    profanity_filter=(profanity_filter if profanity_filter is not None else True),
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
        """Check if user is flooding (too many messages in short time).

        Uses :func:`time.monotonic`, not :func:`time.time`: a wall-clock step
        (NTP correction, container clock sync) must not be able to erase or
        fabricate a flood window.
        """
        now = time.monotonic()
        window = ModerationEngine.FLOOD_WINDOW_SECONDS
        with ModerationEngine._flood_lock:
            tracker = ModerationEngine._flood_tracker
            timestamps = [t for t in tracker.get(user_id, ()) if now - t < window]
            timestamps.append(now)
            tracker[user_id] = timestamps
            tracker.move_to_end(user_id)
            while len(tracker) > ModerationEngine.FLOOD_TRACKER_MAX_USERS:
                tracker.popitem(last=False)
            return len(timestamps) > ModerationEngine.FLOOD_MAX_MESSAGES

    @staticmethod
    def reset_flood_tracking(user_id: int | None = None) -> None:
        """Drop one user's flood window, or all of them (tests/operators)."""
        with ModerationEngine._flood_lock:
            if user_id is None:
                ModerationEngine._flood_tracker.clear()
            else:
                ModerationEngine._flood_tracker.pop(user_id, None)

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
        """Mute a user (set is_muted flag and mute_until timestamp).

        ``mute_until`` is written as **naive UTC** (``core.timeutil.utcnow``).
        The previous spelling stored ``datetime.now(timezone.utc)`` — an aware
        value — into a column SQLite persists without an offset, so the very
        next :meth:`is_muted` read compared a naive value against an aware one
        and raised ``TypeError: can't compare offset-naive and offset-aware
        datetimes``: every muted user crashed the moderation path.
        """
        from datetime import timedelta

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
                    mute_until=utcnow() + timedelta(minutes=duration_minutes),
                )
            else:
                rep.is_muted = True
                rep.mute_until = utcnow() + timedelta(minutes=duration_minutes)
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
        """Check if a user is currently muted.

        The stored deadline is normalised with
        :func:`~nexus_ai_agent.core.timeutil.as_naive_utc` before the
        comparison, so a row written by an older release (aware) or by
        PostgreSQL (``timestamptz``) is handled without raising.
        """
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
            deadline = as_naive_utc(rep.mute_until)
            if deadline is not None and deadline <= utcnow():
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
