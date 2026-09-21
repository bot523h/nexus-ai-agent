"""Global deny-by-default access guard for the Telegram surface (P0-2).

``AuthMiddleware.is_allowed`` was previously consulted in only two code
paths (``on_message`` and ``/imagine``), leaving ~80 other commands
reachable by anyone who finds the bot. This module plugs that hole at a
single choke point: :class:`AccessGuardHandler` is registered in
handler **group -1**, i.e. before every other handler.

The trick is in ``check_update``: it returns ``True`` **only for
updates from users the allow-list rejects**. PTB then runs the guard's
callback (rate-limited denial + audit log) and *blocks* the update from
reaching any other handler. For allowed users ``check_update`` returns
``False`` and the update flows through as if the guard did not exist —
no per-command changes required, no double-processing.

Updates without an ``effective_user`` (rare; e.g. some channel service
messages) are let through: every real command handler degrades safely
without a user, and logging a wall of denials for non-user updates
would just be noise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram import Update
from telegram.ext import BaseHandler, CallbackContext

from nexus_ai_agent.bot.middleware import AuthMiddleware, RateLimiter
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

_DENIAL_TEXT = "⛔ این ربات خصوصی است و دسترسی شما مجاز نیست."
_DENIAL_ALERT = "دسترسی ندارید"
#: How often we bother a rejected user with a reply; the rest is dropped
#: silently (but still logged) so spammers cannot flood us.
_DENIAL_REPLY_LIMIT = 3
_DENIAL_REPLY_WINDOW = 60


class AccessGuardHandler(BaseHandler[Update, CallbackContext, None]):
    """Intercepts updates from unauthorized users and blocks them."""

    def __init__(
        self,
        is_allowed: Callable[[int], bool],
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(callback=self._denied, block=True)
        self._is_allowed = is_allowed
        self._limiter = rate_limiter or RateLimiter(
            max_messages=_DENIAL_REPLY_LIMIT, window_seconds=_DENIAL_REPLY_WINDOW
        )

    async def check_update(self, update: Any) -> bool:
        """Only *denied* updates are handled — i.e. blocked from further handlers."""
        user = getattr(update, "effective_user", None)
        if user is None:
            return False
        return not self._is_allowed(int(user.id))

    async def _denied(self, update: Any, context: CallbackContext) -> None:
        user = update.effective_user
        user_id = int(user.id)
        command = ""
        message = update.message or update.edited_message
        if message is not None and message.text:
            command = (message.text.split() or [""])[0]

        if not self._limiter.is_allowed(user_id):
            # Flooded denials: drop silently, keep the audit trail.
            logger.warning("access_denied_dropped", user_id=user_id, command=command)
            return

        logger.warning("access_denied", user_id=user_id, command=command)
        query = update.callback_query
        if query is not None:
            try:
                await query.answer(_DENIAL_ALERT, show_alert=True)
            except Exception:  # noqa: BLE001 — denial UX must never raise
                logger.exception("access_denial_answer_failed", user_id=user_id)
            return
        if message is not None:
            try:
                await message.reply_text(_DENIAL_TEXT)
            except Exception:  # noqa: BLE001
                logger.exception("access_denial_reply_failed", user_id=user_id)


def build_access_guard(settings: Settings) -> AccessGuardHandler:
    """Build the guard from bot allow-list settings (deny-by-default)."""
    auth = AuthMiddleware(settings.allowed_user_ids, settings.owner_telegram_id)
    return AccessGuardHandler(auth.is_allowed)
