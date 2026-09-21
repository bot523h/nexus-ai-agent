"""Framework-free Telegram command surface for the ``features/`` package.

Each module in this package holds the real implementation behind a group of
``/commands``. They are plain coroutines taking ``(update, context)`` — so they
are registrable as-is with PTB's ``CommandHandler`` / ``CallbackQueryHandler``
/ ``MessageHandler`` — but they never ``import telegram`` (see :mod:`._ptb`),
which keeps them inside the frozen import boundary and unit-testable with
fakes.

Scope of this package (agent B, session ``01a0c634``): only the command groups
that ``bot/handlers.py`` still ships as hard-coded stubs after the merged
PR#34. Everything PR#34 already wired — ``/calc``, ``/tr``, ``/convert``,
``/remind*``, the games, ``/anon_*``, referrals, force-join — lives in
``bot/feature_handlers.py`` and is deliberately **not** duplicated here: two
handlers for one command is exactly the "dead code behind the live handler"
defect the audit exists to remove.

Modules
-------
``gamification``  ``/daily`` ``/profile`` ``/achievements`` ``/xp_leaderboard``
``docs``          ``/docs`` ``/doc_delete`` ``/chat_with_doc`` (+ free-text routing)
"""

from __future__ import annotations

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

__all__ = [
    "achievements_cmd",
    "chat_with_doc_cmd",
    "clear_session",
    "doc_delete_cmd",
    "docs_list_cmd",
    "profile_cmd",
    "route_doc_text",
    "session_for",
    "xp_leaderboard_cmd",
]

#: Command name → handler, for the composition root (``bot/handlers.py``).
COMMAND_HANDLERS: dict[str, object] = {
    "daily": daily_cmd,
    "profile": profile_cmd,
    "achievements": achievements_cmd,
    "xp_leaderboard": xp_leaderboard_cmd,
    "docs": docs_list_cmd,
    "doc_delete": doc_delete_cmd,
    "chat_with_doc": chat_with_doc_cmd,
}
