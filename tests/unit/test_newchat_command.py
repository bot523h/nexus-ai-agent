"""``/newchat`` must reset the real conversation memory, or say so honestly.

The reply path resumes each chat through the compiled graph's checkpointer
(``thread_id = f"tg:{chat_id}"``); ``ConversationStore`` is never read on that
path. Before this fix ``/newchat`` answered "Conversation history cleared."
without touching any state — a fake-success surface whose next message simply
continued the old thread.

These tests drive the real handler built by ``build_handlers`` and pin three
outcomes:

* a checkpointer that can drop the thread → the thread is dropped and success
  is reported;
* no checkpointer (no resumable memory) → an honest "nothing to clear", never
  the success string;
* a failing delete → a typed failure, never the success string.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.presence import PresenceStore

OWNER_ID = 111
CHAT_ID = 222

#: The exact string the old stub replied with; it must never come back.
_OLD_FAKE_SUCCESS = "🔄 Conversation history cleared."


class _FakeMessage:
    def __init__(self) -> None:
        self.text = ""
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, user_id: int = OWNER_ID, chat_id: int = CHAT_ID) -> None:
        self.effective_user = SimpleNamespace(id=user_id, username="tester")
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage()
        self.edited_message = None
        self.callback_query = None


def _newchat_handler(handlers: list[Any]) -> Any:
    for handler in handlers:
        commands = getattr(handler, "commands", None)
        if commands and set(commands) == {"newchat"}:
            return handler
    raise AssertionError("handler for /newchat not found")


@pytest.fixture()
def bot_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    settings_module.get_settings.cache_clear()

    from nexus_ai_agent.features import owner_control

    owner_control._owner_id = None  # type: ignore[attr-defined]

    from nexus_ai_agent.storage import db as db_module

    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    return settings_module.get_settings()


async def _invoke(handlers: list[Any], update: _FakeUpdate) -> list[str]:
    handler = _newchat_handler(handlers)
    context = SimpleNamespace(args=[], bot=SimpleNamespace())
    await handler.callback(update, context)
    assert update.message.replies, "/newchat did not reply"
    return update.message.replies


def _build(settings: Any, graph: Any) -> list[Any]:
    return build_handlers(graph, lambda: None, settings, PresenceStore(), object())


@pytest.mark.asyncio
async def test_newchat_drops_the_thread_and_reports_success(bot_settings: Any) -> None:
    dropped: list[str] = []

    async def adelete_thread(thread_id: str) -> None:
        dropped.append(thread_id)

    graph = SimpleNamespace(checkpointer=SimpleNamespace(adelete_thread=adelete_thread))
    handlers = _build(bot_settings, graph)
    replies = await _invoke(handlers, _FakeUpdate())

    assert dropped == [f"tg:{CHAT_ID}"]
    assert _OLD_FAKE_SUCCESS not in replies[0]
    assert "🔄" in replies[0]


@pytest.mark.asyncio
async def test_newchat_without_checkpointer_never_claims_success(bot_settings: Any) -> None:
    handlers = _build(bot_settings, object())  # no ``checkpointer`` attribute at all
    replies = await _invoke(handlers, _FakeUpdate())

    assert _OLD_FAKE_SUCCESS not in replies[0]
    assert "🔄" not in replies[0]
    assert "ℹ️" in replies[0]


@pytest.mark.asyncio
async def test_newchat_reports_a_failed_clear(bot_settings: Any) -> None:
    async def adelete_thread(thread_id: str) -> None:
        raise RuntimeError("store is locked")

    graph = SimpleNamespace(checkpointer=SimpleNamespace(adelete_thread=adelete_thread))
    handlers = _build(bot_settings, graph)
    replies = await _invoke(handlers, _FakeUpdate())

    assert _OLD_FAKE_SUCCESS not in replies[0]
    assert "❌" in replies[0]
    assert "store is locked" in replies[0]
