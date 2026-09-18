"""Integration tests for `nexus migrate` adoption (D6).

Proves the black-swan path of the Alembic-first rollout: upgrading a
*pre-Alembic* SQLite file must adopt it (create_all + stamp), never crash on
``CREATE TABLE`` collisions and never lose the existing rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

_HEAD = "47903d282ede"


def _legacy_pendingapproval() -> list[tuple[str, str]]:
    """A single subset table matching the current model, like a v3.x install."""
    return [
        ("id", "INTEGER PRIMARY KEY"),
        ("change_type", "VARCHAR NOT NULL"),
        ("description", "VARCHAR NOT NULL"),
        ("created_at", "DATETIME NOT NULL"),
        ("status", "VARCHAR NOT NULL"),
        ("auto_apply_at", "DATETIME"),
    ]


@pytest.fixture()
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pre-Alembic SQLite file with one table and one row of user data."""
    db_file = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_file)
    cols = ",".join(f"{name} {type_}" for name, type_ in _legacy_pendingapproval())
    conn.execute(f"CREATE TABLE pendingapproval ({cols})")
    conn.execute(
        "INSERT INTO pendingapproval (change_type, description, created_at, status)"
        " VALUES ('legacy', 'do not lose me', '2025-01-01 00:00:00', 'pending')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._initialized_paths.clear()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    return db_file


def test_migrate_adopts_legacy_db_without_data_loss(legacy_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "migrated to head" in result.output

    conn = sqlite3.connect(legacy_db)

    # Data survives adoption.
    row = conn.execute("SELECT change_type, description FROM pendingapproval").fetchone()
    assert row == ("legacy", "do not lose me")

    # Adopted = stamped at head, no partial/duplicate replay.
    stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert stamp == (_HEAD,)

    # create_all brought in the rest of the schema (28 more tables).
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chat", "message", "task", "toolrun", "user", "referral"} <= tables

    conn.close()


def test_migrate_adoption_is_idempotent(legacy_db: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["migrate"])
    second = runner.invoke(app, ["migrate"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    conn = sqlite3.connect(legacy_db)
    assert conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone() == (1,)
    conn.close()


def test_run_bot_bootstraps_legacy_db(legacy_db: Path) -> None:
    """ensure_startup_schema on a legacy file adopts (create_all + stamp)."""
    from nexus_ai_agent.storage.migrations import ensure_startup_schema

    report = ensure_startup_schema()
    assert report == {"backend": "sqlite", "source": "legacy_adopted"}

    conn = sqlite3.connect(legacy_db)
    stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert stamp == (_HEAD,)
    conn.close()
