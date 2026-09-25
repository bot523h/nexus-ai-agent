"""Backup → restore proof on a REAL PostgreSQL (task-164).

Runs wherever ``NEXUS_DATABASE_URL`` points at a disposable PostgreSQL with
``pg_dump``/``psql`` on PATH (the maintenance ``restore-drill`` job, or a
local docker Postgres). Skipped otherwise — the reason is visible (-rs).

What is proven, with the exact code path the nightly backup uses:

* ``pg_dump --no-owner --no-privileges`` of a seeded database,
* footer verification,
* ``CREATE DATABASE … TEMPLATE template0`` on the same cluster,
* ``psql -X -v ON_ERROR_STOP=1 --single-transaction -f <dump>``,
* restored inventory == source inventory, **row counts identical**,
* the scratch database is dropped afterwards (idempotent re-runs),
* and — through ``create_backup`` with an in-memory R2 — the full
  produce → persist → round-trip → restore chain reports ``restore_proven``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

DB_URL = os.getenv("NEXUS_DATABASE_URL", "")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="requires PostgreSQL (NEXUS_DATABASE_URL)"),
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("psql") is None,
        reason="requires pg_dump and psql on PATH",
    ),
]

_PROBE_TABLE = "backup_drill_probe"


@pytest.fixture()
def seeded_probe_table() -> Any:
    import psycopg

    with psycopg.connect(DB_URL) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{_PROBE_TABLE}"')
        conn.execute(
            f'CREATE TABLE "{_PROBE_TABLE}" (id serial PRIMARY KEY, note text NOT NULL, '
            "payload bytea, created timestamptz DEFAULT now())"
        )
        for i in range(37):
            conn.execute(
                f'INSERT INTO "{_PROBE_TABLE}" (note, payload) VALUES (%s, %s)',
                (f"row-{i} — unicode ✓ فارسی", bytes([i]) * 64),
            )
        conn.commit()
    yield _PROBE_TABLE
    with psycopg.connect(DB_URL) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{_PROBE_TABLE}"')
        conn.commit()


def _scratch_databases() -> list[str]:
    import psycopg

    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'nexus_restore_%'"
        ).fetchall()
    return [r[0] for r in rows]


def test_restore_drill_round_trips_the_seeded_database(seeded_probe_table: str) -> None:
    from nexus_ai_agent.maintenance.backup import restore_drill

    before = set(_scratch_databases())
    summary = restore_drill(source_url=DB_URL, target_url=DB_URL)

    assert summary["status"] == "success"
    v = summary["verification"]
    assert v["footer"] == "present" and v["restore"] == "ok"
    assert v["row_counts_match"] is True
    assert v["restored_tables"][f"public.{seeded_probe_table}"] == 37
    assert v["restored_tables"] == v["source_tables"]
    # scratch database was dropped — nothing leaks between runs
    assert set(_scratch_databases()) == before


def test_create_backup_reports_restore_proven_end_to_end(
    seeded_probe_table: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nightly command path itself: dump → verify → upload → round-trip → restore."""
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.maintenance.backup import create_backup

    class _MemoryR2:
        name = "r2"

        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def is_configured(self) -> bool:
            return True

        async def upload(self, *, local_path: Path, remote_key: str) -> None:
            self.objects[remote_key] = local_path.read_bytes()

        async def download(self, *, remote_key: str, local_path: Path) -> None:
            local_path.write_bytes(self.objects[remote_key])

    settings_module.get_settings.cache_clear()
    fake = _MemoryR2()
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._build_provider", lambda s: fake)
    settings = Settings(db_path=str(tmp_path / "unused.sqlite3"))

    summary = create_backup(settings=settings, require_postgres=True, restore_target_url=DB_URL)

    assert summary["status"] == "success"
    assert summary["uploaded"] and summary["verified"] and summary["restore_proven"]
    assert summary["key"].startswith("backups/db/")
    assert len(fake.objects) == 1
    stored = next(iter(fake.objects.values()))
    # real trailer: "--\n-- PostgreSQL database dump complete\n--\n\n"
    assert b"-- PostgreSQL database dump complete\n--" in stored[-128:]
    assert summary["verification"]["restored_tables"][f"public.{seeded_probe_table}"] == 37
    assert summary["verification"]["row_counts_match"] is True
