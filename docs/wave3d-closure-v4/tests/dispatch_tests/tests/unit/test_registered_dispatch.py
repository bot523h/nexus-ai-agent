"""Dispatch-boundary tests for the registered free-text ``on_message`` handler.

Reconstruction for the Wave 3D "dispatch ordering" leg, written against the
**exact PR #198 tree** (head ``25c4965b7bfb751cfa9fd60b09962ee27d8bad80``).

Two properties are pinned, and they are *not* the same property:

* **the dispatch path must run** — the registered handler has to reach the
  graph and persist the user/chat rows. On the unpatched #198 base the async
  session boundary is broken (``bot/handlers.py`` calls ``session.exec`` on a
  plain SQLAlchemy ``AsyncSession``), so *every* free-text turn dies in
  ``_upsert_user`` before the graph is invoked. Those are the two direct
  dispatch tests; they are the reason the dispatch-scoped session repair has to
  be applied, and they are why the repair has to come *after* Patch B (Patch B
  changes the prompt the graph builds, not the boundary that lets dispatch reach
  the graph at all).
* **hardening** — the four remaining tests pin behaviour that must hold before
  *and* after the repair: a sender without identity, a sender outside the allow
  list, an unusable text payload, and the registration order of the text handler
  relative to the command handlers (a text handler registered ahead of the
  commands would swallow them; this is the one genuinely *ordering* property in
  the file).

Everything is synthetic; no real user data is involved.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel as _SQLModel

from nexus_ai_agent.bot.feature_handlers import build_feature_engines
from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ai_memory import CONSENT_GRANTED
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.presence import PresenceStore

ALLOWED_USER = 111
DENIED_USER = 999


class _FakeMemoryEngine:
    """Exposes exactly what the dispatch path touches."""

    async def get_consent(self, user_id: int) -> str:
        return CONSENT_GRANTED

    async def has_been_prompted(self, user_id: int) -> bool:
        return True

    async def update_from_message(self, user_id: int, text: str) -> None:
        return None

    async def get_context(self, user_id: int) -> str:
        return ""


class _RecordingGraph:
    def __init__(self) -> None:
        self.states: list[NexusState] = []

    async def ainvoke(self, state: NexusState, config: dict | None = None) -> NexusState:
        self.states.append(dict(state))  # type: ignore[arg-type]
        return {**state, "response": "answer"}


class _Env:
    def __init__(self, handlers: list[Any], graph: _RecordingGraph, replies: list[str], db: Path):
        self.handlers = handlers
        self.graph = graph
        self.replies = replies
        self.db = db

    def on_message(self) -> Any:
        from telegram.ext import MessageHandler

        for handler in self.handlers:
            if isinstance(handler, MessageHandler) and handler.callback.__name__ == "on_message":
                return handler.callback
        raise AssertionError("on_message is not registered in build_handlers()")


async def _prepare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """Build the real handler list on an isolated, temporary database."""
    from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    settings = Settings(telegram_bot_token="test", allowed_user_ids=[ALLOWED_USER])
    container = build_feature_engines(settings)
    container.ai_memory = _FakeMemoryEngine()  # type: ignore[assignment]

    # The force-join gate reads the ambient (CWD-relative) database path, and the
    # tables it needs are only registered once the feature engines have imported
    # their model modules — hence creating them here rather than before.
    ambient = create_engine(f"sqlite:///{tmp_path / 'data' / 'app.sqlite'}")
    _SQLModel.metadata.create_all(ambient)
    ambient.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'handler.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(_SQLModel.metadata.create_all)

    graph = _RecordingGraph()
    replies: list[str] = []

    def _reply(text: str, **kwargs: Any) -> Any:
        async def _send() -> None:
            replies.append(text)

        return _send()

    handlers = build_handlers(
        graph,
        async_sessionmaker(engine, expire_on_commit=False),
        settings,
        PresenceStore(),
        object(),
        container,
    )
    # ``_reply`` lives inside handlers.py; the tests only need to observe what
    # the sender sees, which the real path writes through ``message.reply_text``.
    return _Env(handlers, graph, replies, tmp_path / "handler.sqlite")


def _update(
    *,
    user_id: int | None = ALLOWED_USER,
    text: str | None = "hello there",
    chat_type: str = "private",
    chat_id: int = ALLOWED_USER,
    replies: list[str] | None = None,
) -> SimpleNamespace:
    sink = replies if replies is not None else []

    async def _reply_text(text: str, **kwargs: Any) -> None:
        sink.append(text)

    user = (
        None
        if user_id is None
        else SimpleNamespace(id=user_id, username="alice", full_name="Alice")
    )
    return SimpleNamespace(
        effective_user=user,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        message=SimpleNamespace(text=text, reply_text=_reply_text),
        callback_query=None,
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(bot=None, args=[])


def _rows(db: Path, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# direct dispatch: the registered handler must actually run
# --------------------------------------------------------------------------- #
async def test_direct_dispatch_reaches_the_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A direct call of the registered handler must invoke the graph."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)
    on_message = env.on_message()

    update = _update()
    await asyncio.wait_for(on_message(update, _context()), timeout=15)

    assert env.graph.states, "the dispatch path never reached the graph"
    state = env.graph.states[-1]
    assert state["user_id"] == ALLOWED_USER
    assert state["chat_id"] == ALLOWED_USER


async def test_direct_dispatch_persists_the_sender_and_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The turn must be attributed: the user and chat rows are written first."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)
    on_message = env.on_message()

    await asyncio.wait_for(on_message(_update(), _context()), timeout=15)

    users = _rows(env.db, "user")
    chats = _rows(env.db, "chat")
    assert [row[1] for row in users] == [ALLOWED_USER], f"user row missing: {users!r}"
    assert [row[1] for row in chats] == [ALLOWED_USER], f"chat row missing: {chats!r}"


# --------------------------------------------------------------------------- #
# hardening: properties that must hold before and after the repair
# --------------------------------------------------------------------------- #
async def test_dispatch_fails_closed_without_an_effective_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)

    await asyncio.wait_for(env.on_message()(_update(user_id=None), _context()), timeout=15)

    assert env.graph.states == [], "an unidentified sender must not reach the graph"
    assert _rows(env.db, "user") == []


async def test_dispatch_refuses_a_sender_outside_the_allow_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)
    replies: list[str] = []

    await asyncio.wait_for(
        env.on_message()(_update(user_id=DENIED_USER, replies=replies), _context()), timeout=15
    )

    assert env.graph.states == [], "a denied sender reached the graph"
    assert replies == ["Access denied."], f"a denied sender got no refusal: {replies!r}"


async def test_dispatch_ignores_an_unusable_text_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)

    for text in (None, ""):
        await asyncio.wait_for(env.on_message()(_update(text=text), _context()), timeout=15)

    assert env.graph.states == [], "an unusable payload reached the graph"


async def test_the_text_handler_is_registered_after_the_command_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Registration order: the catch-all text handler must come last.

    python-telegram-bot evaluates group-0 handlers in registration order, so a
    ``MessageHandler(filters.TEXT & ~filters.COMMAND, on_message)`` registered
    *before* the ``CommandHandler``s would swallow every command. Exactly one
    such handler may exist, and it must sit behind all command handlers.
    """
    from telegram.ext import CommandHandler, MessageHandler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    env = await _prepare_env(tmp_path, monkeypatch)

    text_positions = [
        index
        for index, handler in enumerate(env.handlers)
        if isinstance(handler, MessageHandler) and handler.callback.__name__ == "on_message"
    ]
    command_positions = [
        index for index, handler in enumerate(env.handlers) if isinstance(handler, CommandHandler)
    ]

    assert len(text_positions) == 1, f"expected exactly one text handler, got {text_positions}"
    assert command_positions, "no command handlers were registered"
    assert text_positions[0] > max(command_positions), (
        "the catch-all text handler is registered before command handlers and would "
        f"swallow them (text={text_positions[0]}, last command={max(command_positions)})"
    )
