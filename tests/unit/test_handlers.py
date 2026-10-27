from __future__ import annotations

import pytest

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.presence import PresenceStore


def test_build_handlers_includes_required_commands(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(object(), lambda: None, Settings(), PresenceStore(), object())
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
    assert {
        "quiz",
        "leaderboard",
        "guess_start",
        "guess_stop",
        "wordle",
        "wordle_stop",
        "poll",
    }.issubset(commands)
    # Phase 4: Utility Tools
    assert {"remind", "tr", "convert", "calc"}.issubset(commands)


def test_build_handlers_includes_callback_handlers(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    handlers = build_handlers(object(), lambda: None, Settings(), PresenceStore(), object())
    # Check we have CallbackQueryHandler instances
    from telegram.ext import CallbackQueryHandler

    callback_handlers = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
    assert len(callback_handlers) >= 3  # quiz, poll, menu callbacks


@pytest.mark.asyncio
async def test_menu_callback_lang_select(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_exec_res = MagicMock()
    mock_exec_res.first.return_value = None
    mock_session.exec.return_value = mock_exec_res

    def db_factory():
        class ContextManager:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, *args):
                pass

        return ContextManager()

    handlers = build_handlers(object(), db_factory, Settings(), PresenceStore(), object())

    from telegram.ext import CallbackQueryHandler

    menu_handler = next(
        h
        for h in handlers
        if isinstance(h, CallbackQueryHandler)
        and hasattr(h.callback, "__name__")
        and h.callback.__name__ == "menu_callback"
    )

    update = MagicMock()
    query = AsyncMock()
    query.data = "lang_fa"
    update.callback_query = query
    update.effective_user.id = 12345

    context = MagicMock()
    await menu_handler.callback(update, context)

    query.answer.assert_called_once()
    query.edit_message_text.assert_called_once()
    assert "فارسی" in query.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
async def test_agent_callback_handler_invalid(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from nexus_ai_agent.bot.agent_handlers import agent_callback_handler

    update = MagicMock()
    query = AsyncMock()
    query.data = "agent_select_nonexistent_123"
    query.from_user.id = 12345
    update.callback_query = query

    context = MagicMock()
    await agent_callback_handler(update, context)

    query.answer.assert_called_once()
    query.edit_message_text.assert_called_once_with("❌ خطا در فعال‌سازی Agent.")
