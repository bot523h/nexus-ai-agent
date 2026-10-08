"""Framework-free Telegram command surface for the ``features/`` package.

Each module in this package holds the real implementation behind a group of
``/commands``. They are plain coroutines taking ``(update, context)`` — so they
are registrable as-is with PTB's ``CommandHandler`` / ``CallbackQueryHandler``
/ ``MessageHandler`` — but they never ``import telegram`` (see :mod:`._ptb`),
which keeps them inside the frozen import boundary and unit-testable with
fakes.

One further invariant, asserted by ``tests/unit/test_surface_onboarding.py``:
importing this package must not *transitively* pull ``telegram`` either, so a
module-level import of an engine that imports PTB itself (``channel_manager``,
``onboarding``) is resolved lazily inside the function that needs it.

Scope of this package: only the command groups that ``bot/handlers.py`` still
ships as hard-coded stubs. Everything ``bot/feature_handlers.py`` already wires —
``/calc``, ``/tr``, ``/convert``, ``/remind*``, the games, ``/anon_*``,
referrals, force-join — is deliberately **not** duplicated here: two handlers
for one command is exactly the "dead code behind the live handler" defect the
audit exists to remove.

Modules
-------
``gamification``       ``/daily`` ``/profile`` ``/achievements`` ``/xp_leaderboard``
``docs``               ``/docs`` ``/doc_delete`` ``/chat_with_doc`` (+ free-text routing)
``ads``                ``/ad_create`` ``/ad_list`` ``/ad_pause`` ``/ad_resume``
                       ``/ad_delete`` ``/ad_stats``
``channel_management`` ``/post`` ``/schedule`` ``/pin`` ``/ban`` ``/unban`` ``/stats``
                       ``/welcome``
``onboarding``         the ``onboarding_*`` inline-keyboard callbacks
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .ads import (
    ad_create_cmd,
    ad_delete_cmd,
    ad_list_cmd,
    ad_pause_cmd,
    ad_resume_cmd,
    ad_stats_cmd,
)
from .channel_management import (
    ban_cmd,
    pin_cmd,
    post_cmd,
    schedule_cmd,
    stats_cmd,
    unban_cmd,
    welcome_cmd,
)
from .docs import (
    chat_with_doc_cmd,
    clear_session,
    doc_delete_cmd,
    docs_list_cmd,
    route_doc_text,
    session_for,
)
from .gamification import (
    achievements_cmd,
    daily_cmd,
    profile_cmd,
    xp_leaderboard_cmd,
)
from .onboarding import onboarding_callback_cmd

__all__ = [
    "achievements_cmd",
    "ad_create_cmd",
    "ad_delete_cmd",
    "ad_list_cmd",
    "ad_pause_cmd",
    "ad_resume_cmd",
    "ad_stats_cmd",
    "ban_cmd",
    "chat_with_doc_cmd",
    "clear_session",
    "daily_cmd",
    "doc_delete_cmd",
    "docs_list_cmd",
    "onboarding_callback_cmd",
    "pin_cmd",
    "post_cmd",
    "profile_cmd",
    "route_doc_text",
    "schedule_cmd",
    "session_for",
    "stats_cmd",
    "unban_cmd",
    "welcome_cmd",
    "xp_leaderboard_cmd",
]

#: Command name → handler, for the composition root (``bot/handlers.py``).
#: Typed as a coroutine, so a future non-async handler cannot slip in.
#: ``onboarding_callback_cmd`` is absent by design: it is a callback, not a
#: command, and ``test_no_command_is_registered_twice`` plus
#: ``test_the_surface_package_exports_exactly_these_commands`` read this map as
#: the command contract.
COMMAND_HANDLERS: dict[str, Callable[[Any, Any], Awaitable[None]]] = {
    "daily": daily_cmd,
    "profile": profile_cmd,
    "achievements": achievements_cmd,
    "xp_leaderboard": xp_leaderboard_cmd,
    "docs": docs_list_cmd,
    "doc_delete": doc_delete_cmd,
    "chat_with_doc": chat_with_doc_cmd,
    "ad_create": ad_create_cmd,
    "ad_list": ad_list_cmd,
    "ad_pause": ad_pause_cmd,
    "ad_resume": ad_resume_cmd,
    "ad_delete": ad_delete_cmd,
    "ad_stats": ad_stats_cmd,
    "post": post_cmd,
    "schedule": schedule_cmd,
    "pin": pin_cmd,
    "ban": ban_cmd,
    "unban": unban_cmd,
    "stats": stats_cmd,
    "welcome": welcome_cmd,
}
