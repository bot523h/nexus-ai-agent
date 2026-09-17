"""End-to-end SQLite schema journeys (Phase D exit criteria).

Three journeys a real user actually takes, driven through the real Typer CLI:

1. **fresh install** — no file at all → ``nexus migrate`` → full schema plus an
   ``alembic_version`` stamp at the chain head;
2. **legacy upgrade** — a pre-Alembic file with user data → ``nexus migrate``
   → adopted automatically, data intact, stamped at head;
3. **re-run** — ``nexus migrate`` twice → the second run is a clean no-op.

There is deliberately **no** ``nexus adopt`` command for SQLite: D6 made
adoption automatic inside ``run_migrations``/``ensure_startup_schema``, because
a local single-user file carries no risk that justifies an extra manual step.
``nexus adopt-pg`` exists (D10) precisely because a shared hosted PostgreSQL
does.  :func:`test_sqlite_adoption_is_automatic_not_a_command` pins that split.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

#: Chain head — migrations/versions/2a1c4b6d8e9f_pgvector.py.
_HEAD = "2a1c4b6d8e9f"


def _reset_module_state() -> None:
    """Drop cached settings/path guards between tests.

    Deliberately does *not* null ``db_module._engine``: the shared autouse
    fixture ``tests/conftest.py::_dispose_db_engine`` owns engine teardown and
    disposes it inside a live loop.  Dropping the reference here instead lets a
    connection created by ``asyncio.run`` (legacy adoption) be finalised after
    its loop closed, which surfaces as an "Event loop is closed" thread warning
    in later tests.
    """
    settings_module.get_settings.cache_clear()
    db_module._initialized_paths.clear()


@pytest.fixture()
def sqlite_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the whole app at one isolated SQLite file."""
    db_file = tmp_path / "app.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _reset_module_state()
    yield db_file
    _reset_module_state()


def _state(db_file: Path) -> tuple[set[str], list[str]]:
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "alembic_version" not in tables:
            return tables, []
        versions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
        return tables, versions
    finally:
        conn.close()


