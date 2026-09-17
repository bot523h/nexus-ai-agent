"""`nexus migrate` must consult the D10 decision matrix for PostgreSQL.

`prepare_postgres` guards the ``get_session`` path, but ``run_migrations`` —
what ``nexus migrate`` actually calls — went straight to
``command.upgrade(..., "head")``. Against a database that already held NEXUS
tables with no ``alembic_version`` that replayed the initial revision and died
with a bare driver error. Reproduced against a real PostgreSQL server:

    before: stamped = False | tables = 29 | decide() = adopt
    RESULT: CRASH -> ProgrammingError
    asyncpg.exceptions.DuplicateTableError: relation "adcampaign" already exists
    mentions 'nexus adopt-pg': False

``decide()`` already knew the answer was ``adopt``; nothing asked it. These
tests pin the guard that now does, and pin that it does **not** recurse:
``prepare_postgres`` calls back into ``run_migrations``, so the sync path has
to re-express the matrix rather than delegate to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.storage import migrations as migrations_module
from nexus_ai_agent.storage.adopt_pg import (
    ACTION_ADOPT,
    ACTION_FAIL,
    ACTION_MANAGED,
    ACTION_MIGRATE,
    PostgresAdoptionReport,
)

PG_URL = "postgresql://nexus:secret@db.example.com:5432/nexus"


def _report(
    stamped: bool = False,
    tables: int = 0,
    missing: list[str] | None = None,
    extra: list[str] | None = None,
) -> PostgresAdoptionReport:
    return PostgresAdoptionReport(
        database="postgresql://db.example.com:5432",
        action="inspect_only",
        alembic_stamped=stamped,
        table_count=tables,
        missing_tables=missing or [],
        extra_tables=extra or [],
    )


@pytest.fixture()
def pg_env(monkeypatch: pytest.MonkeyPatch):
    """Pretend NEXUS_DATABASE_URL is set, and record what the run did."""
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.storage import adopt_pg as adopt_pg_module
    from nexus_ai_agent.storage import db as db_module

    calls: dict[str, int] = {"upgrade": 0, "stamp": 0, "run_migrations": 0}

    monkeypatch.setenv("NEXUS_DATABASE_URL", PG_URL)
    for module in (db_module, migrations_module):
        monkeypatch.setattr(module, "resolve_migration_url", lambda: "postgresql+asyncpg://x/y")
    settings_module.get_settings.cache_clear()

    def fake_upgrade(*args: object, **kwargs: object) -> None:
        calls["upgrade"] += 1

    def fake_stamp(*args: object, **kwargs: object) -> None:
        calls["stamp"] += 1

    monkeypatch.setattr(migrations_module.command, "upgrade", fake_upgrade)
    monkeypatch.setattr(adopt_pg_module, "stamp_head", fake_stamp)
    yield calls
    settings_module.get_settings.cache_clear()


def _stub_inspection(monkeypatch: pytest.MonkeyPatch, report: PostgresAdoptionReport) -> None:
    from nexus_ai_agent.storage import adopt_pg as adopt_pg_module

    monkeypatch.setattr(adopt_pg_module, "inspect_postgres", lambda url: report)


class TestGuardOnMigrate:
    def test_drift_refuses_before_alembic_runs(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        from nexus_ai_agent.storage.adopt_pg import decide

        report = _report(tables=5, missing=["chat", "user"])
        assert decide(report) == ACTION_FAIL, "precondition: this report is drift"
        _stub_inspection(monkeypatch, report)

        with pytest.raises(RuntimeError) as excinfo:
            migrations_module.run_migrations()

        assert "nexus adopt-pg --dry-run" in str(excinfo.value)
        assert pg_env["upgrade"] == 0, "the guard must fire before Alembic writes anything"
        assert pg_env["stamp"] == 0

    def test_zero_drift_legacy_is_adopted_then_upgraded(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        """The case that used to crash: 29 tables, no stamp, no drift."""
        _stub_inspection(monkeypatch, _report(tables=29))

        migrations_module.run_migrations()

        assert pg_env["stamp"] == 1, "an adoptable database is stamped, not replayed"
        assert pg_env["upgrade"] == 1

    def test_managed_database_is_only_upgraded(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        _stub_inspection(monkeypatch, _report(stamped=True, tables=29))

        migrations_module.run_migrations()

        assert pg_env["stamp"] == 0
        assert pg_env["upgrade"] == 1

    def test_empty_database_is_built_by_upgrade(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        _stub_inspection(monkeypatch, _report(tables=0))

        migrations_module.run_migrations()

        assert pg_env["stamp"] == 0
        assert pg_env["upgrade"] == 1

    def test_extra_tables_are_drift_and_refused(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        _stub_inspection(monkeypatch, _report(tables=30, extra=["rogue_table"]))

        with pytest.raises(RuntimeError):
            migrations_module.run_migrations()
        assert pg_env["upgrade"] == 0


class TestNoRecursion:
    def test_the_sync_path_never_calls_back_into_run_migrations(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        """``prepare_postgres`` calls ``run_migrations``; the reverse would loop."""
        _stub_inspection(monkeypatch, _report(tables=29))

        original = migrations_module.run_migrations

        def tripwire(*args: object, **kwargs: object) -> None:
            pg_env["run_migrations"] += 1
            original()

        monkeypatch.setattr(migrations_module, "run_migrations", tripwire)
        migrations_module._prepare_postgres_sync()

        assert pg_env["run_migrations"] == 0, "_prepare_postgres_sync must not recurse"

    def test_prepare_postgres_sync_returns_the_decided_action(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        for report, expected in (
            (_report(stamped=True, tables=29), ACTION_MANAGED),
            (_report(tables=0), ACTION_MIGRATE),
            (_report(tables=29), ACTION_ADOPT),
        ):
            _stub_inspection(monkeypatch, report)
            assert migrations_module._prepare_postgres_sync() == expected

    def test_prepare_postgres_sync_raises_on_drift(
        self, monkeypatch: pytest.MonkeyPatch, pg_env: dict[str, int]
    ) -> None:
        _stub_inspection(monkeypatch, _report(tables=3, missing=["chat"]))
        with pytest.raises(RuntimeError):
            migrations_module._prepare_postgres_sync()


class TestSqlitePathUnaffected:
    def test_sqlite_migration_never_inspects_postgres(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nexus_ai_agent.config import settings as settings_module
        from nexus_ai_agent.storage import adopt_pg as adopt_pg_module
        from nexus_ai_agent.storage import db as db_module

        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
        settings_module.get_settings.cache_clear()
        # Point the migration URL at the tmp file too: a bare relative URL would
        # create a stray database in whatever the cwd happens to be.
        sqlite_url = f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite'}"
        for module in (db_module, migrations_module):
            monkeypatch.setattr(module, "resolve_migration_url", lambda: sqlite_url)

        def explode(url: str) -> PostgresAdoptionReport:
            raise AssertionError("the SQLite path must not inspect PostgreSQL")

        monkeypatch.setattr(adopt_pg_module, "inspect_postgres", explode)

        migrations_module.run_migrations()
        settings_module.get_settings.cache_clear()
