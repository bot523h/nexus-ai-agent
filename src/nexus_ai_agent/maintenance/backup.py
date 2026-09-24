"""Database backup → Cloudflare R2 blob tier (Phase 5), with verified success.

Stateless by design: each run produces one timestamped dump and uploads it
under ``backups/db/<UTC-stamp>/``. Nothing is tracked between runs; the R2
side is pruned later by :mod:`nexus_ai_agent.maintenance.housekeeping`.

Release-level contract (task-167 / P0-C) — a backup run may call itself a
success only when ALL of these hold:

1. the local artifact exists and is non-empty (``size_bytes``),
2. its content is *measured* (``sha256``) — never implied or flagged,
3. integrity is verified before upload (SQLite: ``PRAGMA integrity_check``
   in an isolated temp DB plus a non-trivial table inventory, so the
   silently-empty dumps that ``sqlite3.backup`` produces from corrupt
   sources cannot masquerade as backups; PostgreSQL: pg_dump's own
   completion footer),
4. the remote object *round-trips*: after upload the bytes are downloaded
   again and must be byte-identical to the measured artifact — anything
   else (truncated, wrong key, partial) hard-fails the run,
5. failure ⇒ non-zero exit and is *never* reported as success; success ⇒
   the summary carries a UTC ``timestamp``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
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

# pg_dump's own end-of-file marker for plain-text dumps (stable across
# versions; absent ⇒ truncated dump).
_PG_DUMP_FOOTER = "-- PostgreSQL database dump complete"


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
    """Consistent copy via the sqlite3 online-backup API (safe while app runs).

    NOTE: the API happily "succeeds" against a zero-byte or corrupt source,
    yielding a syntactically valid but *empty* destination — verification in
    :func:`_verify_sqlite_dump` exists precisely to refuse those artifacts.
    """
    if not db_path.exists():
        raise RuntimeError(f"SQLite database not found at {db_path}")
    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_inventory(path: Path, conn: sqlite3.Connection) -> dict[str, int]:
    user_tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts: dict[str, int] = {}
    for table in user_tables:
        counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return counts


def _verify_sqlite_dump(dump_path: Path) -> dict[str, Any]:
    """Prove the dump is a *real* database, not a syntactically valid ghost.

    Restoring into an isolated temp DB is the strongest local proof we have
    that the artifact could be used for recovery; ``integrity_check`` covers
    structural health and the inventory guards against empty dumps.
    """
    verification: dict[str, Any] = {"engine": "sqlite"}
    with tempfile.TemporaryDirectory(prefix="nexus_backup_verify_") as tmp:
        temp_db = Path(tmp) / "restored.sqlite3"
        shutil.copyfile(dump_path, temp_db)
        try:
            with sqlite3.connect(temp_db) as conn:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                integrity = rows[0][0] if rows else ""
                counts = _sqlite_inventory(temp_db, conn)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"backup artifact is not a readable SQLite DB: {exc}") from exc
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity[:200]}")
    if not counts:
        raise RuntimeError(
            "backup artifact contains no user tables — refusing to call an "
            "empty dump a successful backup (source DB may be corrupt)"
        )
    verification["integrity"] = "ok"
    verification["tables"] = counts
    return verification


def _verify_postgres_dump(dump_path: Path) -> dict[str, Any]:
    """Structural verification for a plain-text pg_dump artifact.

    A real ``pg_restore`` verification needs a live target cluster; the
    nightly job has none, so we verify what is locally decidable and record
    it as typed evidence (the strongest honest claim available):
    non-empty + pg_dump completion footer present.
    """
    data = dump_path.read_bytes()
    if not data:
        raise RuntimeError("pg_dump artifact is empty")
    if not data.rstrip().endswith(_PG_DUMP_FOOTER.encode()):
        raise RuntimeError(
            "pg_dump artifact is truncated or failed mid-run "
            f"(missing footer {_PG_DUMP_FOOTER!r})"
        )
    return {"engine": "postgres", "footer": "present"}


def _verify_round_trip(
    provider: R2Provider, *, key: str, local_path: Path, expected_sha256: str
) -> dict[str, Any]:
    """Re-download the uploaded object and prove byte-identical identity.

    Success on an object whose stored bytes differ from the measured local
    artifact is *false success*; this turns it into a hard failure.
    """
    with tempfile.TemporaryDirectory(prefix="nexus_backup_roundtrip_") as tmp:
        downloaded = Path(tmp) / "roundtrip"
        asyncio.run(provider.download(remote_key=key, local_path=downloaded))
        actual = _sha256(downloaded)
    if actual != expected_sha256:
        raise RuntimeError(
            "round-trip verification failed: remote bytes do not match the "
            f"local artifact (local sha256={expected_sha256}, remote sha256={actual})"
        )
    return {"roundtrip": "byte-identical"}


def _build_provider(settings: Settings) -> R2Provider:
    return R2Provider.from_settings(settings)


def create_backup(*, settings: Settings, dry_run: bool = False) -> dict[str, Any]:
    """Dump the active database, verify the artifact, upload, re-verify.

    See the module docstring for the full success contract. In ``dry_run``
    mode nothing is written or uploaded and the summary says so explicitly.
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
        "verified": False,
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
            verification = _verify_postgres_dump(dump_path)
        else:
            _dump_sqlite(Path(settings.db_path), dump_path)
            verification = _verify_sqlite_dump(dump_path)

        size_bytes = dump_path.stat().st_size
        if size_bytes <= 0:
            raise RuntimeError("backup artifact is empty — refusing to upload")
        sha256 = _sha256(dump_path)

        asyncio.run(provider.upload(local_path=dump_path, remote_key=key))
        verification.update(
            _verify_round_trip(
                provider, key=key, local_path=dump_path, expected_sha256=sha256
            )
        )

    summary.update(
        {
            "uploaded": True,
            "verified": True,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "verification": verification,
            "timestamp": now.isoformat(),
        }
    )
    log.info(
        "maintenance_backup_ok",
        key=key,
        bytes=summary["size_bytes"],
        sha256=sha256,
        verified=True,
    )
    return summary
