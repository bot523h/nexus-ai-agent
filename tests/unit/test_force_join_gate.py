"""Force-join gate security semantics (S2).

Proves, against real SQLModel/SQLAlchemy behaviour (not mocks):

1. the enabled-anywhere predicate is a *SQL* predicate — it compiles to
   ``enabled IS true`` (SQLite: ``IS 1``) and returns rows on both
   SQLite and (by compilation shape) Postgres. The historical bug —
   ``ForceJoinConfig.enabled is True``, a Python identity comparison —
   compiled to ``WHERE 0 = 1`` and made the gate fail-open for
   everyone;
2. an unbound manager (``bot is None``) fails **closed**: the startup
   wiring binds inside a broad ``try/except`` that can swallow a bind
   failure, so no machine guarantee exists that traffic only flows
   after bind — an unbound gate must therefore treat everyone as
   non-member while force-join is enabled;
3. public commands (/start, /help, /forcejoin_status) bypass the gate;
4. membership checks use the real Telegram API when bound, and a
   failing API call fails closed too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlmodel import Session, SQLModel, create_engine, select

import nexus_ai_agent.features.force_join as force_join_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.storage.models import ForceJoinConfig


class _FakeBot:
    """Answers get_chat_member from a scripted table."""

    def __init__(self, statuses: dict[int, str] | None = None, fail: bool = False) -> None:
        self._statuses = statuses or {}
        self._fail = fail
        self.calls: list[int] = []

    async def get_chat_member(self, chat_id: Any, user_id: int) -> Any:
        self.calls.append(user_id)
        if self._fail:
            raise RuntimeError("telegram unavailable")
        return SimpleNamespace(status=self._statuses.get(user_id, "left"))


@pytest.fixture()
def gate_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the force-join engine at a fresh SQLite file with the table."""
    db_path = str(tmp_path / "forcejoin-test.db")
    settings = Settings().model_copy(update={"db_path": db_path})
    monkeypatch.setattr(force_join_module, "get_settings", lambda: settings)
    engine = force_join_module._sync_engine(db_path)
    SQLModel.metadata.create_all(engine)
    return db_path


def _add_enabled_config(db_path: str, enabled: bool = True) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        session.add(ForceJoinConfig(chat_id=-1001234, enabled=enabled))
        session.commit()


# --------------------------------------------------------------------- #
# 1. The predicate is SQL, not a Python identity comparison
# --------------------------------------------------------------------- #


def test_enabled_predicate_compiles_to_sql_is_true() -> None:
    from sqlalchemy import SQLColumnExpression  # noqa: F401

    stmt = select(ForceJoinConfig).where(ForceJoinConfig.enabled.is_(True))
    sqlite_sql = str(stmt.compile(dialect=sqlite.dialect()))
    postgres_sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "WHERE forcejoinconfig.enabled IS 1" in sqlite_sql
    assert "WHERE forcejoinconfig.enabled IS true" in postgres_sql
    # …and it must *not* be the constant-folded ``WHERE 0 = 1`` that the
    # Python ``is True`` version compiled to.
    assert "WHERE 0 = 1" not in sqlite_sql


async def test_enabled_anywhere_returns_true_when_a_row_exists(gate_db: str) -> None:
    _add_enabled_config(gate_db, enabled=True)
    manager = ForceJoinManager()
    assert manager._is_enabled_anywhere_sync() is True


async def test_enabled_anywhere_false_when_no_rows_or_disabled(gate_db: str) -> None:
    manager = ForceJoinManager()
    assert manager._is_enabled_anywhere_sync() is False
    _add_enabled_config(gate_db, enabled=False)
    assert manager._is_enabled_anywhere_sync() is False


async def test_gate_blocks_non_member_when_enabled(gate_db: str) -> None:
    """End-to-end: enabled config + non-member user → blocked."""
    _add_enabled_config(gate_db, enabled=True)
    manager = ForceJoinManager(bot=_FakeBot({999: "member"}))
    # 999 is a member → not blocked; 1001 is not → blocked.
    assert await manager.should_block(999, "") is False
    assert await manager.should_block(1001, "") is True


async def test_gate_disabled_everywhere_blocks_nobody(gate_db: str) -> None:
    manager = ForceJoinManager(bot=None)
    assert await manager.should_block(1001, "") is False


# --------------------------------------------------------------------- #
# 2. Unbound manager fails closed
# --------------------------------------------------------------------- #


async def test_unbound_check_membership_fails_closed(gate_db: str) -> None:
    manager = ForceJoinManager(bot=None)
    assert await manager.check_membership(1) is False


async def test_unbound_manager_blocks_when_gate_enabled(gate_db: str) -> None:
    """bot is None + force-join enabled → every non-public command blocked.

    The startup wiring (bot/app.py _post_init) binds inside a broad
    try/except that swallows failures, so there is no machine guarantee
    that bind happened before traffic — the gate must fail closed.
    """
    _add_enabled_config(gate_db, enabled=True)
    manager = ForceJoinManager(bot=None)
    assert await manager.should_block(1001, "/ai") is True


async def test_unbound_result_is_not_cached(gate_db: str) -> None:
    """An unbound 'not a member' answer must not poison the cache: once
    the bot binds, the very next check must hit the API."""
    manager = ForceJoinManager(bot=None)
    assert await manager.check_membership(7) is False
    manager.bind(_FakeBot({7: "member"}))
    assert await manager.check_membership(7) is True


async def test_api_failure_fails_closed(gate_db: str) -> None:
    manager = ForceJoinManager(bot=_FakeBot(fail=True))
    assert await manager.check_membership(5) is False


# --------------------------------------------------------------------- #
# 3. Public commands bypass the gate
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("command", ["start", "help", "forcejoin_status"])
async def test_public_commands_bypass_gate(gate_db: str, command: str) -> None:
    _add_enabled_config(gate_db, enabled=True)
    manager = ForceJoinManager(bot=None)
    assert await manager.should_block(1001, command) is False


# --------------------------------------------------------------------- #
# 4. Bound manager uses the real API path (+ cache)
# --------------------------------------------------------------------- #


async def test_bound_membership_uses_api_and_caches(gate_db: str) -> None:
    bot = _FakeBot({42: "member"})
    manager = ForceJoinManager(bot=bot)
    assert await manager.check_membership(42) is True
    assert await manager.check_membership(42) is True
    assert bot.calls == [42], "second check must be served from the cache"
    manager.invalidate_cache(42)
    assert await manager.check_membership(42) is True
    assert bot.calls == [42, 42]
