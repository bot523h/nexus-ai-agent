"""Force-join system for NEXUS AI Telegram bot.

Ensures users join the required channel (@nexus_ai_official) before they
can interact with the bot. Provides membership verification via the
Telegram API, cached checks, and anti-bypass logic.

All non-public commands are blocked until the user is verified as a
channel member.
"""

from __future__ import annotations

import time
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ForceJoinConfig

logger = get_logger(__name__)

# Default channel that users must join
DEFAULT_CHANNEL = "@nexus_ai_official"

# Membership cache: (user_id, channel) → (is_member: bool, timestamp: float)
_membership_cache: dict[tuple[int, str], tuple[bool, float]] = {}
_CACHE_TTL = 300.0  # 5 minutes
#: ``ChatMember`` statuses that count as "joined".
_MEMBER_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})

# Public commands that are always allowed (even without joining)
_PUBLIC_COMMANDS = frozenset({"start", "help", "forcejoin_status"})


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class ForceJoinManager:
    """Manages the force-join gate for the bot.

    The Telegram ``bot`` can be attached after construction with
    :meth:`bind_bot` (handlers only see it on the PTB context).  Without a
    bot the membership check **fails closed**: the previous behaviour
    ("cannot verify → treat as member") made the gate accept everyone, which
    is the opposite of the anti-bypass promise (audit finding P0-3).
    """

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot

    def bind_bot(self, bot: Any) -> None:
        """Attach (or replace) the bot used for ``get_chat_member`` calls."""
        if bot is not None:
            self.bot = bot

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def get_config(chat_id: int) -> ForceJoinConfig | None:
        """Return the force-join config for *chat_id*, or None."""
        engine = _sync_engine()
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
        engine = _sync_engine()
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

    async def check_membership(
        self, user_id: int, channel: str = "", *, bot: Any | None = None
    ) -> bool:
        """Check if *user_id* is a member of *channel* (cached for 5 minutes).

        Only a *positive* answer is cached: a failed or unverifiable check must
        be retried on the next attempt (e.g. right after the user joins).
        Without any bot instance the answer is ``False`` (fail closed).
        """
        ch = channel or DEFAULT_CHANNEL
        now = time.monotonic()

        cached = _membership_cache.get((user_id, ch))
        if cached is not None:
            is_member, ts = cached
            if now - ts < _CACHE_TTL:
                return is_member

        client = bot if bot is not None else self.bot
        if client is None:
            logger.warning("forcejoin_no_bot_fail_closed", user_id=user_id, channel=ch)
            return False
        try:
            member = await client.get_chat_member(chat_id=ch, user_id=user_id)
            status = getattr(member, "status", "")
            is_member = status in _MEMBER_STATUSES
        except Exception:  # noqa: BLE001
            logger.warning("forcejoin_check_failed", user_id=user_id, channel=ch)
            is_member = False

        if is_member:
            _membership_cache[(user_id, ch)] = (True, now)
        return is_member

    # ------------------------------------------------------------------
    # Gate logic
    # ------------------------------------------------------------------

    def is_command_allowed(self, command: str) -> bool:
        """Return True if *command* is a public command (always allowed)."""
        return command in _PUBLIC_COMMANDS

    @staticmethod
    def required_channel(chat_id: int | None) -> str | None:
        """Channel the members of *chat_id* must join, or ``None`` when the gate is off.

        Falls back to any enabled config when *chat_id* is unknown (private
        chats share the bot-wide gate).
        """
        engine = _sync_engine()
        with Session(engine) as session:
            cfg = None
            if chat_id is not None:
                cfg = session.exec(
                    select(ForceJoinConfig).where(ForceJoinConfig.chat_id == chat_id)
                ).first()
            if cfg is None:
                # ``is True`` compared the column object by identity (always False)
                # in the previous version, so the gate could never engage.
                cfg = session.exec(
                    select(ForceJoinConfig).where(col(ForceJoinConfig.enabled).is_(True))
                ).first()
            if cfg is None or not cfg.enabled:
                return None
            return cfg.channel_username or DEFAULT_CHANNEL

    async def should_block(
        self,
        user_id: int,
        command: str = "",
        *,
        chat_id: int | None = None,
        bot: Any | None = None,
    ) -> bool:
        """Return True if *user_id* should be blocked.

        Checks: (1) is force-join enabled for this chat (or anywhere)? (2) is
        the command public? (3) is the user a member of the required channel?
        """
        if command and self.is_command_allowed(command):
            return False
        channel = self.required_channel(chat_id)
        if channel is None:
            return False
        return not await self.check_membership(user_id, channel, bot=bot)

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
        """Remove cached membership status for *user_id* (all channels)."""
        for key in [k for k in _membership_cache if k[0] == user_id]:
            _membership_cache.pop(key, None)
