"""A-01 deeper validation: one free-text message, end to end, on the real factory.

``test_real_session_factory.py`` proves the helpers (``_upsert_user`` /
``_upsert_chat``) work on the production ``get_session`` factory.  This test
closes the remaining gap the audit named: nothing drove the *handler* that
crashed on ``main`` — ``on_message`` — through the real factory into a real
database file.  Here the handler list is built by ``build_handlers`` exactly as
``bot/app.py`` builds it, the schema is brought up the way ``nexus run`` does
it (``ensure_startup_schema`` → Alembic), the text handler is located by its
callback, a message is delivered, and the rows are read back from the SQLite
file with a fresh session.  The only stand-in is the LLM graph (``ainvoke`` returns a canned
response); there is no mock between the handler and the database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import select
from telegram.ext import MessageHandler

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.migrations import ensure_startup_schema
from nexus_ai_agent.storage.models import Chat, User

pytestmark = pytest.mark.asyncio

USER_ID = 4242
CHAT_ID = -100777


class _Message:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []
        self.photo: list[Any] = []
        self.document = None
        self.message_id = 1

    async def reply_text(self, text: str, **_: Any) -> None:
        self.replies.append(text)


class _Update:
    def __init__(self, text: str, *, username: str) -> None:
        self.effective_user = SimpleNamespace(
            id=USER_ID, username=username, first_name="t", language_code="en"
        )
        self.effective_chat = SimpleNamespace(id=CHAT_ID, type="private")
        self.message = _Message(text)
        self.effective_message = self.message
        self.edited_message = None
        self.callback_query = None


class _Graph:
    """Stands in for the compiled LangGraph: records the state, answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, config))
        text = state["messages"][-1]["content"]
        return {"intent": "chat", "response": f"echo:{text}"}


@pytest.fixture()
def message_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    db_file = tmp_path / "app.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(USER_ID))
    monkeypatch.setenv("NEXUS_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "false")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    try:
        settings = settings_module.get_settings()
        # Exactly what ``nexus run`` does before polling (cli.py): Alembic-first
        # schema bring-up.  The sync feature engines (force-join gate runs on
        # every message *before* ``_upsert_user``) never create tables.
        report = ensure_startup_schema()
        assert report == {"backend": "sqlite", "source": "alembic"}, report
        graph = _Graph()
        handlers = build_handlers(graph, get_session, settings, PresenceStore(), object())
        yield SimpleNamespace(db_file=db_file, graph=graph, handlers=handlers)
    finally:
        settings_module.get_settings.cache_clear()
        # The engine is left in place on purpose: the autouse
        # ``_dispose_db_engine`` fixture (tests/conftest.py) disposes it on the
        # live loop.  Dropping the reference here instead leaks the aiosqlite
        # worker thread, which later dies with "Event loop is closed".


def _text_handler(handlers: list[Any]) -> MessageHandler:  # type: ignore[type-arg]
    """The free-text route: the MessageHandler whose callback is ``on_message``."""
    for handler in handlers:
        if isinstance(handler, MessageHandler) and handler.callback.__name__ == "on_message":
            return handler
    raise AssertionError("free-text MessageHandler (on_message) not registered")


async def _rows() -> tuple[list[User], list[Chat]]:
    async with get_session() as session:
        users = list((await session.exec(select(User))).all())
        chats = list((await session.exec(select(Chat))).all())
    return users, chats


async def test_free_text_message_persists_user_and_chat_through_the_real_factory(
    message_env: Any,
) -> None:
    handler = _text_handler(message_env.handlers)
    update = _Update("سلام نکسوس", username="first")

    await handler.callback(update, SimpleNamespace(args=[], bot=None, bot_data={}))

    assert update.message.replies == ["echo:سلام نکسوس"], update.message.replies
    assert message_env.db_file.exists(), "the configured NEXUS_DB_PATH file was never created"
    users, chats = await _rows()
    assert [(u.telegram_id, u.username) for u in users] == [(USER_ID, "first")]
    assert [(c.chat_id, c.thread_id) for c in chats] == [(CHAT_ID, f"tg:{CHAT_ID}")]
    (state, config), *_ = message_env.graph.calls
    assert config == {"configurable": {"thread_id": f"tg:{CHAT_ID}"}}
    assert state["correlation_id"] and state["user_id"] == USER_ID


async def test_second_message_updates_the_same_rows_instead_of_duplicating(
    message_env: Any,
) -> None:
    handler = _text_handler(message_env.handlers)
    context = SimpleNamespace(args=[], bot=None, bot_data={})

    await handler.callback(_Update("one", username="before"), context)
    await handler.callback(_Update("two", username="after"), context)

    users, chats = await _rows()
    assert [(u.telegram_id, u.username) for u in users] == [(USER_ID, "after")]
    assert len(chats) == 1 and chats[0].chat_id == CHAT_ID
    assert len(message_env.graph.calls) == 2


async def test_a_database_failure_is_not_reported_as_a_successful_message(
    message_env: Any,
) -> None:
    """Negative control for the A-01 contract: when the configured database
    cannot be written, ``on_message`` must not answer as if the message had
    been handled.  The failing factory stands at the same seam as
    ``get_session`` (a context manager whose ``__aenter__`` raises the
    SQLAlchemy error a dead backend produces); the LLM graph must never be
    invoked and no reply may be sent — the exception reaches PTB's error
    handler, which logs it, instead of being swallowed into an "echo"."""
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    settings = settings_module.get_settings()

    @asynccontextmanager
    async def failing_factory(db_path: str | None = None) -> Any:
        raise OperationalError("INSERT INTO user", {}, Exception("database is locked"))
        yield  # pragma: no cover - unreachable, keeps the generator shape

    graph = _Graph()
    handlers = build_handlers(graph, failing_factory, settings, PresenceStore(), object())
    handler = _text_handler(handlers)
    update = _Update("hello", username="first")

    with pytest.raises(OperationalError):
        await handler.callback(update, SimpleNamespace(args=[], bot=None, bot_data={}))

    assert update.message.replies == [], "a failed persist must not look like a handled message"
    assert graph.calls == [], "the LLM graph ran although the user/chat rows were never written"
