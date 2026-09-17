"""Unit tests for legacy PostgreSQL adoption (D10).

Two layers are covered:

1. **The decision table** (``PgSchemaReport.state``) is pure — it is exercised
   with synthetic reports and no database at all, so every branch is pinned.
2. **The mechanics** (introspect → create_all → stamp) are exercised against a
   temporary SQLite file used as a *dialect substitute*.  This is not a mock:
   ``adopt`` runs its real introspection, its real ``create_all`` and the real
   Alembic ``stamp`` command; only the dialect differs.  The PostgreSQL
   dialect itself is covered end-to-end by the ``migrate-postgres`` CI job.

The substitute is honest about one limitation: SQLite cannot reproduce a
``DuplicateTable`` crash, which is precisely the failure D10 prevents by
refusing to run Alembic against an un-stamped database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlmodel import SQLModel

from nexus_ai_agent.storage.adopt_pg import (
    ACTION_ADOPT,
    ACTION_NONE,
    ACTION_UPGRADE,
    ALEMBIC_VERSION_TABLE,
    IncompatiblePostgresSchemaError,
    PgBootstrapState,
    PgSchemaReport,
    PostgresNotConfiguredError,
    adopt,
    adopt_postgres,
    assert_postgres_ready,
    expected_tables,
    probe_postgres_state,
    redact_url,
)

#: The chain head, matching migrations/versions/2a1c4b6d8e9f_pgvector.py.
_HEAD = "2a1c4b6d8e9f"

FAKE_PG_URL = "postgresql+asyncpg://nexus:s3cr3t@db.example.com:5432/nexus"


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    """A database file with no tables at all (state: FRESH)."""
    path = tmp_path / "fresh.sqlite"
    create_engine(f"sqlite:///{path}").dispose()
    return path


@pytest.fixture()
def adoptable_db(tmp_path: Path) -> Path:
    """Every model table present, no ``alembic_version``, one row of user data.

    This is exactly what the C1 ``create_all`` stopgap left behind on Neon
    before D7 retired it — the black swan D10 exists for.
    """
    path = tmp_path / "legacy_pg.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO pendingapproval (change_type, description, created_at, status)"
        " VALUES ('legacy', 'do not lose me', '2025-01-01 00:00:00', 'pending')"
    )
    conn.commit()
    conn.close()
    assert ALEMBIC_VERSION_TABLE not in _tables(path)
    return path


@pytest.fixture()
def partial_db(tmp_path: Path) -> Path:
    """A few tables but not the full schema (state: INCOMPATIBLE)."""
    path = tmp_path / "partial.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    subset = [SQLModel.metadata.tables[name] for name in ("chat", "user", "message")]
    SQLModel.metadata.create_all(engine, tables=subset)
    engine.dispose()
    return path


@pytest.fixture()
def managed_db(adoptable_db: Path) -> Path:
    """An already Alembic-stamped database (state: MANAGED)."""
    from alembic import command

    from nexus_ai_agent.storage.migrations import build_alembic_config

    command.stamp(build_alembic_config(_sqlite_url(adoptable_db)), "head")
    return adoptable_db


# ── 1. The decision table (pure, no database) ────────────────────────────


class TestDecisionTable:
    """``PgSchemaReport.state`` must be a total, unambiguous function."""

    def test_alembic_version_present_is_managed_even_if_tables_missing(self) -> None:
        report = PgSchemaReport(
            url=FAKE_PG_URL,
            tables=frozenset({ALEMBIC_VERSION_TABLE}),
            expected=frozenset({"chat", "message"}),
        )
        assert report.state is PgBootstrapState.MANAGED

    def test_no_tables_at_all_is_fresh(self) -> None:
        report = PgSchemaReport(url=FAKE_PG_URL, tables=frozenset(), expected=frozenset({"chat"}))
        assert report.state is PgBootstrapState.FRESH

    def test_alembic_version_alone_does_not_make_a_database_non_fresh(self) -> None:
        """An empty but stamped database is managed, not fresh — stamp wins."""
        report = PgSchemaReport(
            url=FAKE_PG_URL, tables=frozenset({ALEMBIC_VERSION_TABLE}), expected=frozenset()
        )
        assert report.state is PgBootstrapState.MANAGED

    def test_all_expected_tables_without_stamp_is_adoptable(self) -> None:
        report = PgSchemaReport(
            url=FAKE_PG_URL,
            tables=frozenset({"chat", "message", "extra_from_another_app"}),
            expected=frozenset({"chat", "message"}),
        )
        assert report.state is PgBootstrapState.ADOPTABLE

    def test_missing_expected_tables_without_stamp_is_incompatible(self) -> None:
        report = PgSchemaReport(
            url=FAKE_PG_URL, tables=frozenset({"chat"}), expected=frozenset({"chat", "message"})
        )
        assert report.state is PgBootstrapState.INCOMPATIBLE

    def test_missing_and_extra_are_computed_against_user_tables(self) -> None:
        report = PgSchemaReport(
            url=FAKE_PG_URL,
            tables=frozenset({"chat", ALEMBIC_VERSION_TABLE, "unrelated"}),
            expected=frozenset({"chat", "message"}),
        )
        assert report.missing == frozenset({"message"})
        assert report.extra == frozenset({"unrelated"})
        assert ALEMBIC_VERSION_TABLE not in report.user_tables

    def test_expected_tables_matches_the_model_metadata(self) -> None:
        """Adoption and Alembic must agree on what 'complete schema' means."""
        assert expected_tables() == frozenset(SQLModel.metadata.tables)
        assert {"chat", "message", "user", "referral"} <= expected_tables()


# ── 2. Credential hygiene ────────────────────────────────────────────────


class TestRedactUrl:
    def test_password_is_replaced(self) -> None:
        assert "s3cr3t" not in redact_url(FAKE_PG_URL)
        assert ":***@" in redact_url(FAKE_PG_URL)

    def test_host_and_database_survive(self) -> None:
        redacted = redact_url(FAKE_PG_URL)
        assert "db.example.com" in redacted
        assert redacted.endswith("/nexus")

    def test_url_without_password_is_returned_unchanged(self) -> None:
        assert (
            redact_url("postgresql+asyncpg://localhost:5432/nexus")
            == "postgresql+asyncpg://localhost:5432/nexus"
        )
        assert (
            redact_url("postgresql://nexus@localhost:5432/nexus")
            == "postgresql://nexus@localhost:5432/nexus"
        )

    def test_empty_password_field_is_still_redacted(self) -> None:
        """`user:@host` holds no secret, but leaving `:@` in output reads like one."""
        assert redact_url("postgresql://nexus:@localhost:5432/nexus") == (
            "postgresql://nexus:***@localhost:5432/nexus"
        )


# ── 3. Introspection ─────────────────────────────────────────────────────


class TestIntrospect:
    async def test_empty_database_is_classified_fresh(self, empty_db: Path) -> None:
        from nexus_ai_agent.storage.adopt_pg import introspect

        report = await introspect(_sqlite_url(empty_db))
        assert report.state is PgBootstrapState.FRESH
        assert report.tables == frozenset()

    async def test_create_all_database_is_classified_adoptable(self, adoptable_db: Path) -> None:
        from nexus_ai_agent.storage.adopt_pg import introspect

        report = await introspect(_sqlite_url(adoptable_db))
        assert report.state is PgBootstrapState.ADOPTABLE
        assert report.missing == frozenset()
        assert not report.has_alembic_version

    async def test_unreachable_database_raises_not_silently(self, tmp_path: Path) -> None:
        from nexus_ai_agent.storage.adopt_pg import introspect

        with pytest.raises(OperationalError):
            await introspect(f"sqlite+aiosqlite:///{tmp_path / 'nope' / 'missing.sqlite'}")


# ── 4. Dry run changes nothing ───────────────────────────────────────────


class TestDryRun:
    async def test_dry_run_reports_adopt_without_touching_the_database(
        self, adoptable_db: Path
    ) -> None:
        before = _tables(adoptable_db)
        result = await adopt(_sqlite_url(adoptable_db), dry_run=True)

        assert result.action == ACTION_ADOPT
        assert result.state is PgBootstrapState.ADOPTABLE
        assert result.dry_run is True
        assert result.changed is False
        assert result.stamped_revision is None
        assert _tables(adoptable_db) == before
        assert ALEMBIC_VERSION_TABLE not in _tables(adoptable_db)

    async def test_dry_run_on_fresh_database_reports_upgrade(self, empty_db: Path) -> None:
        result = await adopt(_sqlite_url(empty_db), dry_run=True)
        assert result.action == ACTION_UPGRADE
        assert result.changed is False
        assert _tables(empty_db) == set()

    async def test_dry_run_on_managed_database_is_a_no_op(self, managed_db: Path) -> None:
        result = await adopt(_sqlite_url(managed_db), dry_run=True)
        assert result.action == ACTION_NONE
        assert result.changed is False

    async def test_dry_run_still_refuses_an_incompatible_database(self, partial_db: Path) -> None:
        """A dry run must tell the truth: this database can never be adopted."""
        with pytest.raises(IncompatiblePostgresSchemaError, match="Refusing to adopt"):
            await adopt(_sqlite_url(partial_db), dry_run=True)


# ── 5. Real adoption ─────────────────────────────────────────────────────


class TestAdopt:
    async def test_adopt_preserves_data_and_stamps_at_head(self, adoptable_db: Path) -> None:
        result = await adopt(_sqlite_url(adoptable_db))

        assert result.action == ACTION_ADOPT
        assert result.changed is True
        assert result.stamped_revision == "head"

        conn = sqlite3.connect(adoptable_db)
        row = conn.execute("SELECT change_type, description FROM pendingapproval").fetchone()
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        conn.close()

        assert row == ("legacy", "do not lose me"), "adoption must not lose user data"
        assert stamp == (_HEAD,), "adoption must stamp the chain head, not replay the revision"

    async def test_adopt_is_idempotent(self, adoptable_db: Path) -> None:
        first = await adopt(_sqlite_url(adoptable_db))
        second = await adopt(_sqlite_url(adoptable_db))

        assert first.action == ACTION_ADOPT
        assert second.action == ACTION_NONE
        assert second.changed is False

        conn = sqlite3.connect(adoptable_db)
        assert conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone() == (1,)
        conn.close()

    async def test_adopt_on_fresh_database_upgrades_to_head(self, empty_db: Path) -> None:
        result = await adopt(_sqlite_url(empty_db))

        assert result.action == ACTION_UPGRADE
        assert result.stamped_revision == "head"
        assert {"chat", "message", "user"} <= _tables(empty_db)

    async def test_adopt_refuses_incompatible_schema(self, partial_db: Path) -> None:
        with pytest.raises(IncompatiblePostgresSchemaError, match="missing"):
            await adopt(_sqlite_url(partial_db))
        assert ALEMBIC_VERSION_TABLE not in _tables(partial_db), "refusal must not stamp"

    async def test_incompatible_message_names_the_missing_tables(self, partial_db: Path) -> None:
        from nexus_ai_agent.storage.adopt_pg import incompatible_message, introspect

        report = await introspect(_sqlite_url(partial_db))
        message = incompatible_message(report)

        # The count is exact, and the list is shown truncated rather than
        # dumping ~27 names into an error message.
        assert f"{len(report.missing)} table(s)" in message
        assert sorted(report.missing)[0] in message
        assert f"(+{len(report.missing) - 10} more)" in message
        assert "nexus adopt-pg --dry-run" in message


# ── 6. The fail-fast guard ───────────────────────────────────────────────


class TestAssertPostgresReady:
    def test_raises_on_unstamped_database(self, adoptable_db: Path) -> None:
        from nexus_ai_agent.storage.adopt_pg import UnstampedPostgresError

        with pytest.raises(UnstampedPostgresError) as excinfo:
            assert_postgres_ready(_sqlite_url(adoptable_db))

        message = str(excinfo.value)
        assert "un-stamped" in message
        assert "nexus adopt-pg --dry-run" in message
        assert "nexus adopt-pg --yes" in message
        assert "No data was modified" in message

    def test_message_never_leaks_credentials(self) -> None:
        """The report carries the real URL; the message must not."""
        from nexus_ai_agent.storage.adopt_pg import incompatible_message, unstamped_message

        report = PgSchemaReport(
            url=FAKE_PG_URL, tables=frozenset({"chat", "message"}), expected=frozenset({"chat"})
        )
        assert "s3cr3t" not in unstamped_message(report)
        assert "s3cr3t" not in incompatible_message(report)

    def test_raises_on_incompatible_database(self, partial_db: Path) -> None:
        with pytest.raises(IncompatiblePostgresSchemaError):
            assert_postgres_ready(_sqlite_url(partial_db))

    def test_passes_a_managed_database_through(self, managed_db: Path) -> None:
        report = assert_postgres_ready(_sqlite_url(managed_db))
        assert report.state is PgBootstrapState.MANAGED

    def test_passes_a_fresh_database_through(self, empty_db: Path) -> None:
        report = assert_postgres_ready(_sqlite_url(empty_db))
        assert report.state is PgBootstrapState.FRESH


# ── 7. Startup probe must never block boot ───────────────────────────────


class TestProbe:
    def test_unreachable_host_returns_none_instead_of_raising(self, tmp_path: Path) -> None:
        """Neon may be scaled to zero at bot start; boot must not die."""
        assert probe_postgres_state(f"sqlite+aiosqlite:///{tmp_path / 'x' / 'y.sqlite'}") is None

    def test_reachable_database_returns_a_report(self, adoptable_db: Path) -> None:
        report = probe_postgres_state(_sqlite_url(adoptable_db))
        assert report is not None
        assert report.state is PgBootstrapState.ADOPTABLE


# ── 8. Configuration guard ───────────────────────────────────────────────


class TestConfigurationGuard:
    def test_adopt_postgres_without_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus_ai_agent.config import settings as settings_module

        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings_module.get_settings.cache_clear()

        with pytest.raises(PostgresNotConfiguredError, match="NEXUS_DATABASE_URL is not set"):
            adopt_postgres(dry_run=True)

        settings_module.get_settings.cache_clear()

    def test_adopt_postgres_uses_the_configured_url(
        self, monkeypatch: pytest.MonkeyPatch, adoptable_db: Path
    ) -> None:
        """The sync CLI entry point resolves NEXUS_DATABASE_URL and adopts it."""
        from nexus_ai_agent.config import settings as settings_module
        from nexus_ai_agent.storage import db as db_module

        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@h:5432/db")
        monkeypatch.setattr(db_module, "resolve_migration_url", lambda: _sqlite_url(adoptable_db))
        settings_module.get_settings.cache_clear()

        result = adopt_postgres(dry_run=True)
        assert result.action == ACTION_ADOPT

        settings_module.get_settings.cache_clear()
