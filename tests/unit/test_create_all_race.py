"""Deterministic tests for the concurrent-``create_all`` retry.

``MetaData.create_all(checkfirst=True)`` asks whether a table exists and then
emits ``CREATE TABLE``.  Two processes booting against the same database both
see "does not exist", both emit the DDL, and the loser dies with
``table … already exists`` — reproduced with four concurrent ``nexus migrate``
subprocesses where *all four* failed.

``migration_lock()`` prevents this between processes **on one host** (a local
``fcntl.flock`` file).  It cannot see a second host, so the retry is the
complement.  The integration test can only observe the race statistically;
these tests pin the fix deterministically with a scripted engine that fails on
demand:

* a conflict is retried and the operation ultimately succeeds;
* retries stop at the configured bound instead of looping forever;
* an error that is *not* a create-conflict propagates on the first attempt;
* the matcher recognises both dialects' wording and ignores unrelated errors.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import MetaData
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from nexus_ai_agent.storage.db import (
    _CREATE_ALL_ATTEMPTS,
    _is_concurrent_create_conflict,
    create_all_metadata,
)


def _conflict(message: str) -> OperationalError:
    """Build a SQLAlchemy OperationalError wrapping a DBAPI 'already exists'."""
    return OperationalError("CREATE TABLE x", None, Exception(message))  # type: ignore[arg-type]


class _ScriptedConnection:
    """A connection whose ``run_sync`` follows a script of results/exceptions."""

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls = 0

    async def run_sync(self, fn: Any, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        action = self._script.pop(0) if self._script else "created"
        if isinstance(action, DBAPIError):
            raise action
        return str(action)


class _ScriptedTransaction:
    def __init__(self, connection: _ScriptedConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _ScriptedConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _ScriptedEngine:
    """Stands in for an async engine: ``begin()`` yields the scripted connection."""

    def __init__(self, script: list[Any]) -> None:
        self.connection = _ScriptedConnection(script)

    def begin(self) -> _ScriptedTransaction:
        return _ScriptedTransaction(self.connection)


class TestConflictMatcher:
    @pytest.mark.parametrize(
        "message",
        [
            "table message already exists",  # SQLite
            'relation "chat" already exists',  # PostgreSQL
            "index ix_chat_chat_id already exists",
        ],
    )
    def test_recognises_create_conflicts(self, message: str) -> None:
        assert _is_concurrent_create_conflict(_conflict(message)) is True

    @pytest.mark.parametrize(
        "message",
        ["database is locked", "no such table: chat", "syntax error at or near"],
    )
    def test_ignores_unrelated_errors(self, message: str) -> None:
        assert _is_concurrent_create_conflict(_conflict(message)) is False

    def test_loose_matching_is_a_deliberate_trade_off(self) -> None:
        """The matcher over-matches on purpose.

        Missing a real conflict means two processes cannot bootstrap the same
        database at all; over-matching only means an unrelated error is retried
        a few times before being raised unchanged.  The cheap failure mode wins.
        """
        assert _is_concurrent_create_conflict(_conflict("UNIQUE constraint: nothing here")) is False
        assert _is_concurrent_create_conflict(_conflict("... already exists somewhere")) is True

    def test_reads_the_wrapped_dbapi_exception(self) -> None:
        """SQLAlchemy wraps the driver error; the matcher must look through it."""
        error = ProgrammingError("CREATE TABLE x", None, Exception("relation already exists"))
        assert _is_concurrent_create_conflict(error) is True


class TestRetryBehaviour:
    async def test_recovers_after_a_single_conflict(self) -> None:
        engine = _ScriptedEngine([_conflict("table message already exists"), "created"])
        await create_all_metadata(engine, MetaData())
        assert engine.connection.calls == 2

    async def test_recovers_after_repeated_conflicts(self) -> None:
        script: list[Any] = [_conflict("table message already exists")] * 3 + ["created"]
        engine = _ScriptedEngine(script)
        await create_all_metadata(engine, MetaData())
        assert engine.connection.calls == 4

    async def test_gives_up_at_the_bound(self) -> None:
        """A permanently conflicting database must raise, not spin forever."""
        script: list[Any] = [_conflict("table message already exists")] * _CREATE_ALL_ATTEMPTS
        engine = _ScriptedEngine(script)
        with pytest.raises(OperationalError):
            await create_all_metadata(engine, MetaData())
        assert engine.connection.calls == _CREATE_ALL_ATTEMPTS

    async def test_unrelated_error_propagates_immediately(self) -> None:
        engine = _ScriptedEngine([_conflict("database is locked")])
        with pytest.raises(OperationalError, match="database is locked"):
            await create_all_metadata(engine, MetaData())
        assert engine.connection.calls == 1

    async def test_success_on_the_first_try_does_not_retry(self) -> None:
        engine = _ScriptedEngine(["created"])
        await create_all_metadata(engine, MetaData())
        assert engine.connection.calls == 1


class TestRealSqliteIsUnaffected:
    """The retry wrapper must not change behaviour on the happy path."""

    async def test_creates_the_model_schema(self, tmp_path: Path) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel import SQLModel

        import nexus_ai_agent.storage.db  # noqa: F401  (registers every table)

        path = tmp_path / "happy.sqlite"
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        await create_all_metadata(engine, SQLModel.metadata)
        await engine.dispose()

        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"chat", "message", "user", "referral"} <= tables

    async def test_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel import SQLModel

        import nexus_ai_agent.storage.db  # noqa: F401

        path = tmp_path / "twice.sqlite"
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        await create_all_metadata(engine, SQLModel.metadata)
        await create_all_metadata(engine, SQLModel.metadata)
        await engine.dispose()
        assert path.exists()
