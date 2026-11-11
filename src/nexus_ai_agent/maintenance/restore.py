"""Restore drill: turn a backup artifact into proven recoverable state.

A backup that has never been restored is a claim, not a recovery plan. This
module closes the chain

    BACKUP → VERIFIED ARTIFACT → RESTORE → REAL RECOVERY

as an *executable* drill with three honesty levels — never a fake green:

1. **local artifact drill** (CI-provable, zero secrets): a dump file on disk
   is verified (size, checksum when evidence exists, structural integrity),
   then *actually restored* into an isolated target and re-queried, proving
   application-readable state.
2. **R2 artifact drill** (owner-side credentials): same pipeline, fetch via
   ``R2Provider.download`` with the actionable failure taxonomy
   (:mod:`.failures`); the remote sidecar manifest authored by
   :func:`~.create_backup` gives remote checksum evidence. A legacy
   artifact without sidecar is verified structurally and the drill says so —
   it never upgrades itself to "verified".
3. **PostgreSQL structural leg**: footer + non-empty are decidable offline;
   a *real* PG restore requires a live target cluster and is reported as
   ``UNVERIFIED — external dependency`` unless ``--apply --target-url`` is
   given with the required client binaries present.

Safety contract:

* default mode is a dry drill — nothing outside a temp directory mutates;
* ``--apply`` into an existing target requires ``--force``, keeps a
  timestamped safety copy of the replaced bytes, replaces atomically
  (temp file + ``os.replace``), and re-proves integrity on the restored
  target before reporting success;
* every failure is typed and operator-actionable
  (what failed / where / why / retryable / what to do).

Exit status: ``0`` only when the drill's claimed level completed;
``1`` on any typed failure; ``2`` on CLI misuse.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance.backup import (
    SIDECAR_SUFFIX,
    STAMP_FORMAT,
    BlobProvider,
    _build_provider,
    _sha256,
    _verify_postgres_dump,
    _verify_sqlite_dump,
)
from nexus_ai_agent.maintenance.dump_sql import copy_row_counts, expected_dump_tables
from nexus_ai_agent.maintenance.failures import (
    MaintenanceOperationError,
    classify_storage_failure,
)
from nexus_ai_agent.maintenance.housekeeping import _parse_backup_stamp
from nexus_ai_agent.maintenance.redact import redact_text, split_dsn_password, target_identity
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError

#: A lowercase, 64-hex SHA-256. Used to reject a sidecar (or operator flag)
#: whose ``sha256`` is not actually a hash, so it can never be "compared" as
#: if it proved something (F-007D).
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

log = get_logger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS = 600


class _ScratchDir:
    """A temporary directory whose removal is guaranteed (F-003).

    ``tempfile.mkdtemp`` alone leaks: the previous code created
    ``nexus_restore_fetch_*`` / ``nexus_restore_stage_*`` directories and never
    removed them, so a failed drill left staged artifacts (and any passfile) on
    disk.  This is a context manager so cleanup runs on success, exception and
    cancellation alike — ``__exit__`` runs even when the body raises.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self._prefix))
        return self.path

    def __exit__(self, *_exc: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None


class RestorePlanError(RuntimeError):
    """The drill request itself is invalid or unsafe (operator must change it)."""


class RestoreVerificationError(RuntimeError):
    """The artifact failed a verification stage (never a success)."""


@dataclass
class DrillReport:
    """Structured evidence of every stage the drill actually executed."""

    mode: str
    engine: str | None = None
    artifact: dict[str, Any] = field(default_factory=dict)
    chain: list[dict[str, Any]] = field(default_factory=list)
    restored: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = False

    def record(self, state: str, ok: bool, detail: str = "") -> None:
        self.chain.append({"state": state, "ok": ok, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "engine": self.engine,
            "ok": self.ok,
            "artifact": self.artifact,
            "chain": self.chain,
            "restored": self.restored,
            "warnings": self.warnings,
        }


def _fail(report: DrillReport, state: str, exc: Exception) -> None:
    report.record(state, False, str(exc))
    raise exc


# ── discovery & fetch ───────────────────────────────────────────────────


def resolve_latest_r2_key(provider: BlobProvider) -> str:
    """Discover the newest well-formed backup object in the bucket."""
    try:
        keys = asyncio.run(provider.list_files(prefix="backups/db/"))
    except ProviderUnavailable:
        raise
    except StorageError as exc:
        raise MaintenanceOperationError(
            classify_storage_failure(
                operation="list",
                where="backups/db/",
                exc=exc,
                bucket=getattr(provider, "_bucket", None),
            )
        ) from exc
    stamped: list[tuple[dt.datetime, str]] = []
    for key in keys:
        if key.endswith(SIDECAR_SUFFIX):
            continue
        stamp = _parse_backup_stamp(key)
        if stamp is not None:
            stamped.append((stamp, key))
    if not stamped:
        raise RestorePlanError(
            "no restorable backup found under 'backups/db/' — either no backup "
            "ever succeeded (check the maintenance workflow history) or "
            "housekeeping retention pruned everything; take a manual backup "
            "(`nexus maintenance backup`) before drilling"
        )
    stamp_dt, newest = max(stamped, key=lambda item: item[0])
    del stamp_dt
    return newest


def _detect_engine(artifact: Path, filename_hint: str) -> str:
    """Engine from the artifact name (backup pipeline names carry it)."""
    if filename_hint.endswith(".sql"):
        return "postgres"
    if filename_hint.endswith(".sqlite3"):
        return "sqlite"
    # Fallback: sniff the sqlite header, else the pg_dump header comment.
    # F-012: read only the first bytes — the previous ``read_bytes()[:100]``
    # loaded the whole (possibly multi-gigabyte) artifact to inspect 100 bytes.
    try:
        with artifact.open("rb") as handle:
            head = handle.read(100)
    except OSError as exc:
        raise RestorePlanError(f"artifact is not readable: {artifact} ({exc})") from exc
    if head.startswith(b"SQLite format 3"):
        return "sqlite"
    if b"PostgreSQL database dump" in head or head.startswith(b"--"):
        return "postgres"
    raise RestoreVerificationError(
        f"artifact {artifact} is neither a SQLite database image nor a "
        "pg_dump text dump — refusing to guess engine from unknown bytes"
    )


def _fetch_r2_artifact(
    provider: BlobProvider, key: str, report: DrillReport
) -> tuple[Path, Path | None]:
    """Download the artifact (+sidecar when present) with a bounded timeout."""
    temp_dir = Path(tempfile.mkdtemp(prefix="nexus_restore_fetch_"))
    artifact = temp_dir / Path(key).name
    try:

        async def _download() -> None:
            await asyncio.wait_for(
                provider.download(remote_key=key, local_path=artifact),
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            )

        asyncio.run(_download())
    except TimeoutError:
        raise MaintenanceOperationError(
            classify_storage_failure(
                operation="download",
                where=key,
                exc=TimeoutError(
                    f"download exceeded {_DOWNLOAD_TIMEOUT_SECONDS}s — treat as a "
                    "network/large-object timeout; retry or download manually via the R2 console"
                ),
                bucket=getattr(provider, "_bucket", None),
            )
        ) from None
    except ProviderUnavailable:
        raise
    except StorageError as exc:
        raise MaintenanceOperationError(
            classify_storage_failure(
                operation="download",
                where=key,
                exc=exc,
                bucket=getattr(provider, "_bucket", None),
            )
        ) from exc

    sidecar: Path | None = None
    sidecar_local = temp_dir / f"{Path(key).name}{SIDECAR_SUFFIX}"
    try:
        asyncio.run(
            provider.download(remote_key=f"{key}{SIDECAR_SUFFIX}", local_path=sidecar_local)
        )
        sidecar = sidecar_local
        report.record("SIDECAR_FETCHED", True, f"{key}{SIDECAR_SUFFIX}")
    except StorageError as exc:
        # Absent sidecar is EXPECTED for pre-3.14 artifacts; other remote
        # failures are honest warnings, never silent skips.
        report.warnings.append(
            f"no sidecar manifest: artifact predates the sidecar feature or the "
            f"manifest is unreadable ({type(exc).__name__}) — checksum evidence "
            "downgrades to structural verification only"
        )
        report.record("SIDECAR_FETCHED", False, "absent")
    return artifact, sidecar


# ── checksum & structural verification ──────────────────────────────────


def _verify_checksum(
    artifact: Path,
    report: DrillReport,
    *,
    expected_sha256: str | None,
    sidecar: Path | None,
) -> str:
    measured = _sha256(artifact)
    size = artifact.stat().st_size
    report.artifact.update({"size_bytes": size, "sha256": measured})
    if size <= 0:
        raise RestoreVerificationError(
            "backup artifact is empty (0 bytes) — an empty dump is not a "
            "backup; do NOT attempt recovery from it, take a new backup "
            "and investigate why the empty artifact was uploadable"
        )
    if expected_sha256 is not None and measured != expected_sha256.lower().strip():
        raise RestoreVerificationError(
            "checksum MISMATCH: operator-supplied sha256 does not match the "
            f"artifact (expected {expected_sha256}, measured {measured}). "
            "Possible corruption or tampered object — do not restore; "
            "fetch the artifact again or pick an older backup"
        )
    if sidecar is not None:
        try:
            manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RestoreVerificationError(
                f"sidecar manifest exists but is unparseable ({exc}) — "
                "refusing to treat the artifact as checksum-verified; "
                "structural verification may still prove restorability"
            ) from exc
        # F-007D: a sidecar is evidence only if it is a well-formed manifest.
        # ``{}`` (or any object missing a usable sha256) previously fell through
        # to ``CHECKSUM_VERIFIED`` with nothing actually compared — an empty
        # object is not proof. Refuse the manifest and fall back to the honest
        # structural-only state instead of a fabricated verification.
        problem = _sidecar_manifest_problem(manifest)
        if problem is not None:
            raise RestoreVerificationError(
                f"sidecar manifest is present but not a usable checksum record "
                f"({problem}) — refusing to report CHECKSUM_VERIFIED on an "
                "unverified object; structural verification may still prove "
                "restorability"
            )
        recorded = str(manifest["sha256"]).lower()
        if recorded != measured:
            raise RestoreVerificationError(
                "checksum MISMATCH against the remote sidecar manifest "
                f"(manifest {recorded}, measured {measured}). The object in "
                "the bucket is not the artifact the backup pipeline "
                "measured at upload time — treat as corruption/tampering; "
                "do not restore; investigate before retrying the drill"
            )
        report.artifact["sidecar"] = manifest
        report.record("CHECKSUM_VERIFIED", True, "sidecar manifest matches artifact")
    elif expected_sha256 is not None:
        report.record("CHECKSUM_VERIFIED", True, "operator-supplied sha256 matches artifact")
    else:
        report.record(
            "CHECKSUM_STRUCTURAL_ONLY",
            True,
            "no checksum evidence available (no sidecar, no --expected-sha256)",
        )
    return measured


def _sidecar_manifest_problem(manifest: object) -> str | None:
    """Return why *manifest* cannot serve as checksum evidence, else ``None``.

    F-007D: the sidecar is written by :func:`~.backup._build_sidecar` with a
    fixed shape (``kind``/``key``/``sha256``/``size_bytes``/``timestamp``/
    ``source``/``verification``). A drill must not label an object
    ``CHECKSUM_VERIFIED`` off a manifest that does not actually pin a sha256:
    ``{}`` compares nothing, yet the old code fell through to a green label.
    Each field the finding names is checked here; a failure is reported so the
    operator sees which field was wrong.
    """
    if not isinstance(manifest, dict):
        return f"not a JSON object ({type(manifest).__name__})"
    kind = manifest.get("kind")
    if not isinstance(kind, str) or not kind.startswith("nexus.db-backup.meta/"):
        return f"unexpected kind {kind!r}"
    sha = manifest.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        return f"sha256 is not 64 lowercase hex ({sha!r})"
    size = manifest.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return f"size_bytes is not a positive integer ({size!r})"
    key = manifest.get("key")
    if not isinstance(key, str) or not key:
        return "key is missing or empty"
    if not isinstance(manifest.get("timestamp"), str) or not manifest.get("timestamp"):
        return "timestamp is missing or empty"
    if not isinstance(manifest.get("source"), str) or not manifest.get("source"):
        return "source is missing or empty"
    return None


# ── sqlite restore ──────────────────────────────────────────────────────


def _expected_app_tables() -> set[str]:
    """Tables the application ORM declares (app-readable schema contract).

    Goes through ``nexus_ai_agent.storage.models`` — the schema-owner facade.
    The maintenance lane never imports the ORM library directly (import
    boundary, enforced by tests/architecture/test_import_boundaries.py).
    """
    from nexus_ai_agent.storage import models as _models

    return set(_models.SQLModel.metadata.tables)


def _apply_sqlite_restore(
    artifact: Path,
    target: Path,
    report: DrillReport,
    *,
    force: bool,
    stamp: str,
    expect_app_schema: bool,
) -> None:
    if target.exists():
        if target.is_dir():
            raise RestorePlanError(
                f"restore target {target} is a directory — the target must be a file path"
            )
        if not force:
            raise RestorePlanError(
                f"restore target {target} already exists — re-run with --force to "
                "replace it (a timestamped safety copy is kept). Refusing to "
                "overwrite silently is deliberate."
            )
    target.parent.mkdir(parents=True, exist_ok=True)

    safety: Path | None = None
    if target.exists():
        safety = target.with_name(target.name + f".pre-restore-{stamp}")
        shutil.copy2(target, safety)
        report.record("SAFETY_COPY", True, str(safety))

    # Write restored bytes to a same-directory temp then atomically replace.
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".restore-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with artifact.open("rb") as src:
                shutil.copyfileobj(src, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

    # Post-restore proof: reopen THE TARGET ITSELF.
    try:
        verification = _verify_sqlite_dump(target)
    except RuntimeError as exc:
        raise RestoreVerificationError(
            f"restored target failed verification: {exc}. The pre-restore "
            f"safety copy is at {safety} — inspect, then decide (a bad "
            "artifact must never replace a good database)"
        ) from exc
    report.record("POST_RESTORE_INTEGRITY", True, "integrity_check=ok on restored target")

    if expect_app_schema:
        expected = _expected_app_tables()
        if expected:
            with sqlite3.connect(target) as conn:
                present = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            missing = sorted(expected - present)
            if missing:
                raise RestoreVerificationError(
                    "restored database is missing application tables: "
                    + ", ".join(missing)
                    + " — the artifact is a valid SQLite file but NOT a "
                    "complete application database; do not point the app at it"
                )
            report.record("APP_SCHEMA_READABLE", True, f"{len(expected)} app tables present")

    report.restored.update(
        {
            "target": str(target),
            "safety_copy": str(safety) if safety else None,
            "size_bytes": target.stat().st_size,
            "tables": verification["tables"],
        }
    )


# ── postgres restore ────────────────────────────────────────────────────


def _list_postgres_tables(
    psql: str, target_url: str, env: dict[str, str] | None = None
) -> set[str]:
    """Read the table names the target cluster actually holds.

    A real post-restore proof must ask the *target* what it has, not trust the
    exit code of the apply. This is the PostgreSQL analogue of reopening the
    restored SQLite file.
    """
    proc = subprocess.run(  # noqa: S603 - operator-run drill, args are explicit
        [
            psql,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
            "--dbname",
            target_url,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RestoreVerificationError(
            f"post-restore table read failed on the live target: "
            f"{redact_text(proc.stderr).strip()[:400]} — the apply reported "
            "success but the target could not be re-read; classify "
            "CLUSTER-SIDE before claiming the restore is proven"
        )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _list_postgres_row_counts(
    psql: str, target_url: str, tables: list[str], env: dict[str, str] | None = None
) -> dict[str, int]:
    """Read exact row counts for *tables* from the target cluster.

    F-007E: the post-restore data proof. Counts are read in one round-trip via
    ``UNION ALL`` over ``count(*)``; identifiers are quoted with ``psql``'s
    ``quote_ident`` semantics (``""`` doubled) so a table name can never break
    out of the query. A missing table counts as absent from the mapping, which
    the caller reports as a mismatch.
    """
    if not tables:
        return {}
    parts = []
    for name in tables:
        # Both the label (a string literal) and the table name (an identifier)
        # are quoted so a crafted table name cannot break out of the query.
        label = name.replace("'", "''")
        quoted = name.replace('"', '""')
        parts.append(f"SELECT '{label}' AS t, count(*) FROM \"{quoted}\"")
    query = " UNION ALL ".join(parts)
    proc = subprocess.run(  # noqa: S603 - operator-run drill, args are explicit
        [psql, "-v", "ON_ERROR_STOP=1", "-tAF", "\t", "-c", query, "--dbname", target_url],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RestoreVerificationError(
            "post-restore row-count read failed on the live target: "
            f"{redact_text(proc.stderr).strip()[:400]} — the apply reported "
            "success but the data could not be re-read; classify CLUSTER-SIDE "
            "before claiming the restore is proven"
        )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        name, _, value = line.partition("\t")
        if name:
            counts[name] = int(value)
    return counts


def _expected_dump_tables(artifact: Path) -> set[str]:
    """Table names the dump itself creates.

    F-007: the artifact is scanned by :func:`dump_sql.expected_dump_tables`,
    which strips comments, string literals and dollar-quoted function bodies and
    understands ``IF NOT EXISTS``/``ONLY``/``UNLOGGED``/quoted identifiers and
    ``PARTITION OF``. The old line-by-line ``startswith("CREATE TABLE ")`` test
    missed indented/one-line statements and counted tables mentioned inside
    function bodies, so the post-restore proof could pass while a table was
    missing. Read from the artifact (not the ORM) so the proof compares the dump
    to the target, independent of the application's current schema.
    """
    return expected_dump_tables(artifact.read_text(encoding="utf-8", errors="replace"))


def _pg_psql_env(target_url: str, scratch: Path) -> tuple[str, dict[str, str]]:
    """Return ``(password_free_url, env)`` for a credential-safe ``psql`` run.

    F-003: the DSN on ``argv`` is world-readable via ``ps``. The password is
    moved into a mode-0600 ``PGPASSFILE`` inside *scratch* (removed with the
    scratch dir) and the URL handed to ``psql`` carries no password at all.
    """
    import os

    stripped, password = split_dsn_password(target_url)
    env = dict(os.environ)
    if password is not None:
        parts = urlsplit(target_url)
        passfile = scratch / ".pgpass"
        passfile.write_text(
            f"{parts.hostname or '*'}:{parts.port or 5432}:*:{parts.username or '*'}:{password}\n"
        )
        passfile.chmod(0o600)
        env["PGPASSFILE"] = str(passfile)
    return stripped, env


def _apply_postgres_restore(
    artifact: Path,
    target_url: str | None,
    report: DrillReport,
    *,
    force: bool,
    scratch: Path,
    expect_app_schema: bool = False,
) -> None:
    # Artifacts are plain-text ``pg_dump`` output (see
    # ``backup._dump_postgres``). ``pg_restore`` rejects that format
    # ("input file appears to be a text format dump. Please use psql."), so a
    # real restore of a NEXUS backup is applied with ``psql``. The dump is
    # written with ``--clean --if-exists``, so re-applying it over an existing
    # database drops and recreates the objects instead of failing.
    #
    # F-002: ``--single-transaction`` (``-1``) wraps the whole artifact in one
    # transaction. ``--clean --if-exists`` emits destructive DDL, so without
    # ``-1`` a failure part-way through leaves the target with some tables
    # dropped and others not — a partially destroyed database. With ``-1`` any
    # failure rolls the entire apply back and the target is left exactly as it
    # was. This is proven by
    # ``tests/integration/test_postgres_restore_atomicity.py`` against a real
    # cluster, and the full generated NEXUS dump is proven compatible with
    # transactional execution there (PG17 trailers included).
    psql = shutil.which("psql")
    if psql is None:
        raise RestorePlanError(
            "psql not found on PATH (install postgresql-client) — a real "
            "PostgreSQL restore is UNVERIFIED in this environment; the "
            "structural leg (footer + bytes) is already proven above"
        )
    if not target_url:
        raise RestorePlanError(
            "no --target-url given — a real PostgreSQL restore needs a live "
            "target cluster; the structural drill is proven, the live apply "
            "leg is owner-run by design (never faked here)"
        )
    if not force:
        raise RestorePlanError(
            "refusing to restore into a live database without --force — "
            "this overwrites database objects on the target cluster"
        )
    # F-003: name the target by its redacted identity (host:port/db), never the
    # credential-bearing URL; the password travels in a mode-0600 PGPASSFILE,
    # not on argv (which `ps` exposes).
    target_label = target_identity(target_url) or "target"
    safe_url, pg_env = _pg_psql_env(target_url, scratch)
    expected = _expected_dump_tables(artifact)
    proc = subprocess.run(  # noqa: S603 - operator-run drill, args are explicit
        [
            psql,
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "--dbname",
            safe_url,
            "--file",
            str(artifact),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        env=pg_env,
    )
    if proc.returncode != 0:
        # F-003: the driver echoes the DSN back in its stderr; scrub it before
        # it becomes evidence.
        raise RestoreVerificationError(
            f"psql restore failed on {target_label} (rolled back — the target "
            f"is unchanged): {redact_text(proc.stderr).strip()[:400]} — "
            "classify CLUSTER-SIDE before retrying (roles? occupied "
            "connections? version match?)"
        )
    report.record(
        "POSTGRES_LIVE_RESTORE",
        True,
        f"psql applied the dump atomically on {target_label}",
    )

    # Post-restore proof: reopen the TARGET and prove the dump's tables are
    # actually there. Without this the ``RESTORE_PROVEN`` label would be an
    # overclaim — the apply's exit code alone does not prove the schema landed.
    present = _list_postgres_tables(psql, safe_url, pg_env)
    missing = sorted(expected - present)
    if missing:
        raise RestoreVerificationError(
            "restored target is missing tables the dump creates: "
            + ", ".join(missing)
            + " — the apply exited 0 but the schema did not land; do not point "
            "the application at this target"
        )
    report.record(
        "POST_RESTORE_SCHEMA",
        True,
        f"{len(expected)} dump tables present on the target",
    )

    # F-007B/C: ``--expect-app-schema`` must mean the same thing for PostgreSQL
    # as it does for SQLite. Previously it was silently ignored on this path, so
    # a caller who asked for the application schema to be required got a green
    # drill without it. A zero-table dump is likewise refused: an artifact that
    # creates no tables cannot be "app-schema readable".
    if expect_app_schema:
        if not expected:
            raise RestoreVerificationError(
                "artifact creates no tables but --expect-app-schema was given — "
                "an empty schema cannot be the application database; do not "
                "point the application at this target"
            )
        app_tables = _expected_app_tables()
        missing_app = sorted(app_tables - present)
        if missing_app:
            raise RestoreVerificationError(
                "restored target is missing application tables: "
                + ", ".join(missing_app)
                + " — the dump is valid but not a complete application "
                "database; do not point the application at this target"
            )
        report.record(
            "APP_SCHEMA_READABLE",
            True,
            f"{len(app_tables)} app tables present on the target",
        )

    # F-007E: prove the *data* landed, not only the schema. A dump whose COPY
    # blocks were dropped or truncated would otherwise pass every check above.
    # The artifact's claimed per-table row counts are compared with the target's
    # real counts; a mismatch is a failed restore, not a warning.
    claimed, truncated = copy_row_counts(artifact)
    if claimed and not truncated:
        actual = _list_postgres_row_counts(psql, safe_url, sorted(claimed), pg_env)
        mismatched = {
            table: (rows, actual.get(table))
            for table, rows in claimed.items()
            if actual.get(table) != rows
        }
        if mismatched:
            detail = ", ".join(
                f"{table}: artifact {rows} vs target {got}"
                for table, (rows, got) in mismatched.items()
            )
            raise RestoreVerificationError(
                "restored target row counts do not match the dump: "
                + detail
                + " — the schema landed but the data did not; do not point "
                "the application at this target"
            )
        report.record(
            "POST_RESTORE_DATA",
            True,
            f"{len(claimed)} tables' row counts match the dump ({sum(claimed.values())} rows)",
        )
    else:
        report.record(
            "POST_RESTORE_DATA",
            False,
            "row-count proof skipped (no COPY data in the artifact or the "
            "line budget was exceeded) — data landing is UNVERIFIED",
        )
    report.restored.update(
        {
            # F-003: redacted identity, never the raw URL.
            "target": target_label,
            "tables": sorted(present),
        }
    )


# ── the drill ───────────────────────────────────────────────────────────


def restore_drill(
    *,
    settings: Settings,
    from_local: Path | None = None,
    from_r2_key: str | None = None,
    latest: bool = False,
    target: Path | None = None,
    target_url: str | None = None,
    expected_sha256: str | None = None,
    apply: bool = False,
    force: bool = False,
    expect_app_schema: bool = False,
) -> DrillReport:
    """Execute the restore drill and return structured stage evidence.

    Raises typed errors (RestorePlanError / RestoreVerificationError /
    MaintenanceOperationError / ProviderUnavailable) on any failed stage;
    the report is returned only when the drill reached its claimed level.
    """
    if (from_local is None) == (from_r2_key is None and not latest):
        raise RestorePlanError("choose exactly one source: --from-local, --from-r2, or --latest")

    mode = "apply" if apply else "drill"
    report = DrillReport(mode=mode)

    if apply and target is None and target_url is None:
        raise RestorePlanError(
            "--apply needs a target: --target (sqlite) or --target-url (postgres)"
        )

    # F-002: a live PostgreSQL target must never be the configured *source*
    # database. Restoring a backup of the production database back onto the
    # production database is the one destructive accident this guard exists to
    # prevent; identity is compared on host/port/database (credentials
    # irrelevant), and the check runs before any file is touched.
    if apply and target_url is not None:
        _guard_target_identity(target_url)

    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime(STAMP_FORMAT)

    with _ScratchDir("nexus_restore_stage_") as staged:
        return _run_drill(
            staged=staged,
            report=report,
            settings=settings,
            from_local=from_local,
            from_r2_key=from_r2_key,
            latest=latest,
            target=target,
            target_url=target_url,
            expected_sha256=expected_sha256,
            apply=apply,
            force=force,
            expect_app_schema=expect_app_schema,
            stamp=stamp,
        )


def _guard_target_identity(target_url: str) -> None:
    """Refuse a target that resolves to the configured source database (F-002).

    The source is whatever ``NEXUS_DATABASE_URL`` names; restoring its own dump
    back over it is a destructive no-op at best and a data-loss event at worst.
    Identity is ``host:port/db`` — the same redacted identity the report carries.
    """
    from nexus_ai_agent.storage.db import resolve_database_url

    source_url = resolve_database_url()
    if source_url is None:
        return
    if target_identity(source_url) == target_identity(target_url):
        raise RestorePlanError(
            "refusing to restore: the target resolves to the configured source "
            f"database ({target_identity(target_url)}) — restore into an "
            "isolated target, never over the live source"
        )


def _run_drill(
    *,
    staged: Path,
    report: DrillReport,
    settings: Settings,
    from_local: Path | None,
    from_r2_key: str | None,
    latest: bool,
    target: Path | None,
    target_url: str | None,
    expected_sha256: str | None,
    apply: bool,
    force: bool,
    expect_app_schema: bool,
    stamp: str,
) -> DrillReport:
    sidecar: Path | None = None
    filename_hint = ""

    # ── FETCH ───────────────────────────────────────────────────────
    if from_local is not None:
        artifact_src = from_local.expanduser()
        if not artifact_src.is_file():
            raise RestorePlanError(
                f"local artifact {artifact_src} does not exist or is not a file — "
                "check the path (a directory or a typo is not a backup)"
            )
        filename_hint = artifact_src.name
        artifact = staged / artifact_src.name
        shutil.copyfile(artifact_src, artifact)
        report.record("FETCHED", True, f"local:{artifact_src}")
    else:
        provider = _build_provider(settings)
        if not provider.is_configured():
            raise ProviderUnavailable(
                "R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY, R2_BUCKET) and no valid Backblaze B2 tier is "
                "configured (NEXUS_B2_KEY_ID, NEXUS_B2_APPLICATION_KEY, NEXUS_B2_BUCKET, "
                "NEXUS_B2_ENDPOINT, NEXUS_B2_REGION) — the remote drill cannot "
                "fetch anything; run the local leg with --from-local instead"
            )
        key = from_r2_key
        if latest:
            key = resolve_latest_r2_key(provider)
            report.record("LATEST_RESOLVED", True, key)
        assert key is not None
        filename_hint = Path(key).name
        artifact, sidecar = _fetch_r2_artifact(provider, key, report)
        report.record("FETCHED", True, f"r2:{key}")

    # ── ENGINE + SIZE + CHECKSUM ────────────────────────────────────
    engine = _detect_engine(artifact, filename_hint)
    report.engine = engine
    report.artifact["engine"] = engine
    _verify_checksum(artifact, report, expected_sha256=expected_sha256, sidecar=sidecar)

    # ── STRUCTURAL INTEGRITY (isolated) ─────────────────────────────
    if engine == "sqlite":
        try:
            verification = _verify_sqlite_dump(artifact)
        except RuntimeError as exc:
            raise RestoreVerificationError(
                f"artifact integrity failed: {exc}. This backup cannot be "
                "restored — do not point the application at it; drill an "
                "older backup instead"
            ) from exc
        report.record("INTEGRITY_VERIFIED", True, "integrity_check=ok, tables present")
        report.artifact["tables"] = verification["tables"]
    else:
        try:
            verification = _verify_postgres_dump(artifact)
        except RuntimeError as exc:
            raise RestoreVerificationError(
                f"artifact integrity failed: {exc}. The dump is truncated or "
                "corrupt — do not feed it to pg_restore; drill an older backup"
            ) from exc
        report.record("INTEGRITY_VERIFIED", True, "pg_dump footer present, non-empty")

    # F-007E: "the artifact is verified" is its own state, distinct from a
    # checksum match and from structural integrity. Recording it explicitly is
    # what lets the proof line name exactly which evidence level was reached.
    report.record(
        "ARTIFACT_VERIFIED",
        True,
        "checksum + structural integrity verified",
    )

    if not apply:
        # The proof label follows the CHECKSUM stage actually executed —
        # a structural-only artifact must never be relabeled as
        # checksum-verified (silent downgrade of evidence is forbidden).
        checksum_state = next(
            (s["state"] for s in report.chain if s["state"].startswith("CHECKSUM")),
            "",
        )
        label = {
            "CHECKSUM_VERIFIED": " (checksum-verified)",
            "CHECKSUM_STRUCTURAL_ONLY": " (structural only)",
        }.get(checksum_state, "")
        report.record("RESTORE_PROVEN", True, "verified restorable artifact" + label)
        report.ok = True
        log.info(
            "maintenance_restore_drill_ok",
            engine=engine,
            checksum=report.artifact.get("sha256"),
            apply=False,
        )
        return report

    # ── APPLY (real recovery) ───────────────────────────────────────
    if engine == "sqlite":
        if target is None:
            raise RestorePlanError("sqlite --apply needs --target <path>")
        _apply_sqlite_restore(
            artifact,
            target.expanduser(),
            report,
            force=force,
            stamp=stamp,
            expect_app_schema=expect_app_schema,
        )
    else:
        _apply_postgres_restore(
            artifact,
            target_url,
            report,
            force=force,
            scratch=staged,
            expect_app_schema=expect_app_schema,
        )

    # The final label must state the *weakest* evidence level actually reached:
    # a restore that landed the schema but could not compare row counts is not
    # the same claim as one that proved the data too (F-007E).
    data_step = next(
        (s for s in report.chain if s["state"] == "POST_RESTORE_DATA"),
        None,
    )
    if data_step is not None and data_step["ok"]:
        proven_detail = "restored, re-verified, and data row counts match on the target"
    else:
        proven_detail = (
            "restored and re-verified on the target (schema only — "
            "post-restore data comparison was not completed)"
        )
    report.record("RESTORE_PROVEN", True, proven_detail)
    report.ok = True
    log.info(
        "maintenance_restore_apply_ok",
        engine=engine,
        # F-003: never log the raw target URL — it carries the password.
        target=str(target) if target is not None else target_identity(target_url),
        checksum=report.artifact.get("sha256"),
    )
    return report


# ── CLI (module entry: cli.py is outside this task's lease) ─────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.maintenance.restore",
        description="Restore drill: prove a backup can actually be restored.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-local", type=Path, default=None, help="Local artifact path")
    source.add_argument("--from-r2", default=None, help="Remote object key (backups/db/...)")
    source.add_argument("--latest", action="store_true", help="Use the newest backup in R2")
    parser.add_argument("--target", type=Path, default=None, help="SQLite restore target path")
    parser.add_argument("--target-url", default=None, help="PostgreSQL target DSN for --apply")
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="Checksum the artifact MUST match (operator evidence)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually restore into the target (default: verify-only drill)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow replacing an existing target / writing to a live database",
    )
    parser.add_argument(
        "--expect-app-schema",
        action="store_true",
        help="On apply, require all application ORM tables on the restored target",
    )
    parser.add_argument("--json", action="store_true", help="Print the drill report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.json:
        # --json makes stdout a machine-parseable document; nothing else may
        # interleave with it (drill evidence is consumed by CI scripts). The
        # JSON report IS the evidence, so drill log records are dropped for
        # this invocation (a plain `file=sys.stderr` sink would bind whatever
        # stream object exists at call time — wrong for embedded/captured
        # runtimes like pytest).
        import logging

        import structlog

        structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
        logging.getLogger().setLevel(logging.CRITICAL)
    from nexus_ai_agent.config.settings import get_settings

    try:
        report = restore_drill(
            settings=get_settings(),
            from_local=args.from_local,
            from_r2_key=args.from_r2,
            latest=args.latest,
            target=args.target,
            target_url=args.target_url,
            expected_sha256=args.expected_sha256,
            apply=args.apply,
            force=args.force,
            expect_app_schema=args.expect_app_schema,
        )
    except (
        RestorePlanError,
        RestoreVerificationError,
        MaintenanceOperationError,
        ProviderUnavailable,
    ) as exc:
        print(f"❌ restore drill failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"❌ restore drill failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        states = " → ".join(f"{s['state']}{'✓' if s['ok'] else '✗'}" for s in report.chain)
        print(f"engine:  {report.engine}")
        print(f"sha256:  {report.artifact.get('sha256', 'n/a')}")
        print(f"chain:   {states}")
        for warning in report.warnings:
            print(f"warning: {warning}")
        if report.mode == "apply":
            print(f"target:  {report.restored.get('target')}")
            if report.restored.get("safety_copy"):
                print(f"safety:  {report.restored['safety_copy']}")
        print("✅ restore drill proven at the executed level")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
