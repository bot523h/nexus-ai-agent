"""Unit tests for NEXUS_DATABASE_URL parsing and backend selection (C1).

Everything here runs without a Postgres server: engine creation is either
monkeypatched or built as a plain object (create_async_engine does not open
a connection).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import (
    decide_sqlite_bootstrap,
    get_session,
    normalize_database_url,
    resolve_database_url,
    to_asyncpg_url,
)
from nexus_ai_agent.storage.models import PendingApproval

VALID_URL = "postgresql://nexus:secret@db.example.com:5432/nexusdb"


@pytest.fixture(autouse=True)
async def _clean_db_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate these tests from inherited env values and module caches."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()
    while db_module._replaced_engines:
        await db_module._replaced_engines.pop().dispose()
    for engine in list(db_module._pg_engines.values()):
        if hasattr(engine, "dispose"):
            await engine.dispose()
    db_module._pg_engines.clear()
    db_module._pg_session_factories.clear()
    db_module._pg_prepared_urls.clear()


class _FakeSessionCM:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.call_count = 0

    def __call__(self) -> _FakeSessionCM:
        self.call_count += 1
        return _FakeSessionCM(self.session)


class TestNormalizeDatabaseUrl:
    @pytest.mark.parametrize(
        "raw",
        [
            "postgresql://nexus:secret@db.example.com:5432/nexusdb",
            "postgres://nexus:secret@db.example.com:5432/nexusdb",
            "postgresql+asyncpg://nexus:secret@db.example.com:5432/nexusdb",
        ],
    )
    def test_accepted_schemes_normalize_to_postgresql(self, raw: str) -> None:
        assert normalize_database_url(raw) == VALID_URL

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_database_url(f"  {VALID_URL}\n") == VALID_URL

    def test_preserves_credentials_port_and_query(self) -> None:
        raw = "postgres://u:p@host:6543/neondb?sslmode=require"
        assert normalize_database_url(raw) == "postgresql://u:p@host:6543/neondb?sslmode=require"

    @pytest.mark.parametrize(
        "bad",
        [
            "sqlite:///data/app.sqlite",
            "sqlite+aiosqlite:////tmp/app.sqlite",
            "mysql://user@host/db",
            "http://example.com/db",
            "not-a-url",
            "",
            "   ",
        ],
    )
    def test_rejects_other_schemes_with_clear_error(self, bad: str) -> None:
        with pytest.raises(ValueError, match="scheme|empty"):
            normalize_database_url(bad)

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(ValueError, match="missing host"):
            normalize_database_url("postgresql:///nexusdb")


class TestToAsyncpgUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
            ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
            ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ],
    )
    def test_converts_to_asyncpg_scheme(self, raw: str, expected: str) -> None:
        assert to_asyncpg_url(raw) == expected

    def test_rejects_sqlite(self) -> None:
        with pytest.raises(ValueError, match="scheme"):
            to_asyncpg_url("sqlite:///data/app.sqlite")


class TestResolveDatabaseUrl:
    def test_unset_returns_none(self) -> None:
        assert resolve_database_url() is None

    def test_blank_value_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "")
        monkeypatch.setenv("DATABASE_URL", "   ")
        assert resolve_database_url() is None

    def test_reads_nexus_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", VALID_URL)
        assert resolve_database_url() == VALID_URL

    def test_reads_legacy_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
        assert resolve_database_url() == "postgresql://u:p@h:5432/db"

    def test_nexus_alias_wins_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", VALID_URL)
        monkeypatch.setenv("DATABASE_URL", "postgres://other:pw@h:5432/db")
        assert resolve_database_url() == VALID_URL

    def test_normalizes_configured_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgres://u:p@h:5432/db")
        assert resolve_database_url() == "postgresql://u:p@h:5432/db"

    def test_rejects_invalid_configured_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "sqlite:///data/app.sqlite")
        with pytest.raises(ValueError, match="scheme"):
            resolve_database_url()


class TestPgEngineSelection:
    def test_engine_created_with_asyncpg_and_pre_ping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        sentinel = object()

        def fake_create_async_engine(url: str, **kwargs: Any) -> Any:
            calls.append({"url": url, **kwargs})
            return sentinel

        monkeypatch.setattr(db_module, "create_async_engine", fake_create_async_engine)

        engine = db_module._get_pg_engine("postgres://u:p@h:5432/db")

        assert engine is sentinel
        assert calls == [
            {"url": "postgresql+asyncpg://u:p@h:5432/db", "echo": False, "pool_pre_ping": True}
        ]

    def test_engine_is_cached_per_normalized_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = object()
        monkeypatch.setattr(
            db_module,
            "create_async_engine",
            lambda url, **kwargs: sentinel,
        )
        first = db_module._get_pg_engine(VALID_URL)
        # Same database, different scheme → same cached engine.
        second = db_module._get_pg_engine("postgres://nexus:secret@db.example.com:5432/nexusdb")
        assert first is second

    @pytest.mark.asyncio
    async def test_real_engine_builds_without_network(self) -> None:
        # create_async_engine is a pure object build: no connection is opened,
        # so this proves the asyncpg dialect wiring without any server.
        engine = db_module._get_pg_engine("postgresql://u:p@localhost:5432/db")
        assert engine.url.drivername == "postgresql+asyncpg"
        await engine.dispose()


class TestGetSessionBackendSelection:
    @pytest.mark.asyncio
    async def test_no_arg_without_url_uses_default_sqlite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        fake_factory = _FakeSessionFactory(session="sqlite-session")

        async def fake_create_all_tables(db_path: str = "data/app.sqlite") -> None:
            calls.append(db_path)

        monkeypatch.setattr(db_module, "create_all_tables", fake_create_all_tables)
        monkeypatch.setattr(db_module, "_session_factory", fake_factory)

        async with get_session() as session:
            pass

        assert session == "sqlite-session"
        assert calls == ["data/app.sqlite"]
        assert fake_factory.call_count == 1

    @pytest.mark.asyncio
    async def test_no_arg_with_url_uses_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgres://u:p@h:5432/db")
        ensured: list[str] = []
        factory_calls: list[str] = []
        fake_factory = _FakeSessionFactory(session="pg-session")

        async def fake_ensure_pg_schema(url: str) -> None:
            ensured.append(url)

        def fake_pg_factory(url: str) -> _FakeSessionFactory:
            factory_calls.append(url)
            return fake_factory

        monkeypatch.setattr(db_module, "_ensure_pg_schema", fake_ensure_pg_schema)
        monkeypatch.setattr(db_module, "_get_pg_session_factory", fake_pg_factory)

        async with get_session() as session:
            pass

        assert session == "pg-session"
        # Both hooks receive the normalized URL.
        assert ensured == ["postgresql://u:p@h:5432/db"]
        assert factory_calls == ["postgresql://u:p@h:5432/db"]
        assert fake_factory.call_count == 1

    @pytest.mark.asyncio
    async def test_explicit_path_always_uses_sqlite_even_with_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", VALID_URL)
        db_path = tmp_path / "explicit.sqlite"

        async with get_session(str(db_path)) as session:
            session.add(PendingApproval(change_type="test", description="explicit path"))
            await session.commit()

        async with get_session(str(db_path)) as session:
            rows = (await session.execute(select(PendingApproval))).scalars().all()

        # Real SQLite round-trip: the explicit path is untouched by the URL.
        assert len(rows) == 1
        assert db_path.exists()


class TestEnsurePgSchema:
    """D7: Postgres schema is prepared lazily via Alembic, cached per URL."""

    @pytest.mark.asyncio
    async def test_runs_migrations_once_per_normalized_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_run_migrations() -> None:  # simulate a thread call
            calls.append("ran")

        monkeypatch.setattr("nexus_ai_agent.storage.migrations.run_migrations", fake_run_migrations)

        # Drop the thread hop: _ensure_pg_schema awaits asyncio.to_thread; make
        # it call the fake synchronously-in-loop instead.
        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return await fn(*args) if asyncio.iscoroutinefunction(fn) else fn(*args)

        import asyncio

        monkeypatch.setattr(db_module.asyncio, "to_thread", fake_to_thread)

        db_module._pg_prepared_urls.clear()
        await db_module._ensure_pg_schema("postgres://u:p@h:5432/db")
        await db_module._ensure_pg_schema("postgresql://u:p@h:5432/db")  # normalized same
        assert calls == ["ran"]  # one alembic run; second URL is the cache hit

    @pytest.mark.asyncio
    async def test_prepares_distinct_urls_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_run_migrations() -> None:
            calls.append("ran")

        monkeypatch.setattr("nexus_ai_agent.storage.migrations.run_migrations", fake_run_migrations)

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return await fn(*args) if asyncio.iscoroutinefunction(fn) else fn(*args)

        monkeypatch.setattr(db_module.asyncio, "to_thread", fake_to_thread)

        db_module._pg_prepared_urls.clear()
        await db_module._ensure_pg_schema("postgres://u:p@h:5432/db1")
        await db_module._ensure_pg_schema("postgres://u:p@h:5432/db2")
        assert calls == ["ran", "ran"]


class TestDecideSqliteBootstrap:
    """D5 bootstrap decision: Alembic-first, create_all for pre-Alembic files."""

    @pytest.fixture(autouse=True)
    def _reset_state(self) -> None:
        db_module._initialized_paths.clear()
        yield
        db_module._initialized_paths.clear()

    def test_missing_file_is_alembic(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "fresh.sqlite")
        assert decide_sqlite_bootstrap(db_path) == "alembic"

    def test_empty_existing_file_is_alembic(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.sqlite"
        db_path.touch()
        assert decide_sqlite_bootstrap(str(db_path)) == "alembic"

    def test_alembic_stamped_file_is_alembic(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "managed.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES ('47903d282ede')")
        conn.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert decide_sqlite_bootstrap(str(db_path)) == "alembic"

    def test_legacy_tables_without_stamp_are_create_all(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert decide_sqlite_bootstrap(str(db_path)) == "create_all"

    def test_initialized_path_is_nothing(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "done.sqlite")
        db_module._initialized_paths.add(db_path)
        assert decide_sqlite_bootstrap(db_path) == "nothing"
