"""Framework-free Telegram command surface for the ``features/`` package (B1–B8).

Each module holds the real implementation behind a group of ``/commands``
that ``bot/handlers.py`` currently ships as demo stubs.  The handlers take the
usual ``(update, context)`` pair and are registrable as-is with
``CommandHandler``/``CallbackQueryHandler``/``PollAnswerHandler``; they just
never import ``telegram`` (see :mod:`._ptb`), which keeps them inside the
frozen import-boundary baseline and testable with plain fakes.

Wiring (replacing the stubs and registering the extra handlers) happens in
``bot/handlers.py`` and is a separate step owned by the security batch.
"""

from __future__ import annotations

from .community import (
    anon_report_cmd,
    anon_start_cmd,
    anon_stop_cmd,
    forcejoin_gate,
    forcejoin_verify_callback,
    route_anon_text,
)
from .docs import chat_with_doc_cmd, doc_delete_cmd, docs_list_cmd, route_doc_chat
from .games import (
    guess_cmd,
    guess_start_cmd,
    guess_stop_cmd,
    leaderboard_cmd,
    poll_answer_handler,
    poll_cmd,
    poll_results_cmd,
    quiz_cmd,
    route_game_text,
    wordle_cmd,
    wordle_stop_cmd,
)
from .gamification import achievements_cmd, daily_cmd, profile_cmd, xp_leaderboard_cmd
from .referral import apply_start_referral
from .tools import calc_cmd, convert_cmd, remind_cmd, reminders_cmd, tr_cmd

__all__ = [
    "achievements_cmd",
    "anon_report_cmd",
    "anon_start_cmd",
    "anon_stop_cmd",
    "apply_start_referral",
    "calc_cmd",
    "chat_with_doc_cmd",
    "convert_cmd",
    "daily_cmd",
    "doc_delete_cmd",
    "docs_list_cmd",
    "forcejoin_gate",
    "forcejoin_verify_callback",
    "guess_cmd",
    "guess_start_cmd",
    "guess_stop_cmd",
    "leaderboard_cmd",
    "poll_answer_handler",
    "poll_cmd",
    "poll_results_cmd",
    "profile_cmd",
    "quiz_cmd",
    "remind_cmd",
    "reminders_cmd",
    "route_anon_text",
    "route_doc_chat",
    "route_game_text",
    "tr_cmd",
    "wordle_cmd",
    "wordle_stop_cmd",
    "xp_leaderboard_cmd",
]
