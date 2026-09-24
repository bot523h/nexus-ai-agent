"""Behavioural tests for the P0-7 LLM-egress consent gate (AIMemory).

The 2026-09-21 audit found that the main message handler sent **every**
user's raw message text to the external Gemini model for "personal
information extraction" — no consent, no opt-out, one fresh engine (and
provider) per message, no rate limit.

These tests lock the corrected contract:

* **default-deny** — no egress while the per-user vote is unset or denied;
* **global kill switch** — ``NEXUS_AI_MEMORY_ENABLED=false`` disables the
  feature entirely (no egress, no prompt);
* **one-time prompt** — unset users see the consent question exactly once;
  ignoring it re-prompts nobody;
* **explicit grant** — after ``aimem:grant`` the extraction runs and is
  persisted;
* **rate limit** — at most one egress per user per
  ``ai_memory_min_egress_seconds``;
* **forget ⇒ revoke** — ``/forget_me`` deletes the memory row *and* the
  consent record, so egress requires a fresh vote;
* **single shared engine** — the handler factory and the message-handler
  prompt path use the injected instance (no per-call construction).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.bot.memory_handlers import (
    AIMEMORY_CONSENT_KEYBOARD,
    AIMEMORY_CONSENT_PROMPT,
    build_memory_handlers,
    ensure_consent_prompted,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features.ai_memory import (
    CONSENT_DENIED,
    CONSENT_GRANTED,
    CONSENT_UNSET,
    EGRESSED,
    SKIP_DISABLED,
    SKIP_NOT_CONSENTED,
    SKIP_RATE_LIMITED,
    AIMemoryEngine,
)
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage import models as models_module  # noqa: F401
from nexus_ai_agent.storage.models import UserMemory

# ── fakes ──────────────────────────────────────────────────────────────


class FakeGemini:
    """Stands in for GeminiProvider; records every egress prompt."""

    def __init__(self, reply: str = '{"name": "Sara", "interests": ["chess"]}'):
        self.reply = reply
        self.calls: list[str] = []

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls.append(prompt)
        return self.reply


def _reset_db_engine_cache() -> None:
    db_module._engine = None  # type: ignore[misc]
    db_module._engine_path = None  # type: ignore[misc]
    db_module._session_factory = None  # type: ignore[misc]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """``get_session()`` resolves data/app.sqlite relative to CWD."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "app.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    _reset_db_engine_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    yield str(db_file)
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture()
def engine(db: str, gemini: FakeGemini) -> AIMemoryEngine:
    # ``db`` (via tmp_path + chdir) isolates get_session()'s CWD-relative
    # data/app.sqlite; the order guarantees the temp DB exists first.
    return AIMemoryEngine(gemini_provider=gemini, min_egress_seconds=3600)  # type: ignore[arg-type]


def memory_row(user_id: int) -> UserMemory | None:
    # Read via the same file the engine used (CWD-relative data/app.sqlite).
    path = Path("data") / "app.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    try:
        with Session(eng) as session:
            return session.exec(select(UserMemory).where(UserMemory.user_id == user_id)).first()
    finally:
        eng.dispose()


# ── engine-level gate ─────────────────────────────────────────────────


async def test_no_egress_when_consent_unset(engine: AIMemoryEngine, gemini: FakeGemini) -> None:
    assert await engine.get_consent(1) == CONSENT_UNSET
    result = await engine.update_from_message(1, "سلام، من سارا هستم")
    assert result == SKIP_NOT_CONSENTED
    assert gemini.calls == []
    assert memory_row(1) is None


async def test_no_egress_when_denied(engine: AIMemoryEngine, gemini: FakeGemini) -> None:
    assert await engine.set_consent(1, granted=False) == CONSENT_DENIED
    result = await engine.update_from_message(1, "سلام")
    assert result == SKIP_NOT_CONSENTED
    assert gemini.calls == []


