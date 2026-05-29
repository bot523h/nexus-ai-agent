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
    assert {"start", "online", "disconnect", "storage", "model"}.issubset(commands)