class TestFreshInstall:
    def test_migrate_creates_full_schema_and_stamps_head(self, sqlite_env: Path) -> None:
        assert not sqlite_env.exists()

        result = CliRunner().invoke(app, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "migrated to head" in result.output

        tables, versions = _state(sqlite_env)
        assert versions == [_HEAD]
        assert set(SQLModel.metadata.tables) <= tables

    def test_fresh_schema_is_immediately_usable(self, sqlite_env: Path) -> None:
        """A migrated database accepts real writes through its real constraints."""
        CliRunner().invoke(app, ["migrate"])

        conn = sqlite3.connect(sqlite_env)
        conn.execute(
            "INSERT INTO chat (chat_id, thread_id, created_at, policy)"
            " VALUES (4242, 'smoke', '2026-09-17 00:00:00', 'default')"
        )
        conn.commit()
        assert conn.execute("SELECT policy FROM chat WHERE chat_id = 4242").fetchone() == (
            "default",
        )
        conn.close()

    def test_startup_schema_reports_alembic_for_a_new_file(self, sqlite_env: Path) -> None:
        from nexus_ai_agent.storage.migrations import ensure_startup_schema

        assert ensure_startup_schema() == {"backend": "sqlite", "source": "alembic"}


class TestLegacyUpgrade:
    @pytest.fixture()
    def legacy_db(self, sqlite_env: Path) -> Path:
        """A v3.x-era file: one table, one row, no ``alembic_version``."""
        conn = sqlite3.connect(sqlite_env)
        conn.execute(
            "CREATE TABLE pendingapproval ("
            "id INTEGER PRIMARY KEY, change_type VARCHAR NOT NULL,"
            "description VARCHAR NOT NULL, created_at DATETIME NOT NULL,"
            "status VARCHAR NOT NULL, auto_apply_at DATETIME)"
        )
        conn.execute(
            "INSERT INTO pendingapproval (change_type, description, created_at, status)"
            " VALUES ('legacy', 'do not lose me', '2025-01-01 00:00:00', 'pending')"
        )
        conn.commit()
        conn.close()
        return sqlite_env

    def test_migrate_adopts_legacy_file_without_data_loss(self, legacy_db: Path) -> None:
        result = CliRunner().invoke(app, ["migrate"])
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(legacy_db)
        row = conn.execute("SELECT change_type, description FROM pendingapproval").fetchone()
        versions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
        conn.close()

        assert row == ("legacy", "do not lose me")
        assert versions == [_HEAD], "adoption stamps head; it never replays the revision"

    def test_legacy_file_gains_the_rest_of_the_schema(self, legacy_db: Path) -> None:
        CliRunner().invoke(app, ["migrate"])
        tables, _ = _state(legacy_db)
        assert {"chat", "message", "task", "toolrun", "user", "referral"} <= tables

    def test_startup_schema_reports_legacy_adopted(self, legacy_db: Path) -> None:
        from nexus_ai_agent.storage.migrations import ensure_startup_schema

        assert ensure_startup_schema() == {"backend": "sqlite", "source": "legacy_adopted"}

    def test_sqlite_adoption_is_automatic_not_a_command(self) -> None:
        """The documented split: SQLite adopts silently, PostgreSQL needs a command."""
        from nexus_ai_agent.storage.adopt_pg import adopt_postgres  # noqa: F401

        commands = set(app.registered_commands)
        names = {
            command.name or (command.callback.__name__ if command.callback else "")
            for command in commands
        }
        assert "migrate" in names
        assert "adopt_pg" in names or "adopt-pg" in names
        assert "adopt" not in names, "SQLite adoption is automatic by design (D6)"


class TestIdempotency:
    def test_migrate_twice_is_a_clean_no_op(self, sqlite_env: Path) -> None:
        runner = CliRunner()
        first = runner.invoke(app, ["migrate"])
        before, _ = _state(sqlite_env)

        second = runner.invoke(app, ["migrate"])
        after, versions = _state(sqlite_env)

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert versions == [_HEAD]
        assert before == after

    def test_legacy_then_migrate_is_also_idempotent(self, sqlite_env: Path) -> None:
        engine = create_engine(f"sqlite:///{sqlite_env}")
        subset = [SQLModel.metadata.tables[name] for name in ("chat", "user")]
        SQLModel.metadata.create_all(engine, tables=subset)
        engine.dispose()
        _reset_module_state()

        runner = CliRunner()
        # A partial file is adopted by create_all (idempotent), then stamped.
        assert runner.invoke(app, ["migrate"]).exit_code == 0
        assert runner.invoke(app, ["migrate"]).exit_code == 0

        _, versions = _state(sqlite_env)
        assert versions == [_HEAD]


class TestAdoptPgCli:
    def test_without_a_database_url_it_explains_and_exits_nonzero(
        self, sqlite_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = CliRunner().invoke(app, ["adopt-pg", "--dry-run"])
        assert result.exit_code == 1
        assert "NEXUS_DATABASE_URL is not set" in result.output

    def test_dry_run_through_the_cli_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI path is exercised end to end against the SQLite stand-in."""
        db_file = tmp_path / "neon.sqlite"
        engine = create_engine(f"sqlite:///{db_file}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()

        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:secret@h:5432/db")
        monkeypatch.setattr(
            db_module, "resolve_migration_url", lambda: f"sqlite+aiosqlite:///{db_file}"
        )
        settings_module.get_settings.cache_clear()

        result = CliRunner().invoke(app, ["adopt-pg", "--dry-run"])
        settings_module.get_settings.cache_clear()

        assert result.exit_code == 0, result.output
        assert "adoptable" in result.output
        assert "Dry run" in result.output
        assert "secret" not in result.output
        tables, _ = _state(db_file)
        assert "alembic_version" not in tables

    def test_yes_flag_applies_without_a_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_file = tmp_path / "neon.sqlite"
        engine = create_engine(f"sqlite:///{db_file}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()
        conn = sqlite3.connect(db_file)
        conn.execute(
            "INSERT INTO pendingapproval (change_type, description, created_at, status)"
            " VALUES ('legacy', 'keep me', '2025-01-01 00:00:00', 'pending')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:secret@h:5432/db")
        monkeypatch.setattr(
            db_module, "resolve_migration_url", lambda: f"sqlite+aiosqlite:///{db_file}"
        )
        settings_module.get_settings.cache_clear()

        result = CliRunner().invoke(app, ["adopt-pg", "--yes"])
        settings_module.get_settings.cache_clear()

        assert result.exit_code == 0, result.output
        assert "Adopted" in result.output
        conn = sqlite3.connect(db_file)
        assert conn.execute("SELECT description FROM pendingapproval").fetchone() == ("keep me",)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (_HEAD,)
        conn.close()

    def test_abort_at_the_prompt_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_file = tmp_path / "neon.sqlite"
        engine = create_engine(f"sqlite:///{db_file}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()

        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:secret@h:5432/db")
        monkeypatch.setattr(
            db_module, "resolve_migration_url", lambda: f"sqlite+aiosqlite:///{db_file}"
        )
        settings_module.get_settings.cache_clear()

        result = CliRunner().invoke(app, ["adopt-pg"], input="n\n")
        settings_module.get_settings.cache_clear()

        assert result.exit_code == 1
        assert "Aborted" in result.output
        tables, _ = _state(db_file)
        assert "alembic_version" not in tables


class TestContinuumCli:
    def test_show_prints_the_committed_snapshot(self) -> None:
        from nexus_ai_agent.continuum import default_snapshot_path

        path = default_snapshot_path()
        if not path.exists():  # pragma: no cover - the file is committed
            pytest.skip("continuum snapshot not present")
        result = CliRunner().invoke(app, ["continuum", "show"])
        assert result.exit_code == 0, result.output
        import json

        assert json.loads(result.output)["schema_version"] == 1

    def test_verify_runs_against_the_committed_snapshot(self) -> None:
        from nexus_ai_agent.continuum import default_snapshot_path

        if not default_snapshot_path().exists():  # pragma: no cover
            pytest.skip("continuum snapshot not present")
        result = CliRunner().invoke(app, ["continuum", "verify"])
        # Either outcome is valid; what matters is that it runs and explains itself.
        assert result.exit_code in (0, 1)
        assert "phase:" in result.output

    def test_show_reports_a_missing_snapshot_clearly(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            app, ["continuum", "show", "--path", str(tmp_path / "absent.json")]
        )
        assert result.exit_code == 1
        assert "no continuum snapshot" in result.output