async def test_egress_after_explicit_grant(engine: AIMemoryEngine, gemini: FakeGemini) -> None:
    await engine.set_consent(1, granted=True)
    result = await engine.update_from_message(1, "سلام، من سارا هستم")
    assert result == EGRESSED
    assert len(gemini.calls) == 1
    ctx = await engine.get_context(1)
    assert "Sara" in ctx
    assert "chess" in ctx


async def test_global_kill_switch_blocks_egress_and_prompt(
    engine: AIMemoryEngine,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    try:
        # Even an explicit grant must not egress while the switch is off.
        await engine.set_consent(1, granted=True)
        result = await engine.update_from_message(1, "سلام")
        assert result == SKIP_DISABLED
        assert gemini.calls == []

        # …and the one-time question is not shown either.
        replies: list[Any] = []
        state = await ensure_consent_prompted(
            engine,
            settings_module.get_settings().ai_memory_enabled,
            2,
            lambda text, **kw: _record(replies, text, kw),
        )
        assert state == CONSENT_UNSET
        assert replies == []
    finally:
        monkeypatch.delenv("NEXUS_AI_MEMORY_ENABLED", raising=False)
        settings_module.get_settings.cache_clear()


async def _record(store: list[Any], text: str, kwargs: dict) -> None:
    store.append((text, kwargs))


async def test_rate_limit_second_call_within_window(
    engine: AIMemoryEngine, gemini: FakeGemini
) -> None:
    await engine.set_consent(1, granted=True)
    assert await engine.update_from_message(1, "first") == EGRESSED
    # 3600s window: the very next message must not egress.
    assert await engine.update_from_message(1, "second") == SKIP_RATE_LIMITED
    assert len(gemini.calls) == 1


async def test_set_consent_persists_state_and_timestamp(engine: AIMemoryEngine) -> None:
    await engine.set_consent(7, granted=True)
    row = memory_row(7)
    assert row is not None
    assert row.ai_memory_consent == CONSENT_GRANTED
    assert row.ai_memory_consent_at is not None
    assert row.ai_memory_prompted is True

    # A revote updates, it does not duplicate the row.
    await engine.set_consent(7, granted=False)
    row = memory_row(7)
    assert row is not None
    assert row.ai_memory_consent == CONSENT_DENIED
    path = Path("data") / "app.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    try:
        with Session(eng) as session:
            count = len(session.exec(select(UserMemory).where(UserMemory.user_id == 7)).all())
    finally:
        eng.dispose()
    assert count == 1


async def test_forget_wipes_memory_and_consent(engine: AIMemoryEngine, gemini: FakeGemini) -> None:
    await engine.set_consent(1, granted=True)
    await engine.update_from_message(1, "سلام، من سارا هستم")
    assert await engine.get_context(1) != ""

    await engine.forget_user(1)
    assert await engine.get_consent(1) == CONSENT_UNSET
    assert await engine.get_context(1) == ""
    assert memory_row(1) is None

    # Post-revocation egress requires a fresh explicit vote.
    assert await engine.update_from_message(1, "سلام") == SKIP_NOT_CONSENTED
    assert len(gemini.calls) == 1  # only the pre-forget extraction


async def test_mark_prompted_is_idempotent(engine: AIMemoryEngine) -> None:
    assert await engine.has_been_prompted(1) is False
    await engine.mark_prompted(1)
    await engine.mark_prompted(1)
    assert await engine.has_been_prompted(1) is True
    row = memory_row(1)
    assert row is not None
    assert row.ai_memory_prompted is True
    assert row.ai_memory_consent is None  # prompting is not a vote


# ── one-time prompt orchestration ─────────────────────────────────────


async def test_prompt_shown_exactly_once(engine: AIMemoryEngine) -> None:
    replies: list[Any] = []
    state = await ensure_consent_prompted(
        engine, True, 1, lambda text, **kw: _record(replies, text, kw)
    )
    assert state == CONSENT_UNSET
    assert len(replies) == 1
    text, kwargs = replies[0]
    assert "رضایت" in text
    buttons = [b.callback_data for b in kwargs["reply_markup"].inline_keyboard[0]]
    assert buttons == ["aimem:grant", "aimem:deny"]

    # Second message from the same (ignoring) user: no re-prompt.
    replies.clear()
    state = await ensure_consent_prompted(
        engine, True, 1, lambda text, **kw: _record(replies, text, kw)
    )
    assert state == CONSENT_UNSET
    assert replies == []


async def test_prompt_skipped_for_voted_users(engine: AIMemoryEngine) -> None:
    await engine.set_consent(1, granted=False)
    replies: list[Any] = []
    state = await ensure_consent_prompted(
        engine, True, 1, lambda text, **kw: _record(replies, text, kw)
    )
    assert state == CONSENT_DENIED
    assert replies == []


# ── callback + command handlers (shared-instance factory) ─────────────


def _make_callback_update(user_id: int, data: str) -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=10),
        message=None,
        edited_message=None,
        callback_query=query,
    )


