"""Unit tests for the PostgreSQL presence store (task-163, ADR 0005).

The contract under test is the *server-clock* design: TTLs are set and
checked entirely in SQL (``now() + make_interval(...)`` /
``online_until > now()``) — no local clock anywhere.  A fake connection
captures the SQL and answers from a tiny in-memory state; reconnect-once
is tested with an injectable connector.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.stateful import presence_pg as pg_presence


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None) -> None:
        self._rows = rows
        self.description = [("col",)] if rows is not None else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows or []


class _FakeConn:
    def __init__(
        self, online: dict[int, bool] | None = None, closed: list[_FakeConn] | None = None
    ) -> None:
        self.online = dict(online or {})
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._closed = False
        if closed is not None:
            closed.append(self)

    def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        self.executed.append((sql, params))
        if sql.startswith("INSERT INTO"):
            user_id = int(params[0])
            self.online[user_id] = True
            return _Cursor(None)
        if "online_until > now()" in sql and "count(*)" not in sql:
            user_id = int(params[0])
            return _Cursor([(self.online.get(user_id, False),)])
        if "count(*)" in sql:
            return _Cursor([(sum(1 for v in self.online.values() if v),)])
        raise AssertionError(f"unexpected SQL: {sql}")

    def close(self) -> None:
        self._closed = True


def test_mark_online_uses_server_clock_upsert() -> None:
    conn = _FakeConn()
    store = pg_presence.PgPresenceStore("postgresql://x", connection=conn)

    store.mark_online(42, ttl_seconds=120.0)

    sql, params = conn.executed[0]
    assert "now() + make_interval(secs => %s)" in sql  # server clock, not local
    assert "ON CONFLICT (user_id) DO UPDATE" in sql  # upsert, not delete+insert
    assert params == (42, 120.0)


def test_default_ttl_applied_when_none() -> None:
    conn = _FakeConn()
    store = pg_presence.PgPresenceStore("postgresql://x", default_ttl_seconds=77.0, connection=conn)

    store.mark_online(1)

    assert conn.executed[0][1][1] == 77.0


def test_is_online_uses_server_clock_check() -> None:
    conn = _FakeConn(online={7: True})
    store = pg_presence.PgPresenceStore("postgresql://x", connection=conn)

    assert store.is_online(7) is True
    assert store.is_online(8) is False  # absent row → offline

    assert "online_until > now()" in conn.executed[-1][0]  # the database decides


def test_count_online() -> None:
    conn = _FakeConn(online={1: True, 2: True, 3: False})
    store = pg_presence.PgPresenceStore("postgresql://x", connection=conn)
    assert store.count_online() == 2


def test_transient_failure_reconnects_once_and_retries() -> None:
    import psycopg

    dead = _FakeConn()

    class _DiesOnce:
        def __init__(self) -> None:
            self.first = True

        def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
            if self.first:
                self.first = False
                raise psycopg.OperationalError("terminating connection")
            return dead.execute(sql, params)

    made: list[_FakeConn] = []
    connector = lambda url: (made.append(_FakeConn(online={5: True})), made[-1])[1]  # noqa: E731
    store = pg_presence.PgPresenceStore(
        "postgresql://x", connection=_DiesOnce(), connector=connector
    )

    assert store.is_online(5) is True  # first attempt died, retry on the fresh conn
    assert len(made) == 1  # exactly one reconnect


def test_non_transient_failure_raises_without_reconnect() -> None:
    class _BadSQL:
        def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
            raise ValueError("syntax error at or near")

    made: list[_FakeConn] = []
    store = pg_presence.PgPresenceStore(
        "postgresql://x",
        connection=_BadSQL(),
        connector=lambda url: (made.append(_FakeConn()), made[-1])[1],  # noqa: E731
    )
    with pytest.raises(ValueError):
        store.is_online(1)
    assert made == []  # no reconnect for a logic error


def test_close_idempotent_and_quiet() -> None:
    conn = _FakeConn()
    store = pg_presence.PgPresenceStore("postgresql://x", connection=conn)
    store.close()
    store.close()  # second close is a no-op
    assert conn._closed is True


def test_build_presence_store_none_without_database_url() -> None:
    from types import SimpleNamespace

    assert pg_presence.build_presence_store(SimpleNamespace(database_url=None)) is None
    store = pg_presence.build_presence_store(SimpleNamespace(database_url="postgresql://x"))
    assert isinstance(store, pg_presence.PgPresenceStore)
