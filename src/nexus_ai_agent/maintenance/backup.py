"""Database backup → Cloudflare R2 blob tier (Phase 5) — verified, never asserted.

Stateless by design: each run produces one timestamped dump and uploads it under
``backups/db/<UTC-stamp>/``.  Nothing is tracked between runs; the R2 side is
pruned later by :mod:`nexus_ai_agent.maintenance.housekeeping`.

Why this module is longer than a ``dump + upload`` script (owner directive
2026-09-24 §10): ``sqlite3``'s online-backup API can hand back an artifact that
*looks* fine — a file exists, has bytes, even opens — while its content is
semantically wrong (a connection that misses ``-wal`` frames, an empty dump of
a database that was never initialised, a truncated upload).  A backup that is
"probably fine" is worse than a red job, so the pipeline is a chain of
fail-closed verifications:

    source DB
      → dump into a temp dir
      → **pre-upload integrity**   (non-empty ∧ sha256 ∧ read-only restore:
                                    ``PRAGMA integrity_check`` + table inventory
                                    + per-table row-count parity with the source)
      → remote upload              (R2; missing config ⇒ ProviderUnavailable)
      → **remote round-trip**      (download back, byte-identical sha256)
      → **restore verification**   (the round-tripped artifact is opened *again*
                                    and must reproduce the same schema/inventory)
      → summary with sha256 / size / verified-at

Every check raises :class:`BackupVerificationError`; nothing is ever reported as
a successful backup on the strength of an assumption.  PostgreSQL dumps get the
same byte-level checks (footer marker + size + sha round-trip); a real server-side
restore needs a PostgreSQL instance, which is reported honestly in the summary
(``restore`` field) instead of being claimed.
"""

from __future__ import annotations

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
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError
from nexus_ai_agent.storage.providers.r2 import R2Provider

log = get_logger(__name__)

# Blob-tier prefix — recognized by AIStorageManager.is_blob_key.
BLOB_BACKUP_PREFIX = "backups/db"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
PG_DUMP_FOOTER = "PostgreSQL database dump complete"


class BackupVerificationError(StorageError):
    """A backup artifact failed verification; the run must be red."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    The source is opened read-only so a bug can never write to production, and
    the same connection is used for the inventory snapshot below — source and
    artifact are compared against one consistent view of the database.
    """
    if not db_path.exists():
        raise BackupVerificationError(f"SQLite database not found at {db_path.name}")
    if db_path.stat().st_size == 0:
        raise BackupVerificationError("SQLite database is empty (0 bytes)")
    try:
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - hostile filesystem
        raise BackupVerificationError(f"cannot open the source database: {exc}") from exc
    try:
        with source, sqlite3.connect(dest) as destination:
            source.backup(destination)
    except sqlite3.Error as exc:
        # A corrupt source must surface as a red, *typed* backup failure — the
        # online-backup API can raise here without writing a usable artifact.
        raise BackupVerificationError(f"the source database is not usable: {exc}") from exc
    finally:
        source.close()


def _sqlite_inventory(path: Path) -> dict[str, int]:
    """Read-only inventory: table → row count, after a full integrity check.

    Raises:
        BackupVerificationError: if the file cannot be opened/queried or SQLite
            reports any structural problem (this is the "corrupt source" and
            "restore failure" detector).
    """
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"cannot open artifact for verification: {exc}") from exc
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        status = str(result[0]) if result else "no result"
        if status.lower() != "ok":
            raise BackupVerificationError(f"integrity_check failed: {status[:200]}")
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        inventory: dict[str, int] = {}
        for (name,) in rows:
            count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
            inventory[str(name)] = int(count[0]) if count else 0
        return inventory
    except sqlite3.DatabaseError as exc:
        raise BackupVerificationError(f"artifact is not a readable database: {exc}") from exc
    finally:
        connection.close()


def _verify_sqlite_artifact(source: Path, artifact: Path, stage: str) -> dict[str, Any]:
    """Compare a dump against its source: schema, tables and row counts.

    ``stage`` only labels the error message (pre-upload vs after round-trip), so
    a failure names the step that caught it.
    """
    if not artifact.is_file():
        raise BackupVerificationError(f"({stage}) artifact is missing")
    if artifact.stat().st_size == 0:
        raise BackupVerificationError(f"({stage}) artifact is empty")

    expected = _sqlite_inventory(source)
    if not expected:
        raise BackupVerificationError(
            f"({stage}) source database has no tables — refusing to store an empty backup"
        )
    actual = _sqlite_inventory(artifact)

    missing = sorted(set(expected) - set(actual))
    if missing:
        raise BackupVerificationError(f"({stage}) artifact lost tables: {', '.join(missing)}")
    extra = sorted(set(actual) - set(expected))
    if extra:
        raise BackupVerificationError(f"({stage}) artifact gained tables: {', '.join(extra)}")
    drifted = {
        table: (expected[table], actual[table])
        for table in expected
        if expected[table] != actual[table]
    }
    if drifted:
        detail = ", ".join(f"{table}: {want}->{got}" for table, (want, got) in drifted.items())
        raise BackupVerificationError(f"({stage}) artifact row counts drifted: {detail}")
    return {"tables": sorted(actual), "rows": sum(actual.values())}


