"""Fail-fast behaviour for an un-stamped PostgreSQL database (D10).

Before D10, ``run_migrations`` handed a database full of NEXUS tables straight
to ``alembic upgrade head``, which replayed the initial revision and died with
``DuplicateTable: relation "chat" already exists`` — a message that neither
explains the situation nor suggests a fix.  These tests pin the replacement
behaviour:

* ``run_migrations`` and ``ensure_startup_schema`` refuse *before* Alembic
  writes anything, with a message that names the command to run;
* an unreachable host still defers, so a Neon database that is scaled to zero
  cannot stop the bot from booting (the pre-D10 guarantee is preserved);
* once ``adopt`` has run, the very same calls succeed.

PostgreSQL is substituted by a temporary SQLite file at the URL boundary only;
every line of the guard under test is the production code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage import migrations as migrations_module
from nexus_ai_agent.storage.adopt_pg import UnstampedPostgresError, adopt

PG_URL = "postgresql://nexus:s3cr3t@db.example.com:5432/nexus"


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _has_stamp(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return "alembic_version" in names
    finally:
        conn.close()


def _point_migration_url_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Redirect URL resolution to the SQLite stand-in, in *both* namespaces.

    ``storage.migrations`` binds ``resolve_migration_url`` at import time, while
    ``migrations/env.py`` imports it from ``storage.db`` — patching only one
    leaves Alembic resolving the real (unreachable) PostgreSQL URL.  Pinning
    both is what makes the substitution complete.
    """
    url = _sqlite_url(path)
    monkeypatch.setattr(migrations_module, "resolve_migration_url", lambda: url)
    monkeypatch.setattr(db_module, "resolve_migration_url", lambda: url)


@pytest.fixture()
def unstamped_pg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend NEXUS_DATABASE_URL points at a pre-Alembic PostgreSQL database."""
    from nexus_ai_agent.config import settings as settings_module

    path = tmp_path / "neon_legacy.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    assert not _has_stamp(path)

    monkeypatch.setenv("NEXUS_DATABASE_URL", PG_URL)
    _point_migration_url_at(monkeypatch, path)
    settings_module.get_settings.cache_clear()
    yield path
    settings_module.get_settings.cache_clear()


class TestRunMigrationsFailFast:
    def test_raises_before_alembic_writes_anything(self, unstamped_pg: Path) -> None:
        with pytest.raises(UnstampedPostgresError) as excinfo:
            migrations_module.run_migrations()

        assert not _has_stamp(unstamped_pg), "the guard must fire before Alembic runs"
        assert "nexus adopt-pg" in str(excinfo.value)

    def test_message_does_not_leak_the_database_password(self, unstamped_pg: Path) -> None:
        with pytest.raises(UnstampedPostgresError) as excinfo:
            migrations_module.run_migrations()
        assert "s3cr3t" not in str(excinfo.value)

    def test_succeeds_after_adoption(self, unstamped_pg: Path) -> None:
        """The error message's own advice actually resolves the situation."""
        import asyncio

        with pytest.raises(UnstampedPostgresError):
            migrations_module.run_migrations()

        asyncio.run(adopt(_sqlite_url(unstamped_pg)))
        migrations_module.run_migrations()  # must not raise

        assert _has_stamp(unstamped_pg)

    def test_managed_database_migrates_normally(self, unstamped_pg: Path) -> None:
        import asyncio

        asyncio.run(adopt(_sqlite_url(unstamped_pg)))
        migrations_module.run_migrations()
        migrations_module.run_migrations()  # idempotent


class TestStartupFailFast:
    def test_raises_for_an_unstamped_database(self, unstamped_pg: Path) -> None:
        with pytest.raises(UnstampedPostgresError):
            migrations_module.ensure_startup_schema()

    def test_defers_when_the_host_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A scaled-to-zero Neon must not stop the bot from booting."""
        from nexus_ai_agent.config import settings as settings_module

        monkeypatch.setenv("NEXUS_DATABASE_URL", PG_URL)
        _point_migration_url_at(monkeypatch, tmp_path / "asleep" / "neon.sqlite")
        settings_module.get_settings.cache_clear()

        report = migrations_module.ensure_startup_schema()
        assert report == {"backend": "postgresql", "source": "deferred"}

        settings_module.get_settings.cache_clear()

    def test_healthy_database_still_defers_the_actual_migration(self, unstamped_pg: Path) -> None:
        """D10 adds a probe, not a startup migration — the contract is unchanged."""
        import asyncio

        asyncio.run(adopt(_sqlite_url(unstamped_pg)))
        report = migrations_module.ensure_startup_schema()
        assert report == {"backend": "postgresql", "source": "deferred"}
