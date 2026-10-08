"""Global deny-by-default access guard for the Telegram surface (P0-2).

``AuthMiddleware.is_allowed`` was previously consulted in only two code
paths (``on_message`` and ``/imagine``), leaving ~80 other commands
reachable by anyone who finds the bot. This module plugs that hole at a
single choke point: :class:`AccessGuardHandler` is registered in
handler **group -1**, i.e. before every other handler.

The trick is in ``check_update``: it returns ``True`` **only for
updates from users the allow-list rejects**. PTB then runs the guard's
callback (rate-limited denial + audit log) and the callback finishes by
raising :class:`telegram.ext.ApplicationHandlerStop`, which is the
*only* mechanism that stops PTB from evaluating the remaining handler
groups — ``Application.process_update`` iterates every group in order
and treats ``ApplicationHandlerStop`` as a hard stop. Without that
raise, a denial in group -1 would be followed by the real command
handler in group 0 (fail-open). For allowed users ``check_update``
returns ``False`` and the update flows through as if the guard did not
exist — no per-command changes required, no double-processing.

Two PTB contract details this handler must honour (verified against
``telegram.ext`` v21.0 and v22.8 — ``BaseHandler.check_update`` and
``Application.process_update`` source):

1. ``check_update`` is a **synchronous** method: the dispatcher calls
   it as ``check = handler.check_update(update)`` *without* awaiting.
   An ``async def check_update`` would return a coroutine, which is
   always truthy — the guard would then run for *every* update,
   including allowed users, and the allow-list would never be
   consulted. It must stay a plain ``def``.
2. Only ``ApplicationHandlerStop`` (or an error handler that swallows
   the error and returns ``True``) stops later groups; ``block=True``
   merely makes the dispatcher ``await`` the callback instead of
   scheduling it with ``create_task`` (relevant when
   ``concurrent_updates=True``).

Updates without an ``effective_user`` (rare; e.g. some channel service
messages) are let through: every real command handler degrades safely
without a user, and logging a wall of denials for non-user updates
would just be noise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram import Update
from telegram.ext import ApplicationHandlerStop, BaseHandler, CallbackContext

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

    def check_update(self, update: Any) -> bool:
        """Only *denied* updates are handled — i.e. blocked from further handlers.

        Synchronous by PTB contract: ``Application.process_update`` calls this
        without awaiting, so an ``async def`` override would return a truthy
        coroutine for *every* update and the allow-list would never run.
        """
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
            # Flooded denials: drop silently, keep the audit trail — but the
            # update is still denied, so it must never reach another group.
            logger.warning("access_denied_dropped", user_id=user_id, command=command)
            raise ApplicationHandlerStop

        logger.warning("access_denied", user_id=user_id, command=command)
        query = update.callback_query
        if query is not None:
            try:
                await query.answer(_DENIAL_ALERT, show_alert=True)
            except Exception:  # noqa: BLE001 — denial UX must never raise
                logger.exception("access_denial_answer_failed", user_id=user_id)
            raise ApplicationHandlerStop
        if message is not None:
            try:
                await message.reply_text(_DENIAL_TEXT)
            except Exception:  # noqa: BLE001
                logger.exception("access_denial_reply_failed", user_id=user_id)
        # Every denial path ends here: without the stop, PTB would continue
        # with group 0 and execute the very command we just denied.
        raise ApplicationHandlerStop


def build_access_guard(settings: Settings) -> AccessGuardHandler:
    """Build the guard from bot allow-list settings (deny-by-default)."""
    auth = AuthMiddleware(settings.allowed_user_ids, settings.owner_telegram_id)
    return AccessGuardHandler(auth.is_allowed)
