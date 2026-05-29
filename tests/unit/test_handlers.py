from __future__ import annotations

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.presence import PresenceStore


def test_build_handlers_includes_required_commands(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(object(), lambda: None, Settings(), presence=PresenceStore())
    commands = {
        next(iter(handler.commands)) for handler in handlers if hasattr(handler, "commands")
    }
    # Phase 0 (original)
    assert {"start", "online", "disconnect", "storage", "model"}.issubset(commands)
    # Phase 1: Channel & Group Management
    assert {"post", "schedule", "ban", "unban", "stats", "welcome", "pin"}.issubset(commands)
    # Phase 2: Anonymous Chat
    assert {"anon_start", "anon_stop", "anon_report"}.issubset(commands)
    # Phase 3: Games
    assert {"quiz", "leaderboard", "guess_start", "guess_stop", "wordle", "wordle_stop", "poll"}.issubset(
        commands
    )
    # Phase 4: Utility Tools
    assert {"remind", "tr", "convert", "calc"}.issubset(commands)


def test_build_handlers_includes_callback_handlers(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(object(), lambda: None, Settings(), presence=PresenceStore())
    # Check we have CallbackQueryHandler instances
    from telegram.ext import CallbackQueryHandler

    callback_handlers = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
    assert len(callback_handlers) >= 3  # quiz, poll, menu callbacks
