"""Real SQLite path contract across runtime, schema, migration URL and backup.

Only synthetic databases under tmp_path are used. The online backup is real;
no cloud provider or PostgreSQL service is claimed by these tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.maintenance.backup import _dump_sqlite
from nexus_ai_agent.storage.db import create_all_tables, get_session, resolve_migration_url


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for name in ("NEXUS_DB_PATH", "DB_PATH", "NEXUS_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    # Settings initialization must not create paths outside this test sandbox.
    for name, relative in (
        ("NEXUS_CHECKPOINT_PATH", "checkpoint.sqlite"),
        ("NEXUS_VECTOR_PATH", "vector.sqlite"),
        ("NEXUS_MODEL_PATH", "model.gguf"),
        ("NEXUS_CACHE_DIR", "cache"),
        ("CREATIVE_TEMP_DIR", "creative"),
    ):
        monkeypatch.setenv(name, str(tmp_path / relative))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["absolute", "relative", "legacy_alias", "primary_wins", "unset"])
async def test_runtime_migration_and_online_backup_share_database(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if mode == "unset":
        expected = tmp_path / "data/app.sqlite"
    else:
        expected = tmp_path / "configured/runtime.sqlite"
        name = "DB_PATH" if mode == "legacy_alias" else "NEXUS_DB_PATH"
        value = "configured/runtime.sqlite" if mode == "relative" else str(expected)
        monkeypatch.setenv(name, value)
        if mode == "primary_wins":
            monkeypatch.setenv("DB_PATH", str(tmp_path / "wrong-alias.sqlite"))

    async with get_session() as session:
        assert session.bind is not None
        actual = Path(str(session.bind.url.database)).resolve()
        assert actual == expected
        await session.execute(text("CREATE TABLE owner_path_probe (value TEXT NOT NULL)"))
        await session.execute(
            text("INSERT INTO owner_path_probe VALUES (:value)"), {"value": "runtime-write"}
        )
        await session.commit()

    assert expected.is_file()
    assert Path(str(make_url(resolve_migration_url()).database)).resolve() == expected
    # This is the exact source argument used by create_backup's SQLite branch.
    backup = tmp_path / "online-backup.sqlite"
    _dump_sqlite(Path(get_settings().db_path), backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT value FROM owner_path_probe").fetchall() == [
            ("runtime-write",)
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    if mode != "unset":
        assert not (tmp_path / "data/app.sqlite").exists()
    assert not (tmp_path / "wrong-alias.sqlite").exists()


async def test_no_argument_schema_creation_uses_configured_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = tmp_path / "configured/schema.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(expected))
    await create_all_tables()
    assert expected.is_file()
    with sqlite3.connect(expected) as connection:
        assert (
            connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
            > 0
        )
    assert not (tmp_path / "data/app.sqlite").exists()


@pytest.mark.parametrize("postgres_configured", [False, True])
async def test_explicit_sqlite_path_overrides_default_and_postgres(
    postgres_configured: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured = tmp_path / "unused-default.sqlite"
    explicit = tmp_path / "explicit.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(configured))
    if postgres_configured:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://unused:unused@invalid/db")
    await create_all_tables(str(explicit))
    async with get_session(str(explicit)) as session:
        assert session.bind is not None
        assert Path(str(session.bind.url.database)).resolve() == explicit
        assert (await session.execute(text("SELECT 17"))).scalar_one() == 17
    assert explicit.is_file()
    assert not configured.exists()
    assert not (tmp_path / "data/app.sqlite").exists()
