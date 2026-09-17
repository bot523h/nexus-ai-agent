"""Integration test for the ``nexus migrate`` command (D5).

Drives the real Typer app (so the exact CLI wiring a user hits is exercised)
against an isolated SQLite file, then proves the produced schema is healthy:

1. start from an empty database file;
2. run ``migrate``;
3. assert the tables exist;
4. insert a real record to confirm the schema is usable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NEXUS_DB_PATH at an isolated file and clear all module state."""
    db_file = tmp_path / "app.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._initialized_paths.clear()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    return db_file


def test_migrate_command_builds_usable_schema(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "migrated to head" in result.output

    # (c) the tables exist — the full SQLModel set minus internal bookkeeping.
    conn = sqlite3.connect(cli_env)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alembic_version", "pendingapproval", "chat", "message", "user"} <= tables

    # (d) insert a real record through the SQLModel model.
    from sqlalchemy import create_engine

    from nexus_ai_agent.storage.models import PendingApproval

    engine = create_engine(f"sqlite:///{cli_env}")
    with Session(engine) as session:
        session.add(PendingApproval(change_type="migrate-test", description="healthy"))
        session.commit()
    engine.dispose()

    ranks = conn.execute("SELECT COUNT(*) FROM pendingapproval").fetchone()[0]
    assert ranks == 1
    conn.close()


def test_migrate_is_idempotent(cli_env: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["migrate"])
    second = runner.invoke(app, ["migrate"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    conn = sqlite3.connect(cli_env)
    alembic_version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert alembic_version == ("2a1c4b6d8e9f",)
    conn.close()


def test_run_bot_bootstraps_new_database(cli_env: Path) -> None:
    """ensure_startup_schema on a new file goes through Alembic (not create_all)."""
    from nexus_ai_agent.storage.migrations import ensure_startup_schema

    report = ensure_startup_schema()
    assert report == {"backend": "sqlite", "source": "alembic"}
    assert cli_env.exists()
