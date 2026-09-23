"""Force-join system for NEXUS AI Telegram bot.

Ensures users join the required channel (@nexus_ai_official) before they
can interact with the bot. Provides membership verification via the
Telegram API, cached checks, and anti-bypass logic.

All non-public commands are blocked until the user is verified as a
channel member.
"""

from __future__ import annotations

import asyncio
import time
from functools import lru_cache
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ForceJoinConfig

logger = get_logger(__name__)

# Default channel that users must join
DEFAULT_CHANNEL = "@nexus_ai_official"

# Membership cache: user_id → (is_member: bool, timestamp: float)
_membership_cache: dict[int, tuple[bool, float]] = {}
_CACHE_TTL = 300.0  # 5 minutes

# Public commands that are always allowed (even without joining)
_PUBLIC_COMMANDS = frozenset({"start", "help", "forcejoin_status"})


@lru_cache(maxsize=8)
def _sync_engine(db_path: str) -> Any:
    """Return a (cached) synchronous SQLAlchemy engine for feature CRUD.

    P1-2: the gate's DB read runs in worker threads (``asyncio.to_thread``),
    so the engine is created with ``check_same_thread=False``; caching it
    per path also removes the per-message engine churn of the old
    create-per-call behaviour.
    """
    from sqlalchemy import create_engine as _ce

    return _ce(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )


class ForceJoinManager:
    """Manages the force-join gate for the bot."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot

    def bind(self, bot: Any) -> None:
        """Attach (or replace) the Telegram bot (called at startup).

        Without a bot, :meth:`check_membership` cannot verify membership
        and fails open; binding at application startup closes that gap.
        """
        self.bot = bot

    @property
    def is_bound(self) -> bool:
        return self.bot is not None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def get_config(chat_id: int) -> ForceJoinConfig | None:
        """Return the force-join config for *chat_id*, or None."""
        engine = _sync_engine(get_settings().db_path)
        with Session(engine) as session:
            return session.exec(
                select(ForceJoinConfig).where(ForceJoinConfig.chat_id == chat_id)
            ).first()

    @staticmethod
    def set_config(
        chat_id: int,
        *,
        enabled: bool,
        channel_username: str = DEFAULT_CHANNEL,
        welcome_message: str = "",
    ) -> ForceJoinConfig:
        """Create or update force-join config for *chat_id*."""
        engine = _sync_engine(get_settings().db_path)
        with Session(engine) as session:
            existing = session.exec(
                select(ForceJoinConfig).where(ForceJoinConfig.chat_id == chat_id)
            ).first()
            if existing is not None:
                existing.enabled = enabled
                if channel_username:
                    existing.channel_username = channel_username
                if welcome_message:
                    existing.welcome_message = welcome_message
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing
            cfg = ForceJoinConfig(
                chat_id=chat_id,
                enabled=enabled,
                channel_username=channel_username,
                welcome_message=welcome_message or "⛔ لطفاً ابتدا در کانال عضو شوید.",
            )
            session.add(cfg)
            session.commit()
            session.refresh(cfg)
            return cfg

    # ------------------------------------------------------------------
    # Membership check
    # ------------------------------------------------------------------

    async def check_membership(self, user_id: int, channel: str = "") -> bool:
        """Check if *user_id* is a member of *channel*.

        Uses a 5-minute cache to avoid hitting the API on every message.
        """
        ch = channel or DEFAULT_CHANNEL
        now = time.monotonic()

        # Check cache
        cached = _membership_cache.get(user_id)
        if cached is not None:
            is_member, ts = cached
            if now - ts < _CACHE_TTL:
                return is_member

        # Ask Telegram API
        if self.bot is None:
            return True  # can't verify without bot
        try:
            member = await self.bot.get_chat_member(chat_id=ch, user_id=user_id)
            is_member = member.status in ("member", "administrator", "creator")
        except Exception:  # noqa: BLE001
            logger.warning("forcejoin_check_failed", user_id=user_id, channel=ch)
            is_member = False

        _membership_cache[user_id] = (is_member, now)
        return is_member

    # ------------------------------------------------------------------
    # Gate logic
    # ------------------------------------------------------------------

    def is_command_allowed(self, command: str) -> bool:
        """Return True if *command* is a public command (always allowed)."""
        return command in _PUBLIC_COMMANDS

    async def should_block(self, user_id: int, command: str = "") -> bool:
        """Return True if *user_id* should be blocked.

        Checks: (1) is force-join globally enabled? (2) is the command
        public? (3) is the user a member of the required channel?
        """
        # Public commands are never blocked
        if command and self.is_command_allowed(command):
            return False

        # Check if force-join is enabled anywhere.
        # P1-2: this runs on every message — the sync DB read goes to a
        # worker thread so it can never stall the event loop.
        if not await asyncio.to_thread(self._is_enabled_anywhere_sync):
            return False  # force-join not enabled anywhere

        # Check membership
        channel = DEFAULT_CHANNEL
        return not await self.check_membership(user_id, channel)

    def _is_enabled_anywhere_sync(self) -> bool:
        """Synchronous DB core for :meth:`should_block` (worker thread)."""
        engine = _sync_engine(get_settings().db_path)
        with Session(engine) as session:
            return (
                session.exec(
                    select(ForceJoinConfig).where(ForceJoinConfig.enabled == True)  # noqa: E712
                ).first()
                is not None
            )

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_join_keyboard(channel: str = DEFAULT_CHANNEL) -> Any:
        """Return an InlineKeyboardMarkup with Join + Verify buttons."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        # Strip @ for the URL
        ch_clean = channel.lstrip("@")
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📢 عضویت در کانال",
                        url=f"https://t.me/{ch_clean}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ تأیید عضویت",
                        callback_data="forcejoin_verify",
                    )
                ],
            ]
        )

    def invalidate_cache(self, user_id: int) -> None:
        """Remove cached membership status for *user_id*."""
        _membership_cache.pop(user_id, None)
