"""Database backup → Cloudflare R2 blob tier (Phase 5).

Stateless by design: each run produces one timestamped dump and uploads it
under ``backups/db/<UTC-stamp>/``. Nothing is tracked between runs; the R2
side is pruned later by :mod:`nexus_ai_agent.maintenance.housekeeping`.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import resolve_database_url
from nexus_ai_agent.storage.providers.base import ProviderUnavailable
from nexus_ai_agent.storage.providers.r2 import R2Provider

log = get_logger(__name__)

# Blob-tier prefix — recognized by AIStorageManager.is_blob_key.
BLOB_BACKUP_PREFIX = "backups/db"
STAMP_FORMAT = "%Y%m%d-%H%M%S"


def _dump_postgres(database_url: str, dest: Path) -> None:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump not found on PATH (install postgresql-client)")
    proc = subprocess.run(
        [pg_dump, "--no-owner", "--file", str(dest), database_url],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {proc.stderr.strip()[:400]}")


def _dump_sqlite(db_path: Path, dest: Path) -> None:
    """Consistent copy via the sqlite3 online-backup API (safe while app runs)."""
    if not db_path.exists():
        raise RuntimeError(f"SQLite database not found at {db_path}")
    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)


def _build_provider(settings: Settings) -> R2Provider:
    return R2Provider.from_settings(settings)


def create_backup(*, settings: Settings, dry_run: bool = False) -> dict[str, Any]:
    """Dump the active database and upload it to the R2 blob tier.

    PostgreSQL when ``NEXUS_DATABASE_URL`` is set (``pg_dump``), otherwise
    the local SQLite file via the online-backup API. Returns a summary
    dict; in ``dry_run`` mode nothing is written or uploaded.
    """
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime(STAMP_FORMAT)
    database_url = resolve_database_url()

    if database_url:
        filename = f"nexus-pg-{stamp}.sql"
        source = "postgresql (pg_dump)"
    else:
        filename = f"nexus-sqlite-{stamp}.sqlite3"
        source = "sqlite (online backup)"

    key = f"{BLOB_BACKUP_PREFIX}/{stamp}/{filename}"
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "source": source,
        "key": key,
        "uploaded": False,
    }

    if dry_run:
        log.info("maintenance_backup_dry_run", source=source, key=key)
        summary["detail"] = "dry-run: nothing dumped or uploaded"
        return summary

    provider = _build_provider(settings)
    if not provider.is_configured():
        # Fail loudly: a silently skipped backup is worse than a red job.
        raise ProviderUnavailable(
            "R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET) — nothing was uploaded"
        )

    with tempfile.TemporaryDirectory(prefix="nexus_backup_") as tmp:
        dump_path = Path(tmp) / filename
        if database_url:
            _dump_postgres(database_url, dump_path)
        else:
            _dump_sqlite(Path(settings.db_path), dump_path)
        summary["size_bytes"] = dump_path.stat().st_size

        import asyncio

        asyncio.run(provider.upload(local_path=dump_path, remote_key=key))

    summary["uploaded"] = True
    log.info("maintenance_backup_ok", key=key, bytes=summary["size_bytes"])
    return summary