async def test_consent_callback_grant_persists_vote(engine: AIMemoryEngine) -> None:
    _, _, consent_callback = build_memory_handlers(engine)
    update = _make_callback_update(5, "aimem:grant")
    await consent_callback(update, None)  # type: ignore[arg-type]
    assert await engine.get_consent(5) == CONSENT_GRANTED
    assert update.callback_query.answer.await_count == 1
    edited = update.callback_query.edit_message_text.await_args.args[0]
    assert "✅" in edited


async def test_consent_callback_deny_persists_vote(engine: AIMemoryEngine) -> None:
    _, _, consent_callback = build_memory_handlers(engine)
    update = _make_callback_update(5, "aimem:deny")
    await consent_callback(update, None)  # type: ignore[arg-type]
    assert await engine.get_consent(5) == CONSENT_DENIED
    edited = update.callback_query.edit_message_text.await_args.args[0]
    assert "🛑" in edited


async def test_consent_callback_survives_uneditable_message(engine: AIMemoryEngine) -> None:
    """A stale prompt message may be uneditable; the vote must still persist."""
    _, _, consent_callback = build_memory_handlers(engine)
    update = _make_callback_update(6, "aimem:grant")
    update.callback_query.edit_message_text.side_effect = RuntimeError("message not modified")
    await consent_callback(update, None)  # type: ignore[arg-type]
    assert await engine.get_consent(6) == CONSENT_GRANTED


def test_memory_handlers_bind_shared_engine(engine: AIMemoryEngine) -> None:
    memory_cmd, forget_me_cmd, _ = build_memory_handlers(engine)
    assert memory_cmd is not None
    assert forget_me_cmd is not None
    # The keyboard and prompt are stable, importable constants (testable).
    assert AIMEMORY_CONSENT_PROMPT
    assert len(AIMEMORY_CONSENT_KEYBOARD.inline_keyboard) == 1


async def test_forget_me_command_wipes_consent(engine: AIMemoryEngine) -> None:
    await engine.set_consent(1, granted=True)
    _, forget_me_cmd, _ = build_memory_handlers(engine)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=10),
        message=SimpleNamespace(reply_text=AsyncMock()),
        edited_message=None,
        callback_query=None,
    )
    await forget_me_cmd(update, None)  # type: ignore[arg-type]
    assert await engine.get_consent(1) == CONSENT_UNSET
    update.message.reply_text.assert_awaited_once()


# ── migration contract ────────────────────────────────────────────────


def test_consent_columns_present_in_schema() -> None:
    """The ORM table carries the P0-7 columns (drift-free vs 7c2f9d41e8a3)."""
    table = UserMemory.__table__
    for col in ("ai_memory_consent", "ai_memory_consent_at", "ai_memory_prompted"):
        assert col in table.columns, f"missing column {col!r}"
    assert table.c.ai_memory_consent.nullable
    assert table.c.ai_memory_consent_at.nullable
    assert table.c.ai_memory_prompted.nullable


def test_migration_chain_includes_consent_revision() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["a41c9e2b7f63"]
    revisions = [r.revision for r in script.walk_revisions()]
    assert "7c2f9d41e8a3" in revisions and "f4a9c2e71b08" in revisions
    assert "a41c9e2b7f63" in revisions
