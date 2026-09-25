"""Database backup → Cloudflare R2 blob tier (Phase 5), with verified success.

Stateless by design: each run produces one timestamped dump and uploads it
under ``backups/db/<UTC-stamp>/``. Nothing is tracked between runs; the R2
side is pruned later by :mod:`nexus_ai_agent.maintenance.housekeeping`.

Release-level contract (task-167 / P0-C, extended by task-164) — a backup
run may call itself a success only when ALL of these hold:

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
5. **recoverability is proven, not assumed** (task-164): when a scratch
   PostgreSQL target is supplied, the uploaded artifact is restored into a
   fresh database created from ``template0`` with
   ``psql -X -v ON_ERROR_STOP=1 --single-transaction`` and the restored
   table inventory must equal the source inventory (table set; row counts
   are recorded and any drift is reported explicitly),
6. failure ⇒ non-zero exit, a typed :class:`BackupError` carrying a
   ``classification`` (so "the owner has not set the secrets" is never
   confused with "the dump/upload/restore broke"), and is *never* reported
   as success; success ⇒ the summary carries a UTC ``timestamp``.

Restore semantics come first: the unit of recovery is *one plain-SQL
pg_dump (``--no-owner --no-privileges``) restorable with stock psql into an
empty database*, or *one SQLite file openable by stock sqlite3*. Everything
else in this module exists to prove that unit is real.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
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

#: The four R2 settings, in the order the operator sets them
#: (docs/ops/r2-storage.md §3/§4). Each is reported *individually*.
R2_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("R2_ACCOUNT_ID", "r2_account_id"),
    ("R2_ACCESS_KEY_ID", "r2_access_key_id"),
    ("R2_SECRET_ACCESS_KEY", "r2_secret_access_key"),
    ("R2_BUCKET", "r2_bucket"),
)

_URL_CREDENTIALS_RE = re.compile(r"://[^/@\s]+@")


# ── failure taxonomy ─────────────────────────────────────────────────────────


class BackupError(RuntimeError):
    """Typed backup failure. ``classification`` is stable, machine-readable."""

    classification = "failed"


class BackupNotConfigured(BackupError, ProviderUnavailable):
    """Owner action required (missing secrets/config) — nothing was attempted."""

    classification = "not_configured"


class BackupSourceError(BackupError):
    """The database could not be read/dumped (path missing, pg_dump failed)."""

    classification = "dump_failed"


class BackupVerificationError(BackupError):
    """Local integrity or remote round-trip verification failed."""

    classification = "verification_failed"


class BackupUploadError(BackupError):
    """The provider refused/failed the upload."""

    classification = "upload_failed"


class BackupRestoreError(BackupError):
    """The artifact could not be restored into the scratch target."""

    classification = "restore_failed"


def redact(text: str) -> str:
    """Strip ``user:password@`` from anything that looks like a URL."""
    return _URL_CREDENTIALS_RE.sub("://***@", text)


# ── preflight ────────────────────────────────────────────────────────────────


def _binary_version(binary: str | None) -> str | None:
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def preflight(settings: Settings, *, require_postgres: bool = False) -> dict[str, Any]:
    """Name exactly what is (not) configured. Booleans only — never values.

    ``ok`` is True only when a real backup could proceed: every R2 variable
    present AND a database source that exists (PostgreSQL URL + pg_dump on
    PATH, or — unless ``require_postgres`` — an existing SQLite file).
    """
    r2_present = {env: bool(getattr(settings, attr)) for env, attr in R2_ENV_VARS}
    missing_r2 = [env for env, present in r2_present.items() if not present]
    database_url = resolve_database_url()
    pg_dump = shutil.which("pg_dump")
    psql = shutil.which("psql")
    sqlite_path = Path(settings.db_path)

    report: dict[str, Any] = {
        "r2": r2_present,
        "r2_complete": not missing_r2,
        "database": "postgres" if database_url else "sqlite",
        "database_url_present": bool(database_url),
        "sqlite_path_exists": sqlite_path.exists(),
        "pg_dump": pg_dump is not None,
        "pg_dump_version": _binary_version(pg_dump),
        "psql": psql is not None,
        "require_postgres": require_postgres,
    }
    problems: list[str] = [f"{env} is not set" for env in missing_r2]
    if database_url:
        if pg_dump is None:
            problems.append("pg_dump not found on PATH (install postgresql-client)")
    elif require_postgres:
        problems.append(
            "NEXUS_DATABASE_URL is not set and --require-postgres is on: refusing to "
            "back up the local SQLite file of this (ephemeral) host"
        )
    elif not sqlite_path.exists():
        problems.append(f"SQLite database not found at {sqlite_path}")
    report["problems"] = problems
    report["ok"] = not problems
    return report


# ── dump ─────────────────────────────────────────────────────────────────────


def _dump_postgres(database_url: str, dest: Path) -> None:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise BackupSourceError("pg_dump not found on PATH (install postgresql-client)")
    # --no-owner/--no-privileges: the restore target (a scratch cluster or a
    # different provider) need not know the source's roles. This is also
    # Neon's documented restore recipe (--no-owner --no-acl).
    proc = subprocess.run(
        [pg_dump, "--no-owner", "--no-privileges", "--file", str(dest), database_url],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupSourceError(f"pg_dump failed: {redact(proc.stderr.strip())[:400]}")


def _dump_sqlite(db_path: Path, dest: Path) -> None:
    """Consistent copy via the sqlite3 online-backup API (safe while app runs).

    NOTE: the API happily "succeeds" against a zero-byte or corrupt source,
    yielding a syntactically valid but *empty* destination — verification in
    :func:`_verify_sqlite_dump` exists precisely to refuse those artifacts.
    """
    if not db_path.exists():
        raise BackupSourceError(f"SQLite database not found at {db_path}")
    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── verification (local) ─────────────────────────────────────────────────────


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
            raise BackupVerificationError(
                f"backup artifact is not a readable SQLite DB: {exc}"
            ) from exc
    if integrity != "ok":
        raise BackupVerificationError(f"SQLite integrity_check failed: {integrity[:200]}")
    if not counts:
        raise BackupVerificationError(
            "backup artifact contains no user tables — refusing to call an "
            "empty dump a successful backup (source DB may be corrupt)"
        )
    verification["integrity"] = "ok"
    verification["tables"] = counts
    return verification


def _verify_postgres_dump(dump_path: Path) -> dict[str, Any]:
    """Structural verification for a plain-text pg_dump artifact.

    Locally decidable facts only: non-empty + pg_dump completion footer.
    The *restore* proof lives in :func:`_verify_postgres_restore` and needs
    a scratch cluster (the nightly job provides one).
    """
    data = dump_path.read_bytes()
    if not data:
        raise BackupVerificationError("pg_dump artifact is empty")
    # A real plain-format dump ends with the comment block
    #     --\n-- PostgreSQL database dump complete\n--\n\n
    # i.e. the footer is the *second-to-last* comment line, not the last
    # byte run.  The previous ``rstrip().endswith(footer)`` check only ever
    # matched hand-written fixtures and rejected every genuine pg_dump
    # (found by the real-PostgreSQL drill, task-164).  Accept the footer
    # anywhere in the final trailer lines.
    trailer = data[-512:].decode("utf-8", errors="replace")
    trailer_lines = [line.strip() for line in trailer.splitlines() if line.strip()]
    if _PG_DUMP_FOOTER not in trailer_lines[-3:]:
        raise BackupVerificationError(
            f"pg_dump artifact is truncated or failed mid-run (missing footer {_PG_DUMP_FOOTER!r})"
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
        try:
            asyncio.run(provider.download(remote_key=key, local_path=downloaded))
        except Exception as exc:  # provider-specific errors → typed
            raise BackupVerificationError(
                f"round-trip download of {key} failed: {redact(str(exc))[:300]}"
            ) from exc
        actual = _sha256(downloaded)
    if actual != expected_sha256:
        raise BackupVerificationError(
            "round-trip verification failed: remote bytes do not match the "
            f"local artifact (local sha256={expected_sha256}, remote sha256={actual})"
        )
    return {"roundtrip": "byte-identical"}


# ── verification (restore into a scratch PostgreSQL) ─────────────────────────


def _pg_inventory(database_url: str) -> dict[str, int]:
    """``{"schema.table": rowcount}`` for every user table (psycopg, core dep)."""
    import psycopg
    from psycopg import sql

    counts: dict[str, int] = {}
    with psycopg.connect(database_url, connect_timeout=30) as conn:
        rows = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY 1, 2"
        ).fetchall()
        for schema, table in rows:
            query = sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
            row = conn.execute(query).fetchone()
            counts[f"{schema}.{table}"] = int(row[0]) if row else 0
    return counts


def _scratch_db_name(stamp: str) -> str:
    return f"nexus_restore_{stamp.replace('-', '_')}"


def _with_database(url: str, dbname: str) -> str:
    """Return ``url`` pointing at ``dbname`` (path component replaced)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _run_psql(args: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    psql = shutil.which("psql")
    if psql is None:
        raise BackupRestoreError("psql not found on PATH (install postgresql-client)")
    return subprocess.run(
        [psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _verify_postgres_restore(
    dump_path: Path,
    *,
    target_url: str,
    stamp: str,
    source_inventory: dict[str, int] | None,
) -> dict[str, Any]:
    """Restore the artifact into a fresh scratch DB and compare inventories.

    The database is created from ``template0`` (the documented restore
    recipe), loaded with ``psql -X -v ON_ERROR_STOP=1 --single-transaction``
    so any SQL error aborts the whole load, inventoried, and always dropped
    — even on failure — so the scratch cluster stays reusable.
    """
    dbname = _scratch_db_name(stamp)
    admin_url = target_url
    create = _run_psql(
        [admin_url, "-c", f'CREATE DATABASE "{dbname}" TEMPLATE template0'], timeout=120
    )
    if create.returncode != 0:
        raise BackupRestoreError(
            f"could not create scratch database: {redact(create.stderr.strip())[:300]}"
        )
    scratch_url = _with_database(target_url, dbname)
    try:
        load = _run_psql([scratch_url, "--single-transaction", "-f", str(dump_path)])
        if load.returncode != 0:
            raise BackupRestoreError(
                f"psql restore failed (exit {load.returncode}): "
                f"{redact(load.stderr.strip())[-400:]}"
            )
        try:
            restored = _pg_inventory(scratch_url)
        except Exception as exc:
            raise BackupRestoreError(
                f"restored database could not be inventoried: {redact(str(exc))[:300]}"
            ) from exc
    finally:
        drop = _run_psql([admin_url, "-c", f'DROP DATABASE IF EXISTS "{dbname}"'], timeout=120)
        if drop.returncode != 0:
            log.warning("maintenance_backup_scratch_drop_failed", database=dbname)

    if not restored:
        raise BackupRestoreError("restored database contains no user tables")
    result: dict[str, Any] = {
        "restore": "ok",
        "restore_target_database": dbname,
        "restored_tables": restored,
    }
    if source_inventory is not None:
        missing = sorted(set(source_inventory) - set(restored))
        extra = sorted(set(restored) - set(source_inventory))
        if missing or extra:
            raise BackupRestoreError(
                "restored table set differs from source "
                f"(missing={missing[:10]}, unexpected={extra[:10]})"
            )
        drift = {
            table: {"source": source_inventory[table], "restored": restored[table]}
            for table in source_inventory
            if source_inventory[table] != restored[table]
        }
        result["source_tables"] = source_inventory
        result["row_count_drift"] = drift
        result["row_counts_match"] = not drift
    return result


# ── orchestration ────────────────────────────────────────────────────────────


def _build_provider(settings: Settings) -> R2Provider:
    return R2Provider.from_settings(settings)


def write_evidence(path: Path, summary: dict[str, Any]) -> None:
    """Persist the run summary (success *or* failure) as a JSON evidence file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")


def create_backup(
    *,
    settings: Settings,
    dry_run: bool = False,
    require_postgres: bool = False,
    restore_target_url: str | None = None,
) -> dict[str, Any]:
    """Dump the active database, verify the artifact, upload, re-verify, restore.

    See the module docstring for the full success contract. In ``dry_run``
    mode nothing is written or uploaded and the summary says so explicitly.
    Raises a typed :class:`BackupError`; the summary of a failed run is
    attached to the exception as ``exc.summary`` for evidence reporting.
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
        "status": "pending",
        "dry_run": dry_run,
        "source": source,
        "key": key,
        "uploaded": False,
        "verified": False,
        "restore_proven": False,
        "timestamp": now.isoformat(),
    }

    if dry_run:
        log.info("maintenance_backup_dry_run", source=source, key=key)
        summary["status"] = "dry_run"
        summary["detail"] = "dry-run: nothing dumped or uploaded"
        return summary

    try:
        check = preflight(settings, require_postgres=require_postgres)
        summary["preflight"] = check
        provider = _build_provider(settings)
        # Configuration gate — fail loudly and *name each missing piece*: a
        # silently skipped backup is worse than a red job, and an unnamed
        # red job is what kept task-164 open for five nights.
        problems: list[str] = []
        if not provider.is_configured():
            problems.extend(
                [f"{env} is not set" for env, present in check["r2"].items() if not present]
                or ["R2 provider reports not configured"]
            )
        if require_postgres and not database_url:
            problems.append(
                "NEXUS_DATABASE_URL is not set and --require-postgres is on: refusing to "
                "back up the local SQLite file of this (ephemeral) host"
            )
        if problems:
            raise BackupNotConfigured(
                "backup not attempted — " + "; ".join(problems) + " (see docs/ops/r2-storage.md §4)"
            )

        with tempfile.TemporaryDirectory(prefix="nexus_backup_") as tmp:
            dump_path = Path(tmp) / filename
            source_inventory: dict[str, int] | None = None
            if database_url:
                if restore_target_url:
                    try:
                        source_inventory = _pg_inventory(database_url)
                    except Exception as exc:
                        raise BackupSourceError(
                            f"source inventory failed: {redact(str(exc))[:300]}"
                        ) from exc
                _dump_postgres(database_url, dump_path)
                verification = _verify_postgres_dump(dump_path)
            else:
                _dump_sqlite(Path(settings.db_path), dump_path)
                verification = _verify_sqlite_dump(dump_path)

            size_bytes = dump_path.stat().st_size
            if size_bytes <= 0:
                raise BackupVerificationError("backup artifact is empty — refusing to upload")
            sha256 = _sha256(dump_path)
            summary.update({"size_bytes": size_bytes, "sha256": sha256})

            try:
                asyncio.run(provider.upload(local_path=dump_path, remote_key=key))
            except BackupError:
                raise
            except Exception as exc:
                raise BackupUploadError(
                    f"upload of {key} failed: {redact(str(exc))[:300]}"
                ) from exc
            summary["uploaded"] = True
            verification.update(
                _verify_round_trip(provider, key=key, local_path=dump_path, expected_sha256=sha256)
            )
            summary["verified"] = True

            if database_url and restore_target_url:
                verification.update(
                    _verify_postgres_restore(
                        dump_path,
                        target_url=restore_target_url,
                        stamp=stamp,
                        source_inventory=source_inventory,
                    )
                )
                summary["restore_proven"] = True
            summary["verification"] = verification
    except BackupError as exc:
        summary["status"] = "failed"
        summary["classification"] = exc.classification
        summary["error"] = redact(str(exc))
        exc.summary = summary  # type: ignore[attr-defined]
        log.error(
            "maintenance_backup_failed",
            classification=exc.classification,
            key=key,
            error=summary["error"],
        )
        raise

    summary["status"] = "success"
    log.info(
        "maintenance_backup_ok",
        key=key,
        bytes=summary["size_bytes"],
        sha256=summary["sha256"],
        verified=True,
        restore_proven=summary["restore_proven"],
    )
    return summary


def restore_drill(*, source_url: str, target_url: str) -> dict[str, Any]:
    """Prove the dump→restore mechanics without R2 (no secrets needed).

    Used by the scheduled ``restore-drill`` job and the PostgreSQL
    integration test: dump ``source_url`` exactly like the nightly backup
    does, verify the footer, restore into a scratch DB on ``target_url``
    and require the restored inventory to equal the source inventory —
    including exact row counts, since the drill source is quiescent.
    """
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime(STAMP_FORMAT)
    summary: dict[str, Any] = {"status": "pending", "timestamp": now.isoformat(), "drill": True}
    try:
        source_inventory = _pg_inventory(source_url)
        if not source_inventory:
            raise BackupSourceError("drill source has no user tables — nothing to prove")
        with tempfile.TemporaryDirectory(prefix="nexus_restore_drill_") as tmp:
            dump_path = Path(tmp) / f"nexus-pg-{stamp}.sql"
            _dump_postgres(source_url, dump_path)
            verification = _verify_postgres_dump(dump_path)
            summary["size_bytes"] = dump_path.stat().st_size
            summary["sha256"] = _sha256(dump_path)
            verification.update(
                _verify_postgres_restore(
                    dump_path,
                    target_url=target_url,
                    stamp=stamp,
                    source_inventory=source_inventory,
                )
            )
        if not verification.get("row_counts_match"):
            raise BackupRestoreError(
                f"drill row counts differ after restore: {verification.get('row_count_drift')}"
            )
        summary["verification"] = verification
    except BackupError as exc:
        summary["status"] = "failed"
        summary["classification"] = exc.classification
        summary["error"] = redact(str(exc))
        exc.summary = summary  # type: ignore[attr-defined]
        log.error("maintenance_restore_drill_failed", classification=exc.classification)
        raise
    summary["status"] = "success"
    log.info("maintenance_restore_drill_ok", tables=len(source_inventory))
    return summary