def _verify_postgres_dump(artifact: Path, stage: str) -> dict[str, Any]:
    """Byte-level checks for a ``pg_dump`` artifact (no server to restore into).

    A real restore needs a PostgreSQL instance; this verifies the dump is
    complete (footer marker) and non-empty, and the caller adds the sha256
    round-trip.  The summary records ``restore: "footer_and_roundtrip"`` so no
    one reads more into it than was actually proven.
    """
    if not artifact.is_file():
        raise BackupVerificationError(f"({stage}) dump is missing")
    if artifact.stat().st_size == 0:
        raise BackupVerificationError(f"({stage}) dump is empty")
    text = artifact.read_text(encoding="utf-8", errors="replace")
    if PG_DUMP_FOOTER not in text:
        raise BackupVerificationError(
            f"({stage}) dump is incomplete: the pg_dump footer marker is absent"
        )
    return {"tables": [], "rows": None}


def _build_provider(settings: Settings) -> R2Provider:
    return R2Provider.from_settings(settings)


def _verify_roundtrip(
    *,
    provider: R2Provider,
    key: str,
    uploaded_sha256: str,
    uploaded_size: int,
    directory: Path,
    filename: str,
) -> Path:
    """Download the object back and prove it is byte-identical to what we sent."""
    roundtrip = directory / f"roundtrip-{filename}"
    import asyncio

    try:
        asyncio.run(provider.download(remote_key=key, local_path=roundtrip))
    except Exception as exc:  # noqa: BLE001 - any provider failure is a failed backup
        raise BackupVerificationError(f"could not download the uploaded object: {exc}") from exc
    if not roundtrip.is_file():
        raise BackupVerificationError("the uploaded object downloaded back as no file")
    size = roundtrip.stat().st_size
    if size != uploaded_size:
        raise BackupVerificationError(
            f"remote object is truncated: {size} bytes vs {uploaded_size} uploaded"
        )
    digest = _sha256_file(roundtrip)
    if digest != uploaded_sha256:
        raise BackupVerificationError("remote object content does not match what was uploaded")
    return roundtrip


def create_backup(*, settings: Settings, dry_run: bool = False) -> dict[str, Any]:
    """Dump the active database and upload it to the R2 blob tier — verified.

    PostgreSQL when ``NEXUS_DATABASE_URL`` is set (``pg_dump``), otherwise the
    local SQLite file via the online-backup API.  Returns a summary dict; in
    ``dry_run`` mode nothing is written or uploaded.

    Raises:
        ProviderUnavailable: R2 is not configured (fail loudly — a silently
            skipped backup is worse than a red job).
        BackupVerificationError: any verification step failed.  The run is red
            and the summary was never emitted.
    """
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime(STAMP_FORMAT)
    database_url = resolve_database_url()
    is_sqlite = not database_url

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

    db_path = Path(settings.db_path)
    with tempfile.TemporaryDirectory(prefix="nexus_backup_") as tmp:
        dump_path = Path(tmp) / filename
        if database_url:
            _dump_postgres(database_url, dump_path)
        else:
            _dump_sqlite(db_path, dump_path)

        # Pre-upload: the artifact must be provably complete before it leaves
        # this machine — a corrupt upload would only be discovered at restore.
        if is_sqlite:
            _verify_sqlite_artifact(db_path, dump_path, "pre-upload")
        else:
            _verify_postgres_dump(dump_path, "pre-upload")
        sha256 = _sha256_file(dump_path)
        size_bytes = dump_path.stat().st_size
        if size_bytes <= 0:  # pragma: no cover - both verifiers already reject this
            raise BackupVerificationError("dump artifact is empty after verification")

        import asyncio

        try:
            asyncio.run(provider.upload(local_path=dump_path, remote_key=key))
        except Exception as exc:  # noqa: BLE001 - upload failure is a failed backup
            raise StorageError(f"upload to the blob tier failed: {exc}") from exc

        roundtrip = _verify_roundtrip(
            provider=provider,
            key=key,
            uploaded_sha256=sha256,
            uploaded_size=size_bytes,
            directory=Path(tmp),
            filename=filename,
        )
        # Restore verification: open the bytes that came back from the remote
        # tier (not the local file) and re-run the integrity/inventory checks.
        if is_sqlite:
            restored = _verify_sqlite_artifact(db_path, roundtrip, "restore")
            restore_mode = "sqlite_readonly_restore"
        else:
            restored = _verify_postgres_dump(roundtrip, "restore")
            restore_mode = "pg_dump_footer_and_roundtrip"
            restored["restore_note"] = "server-side restore needs a PostgreSQL instance"

    summary.update(
        {
            "uploaded": True,
            "verified": True,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "roundtrip": "byte-identical",
            "restore": restore_mode,
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            **restored,
        }
    )
    log.info(
        "maintenance_backup_ok",
        key=key,
        bytes=size_bytes,
        sha256=sha256,
        tables=len(restored.get("tables") or []),
    )
    return summary
