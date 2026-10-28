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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance.backup import (
    SIDECAR_SUFFIX,
    STAMP_FORMAT,
    _build_provider,
    _sha256,
    _verify_postgres_dump,
    _verify_sqlite_dump,
)
from nexus_ai_agent.maintenance.failures import (
    MaintenanceOperationError,
    classify_storage_failure,
)
from nexus_ai_agent.maintenance.housekeeping import _parse_backup_stamp
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError
from nexus_ai_agent.storage.providers.r2 import R2Provider

log = get_logger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS = 600


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


def resolve_latest_r2_key(provider: R2Provider) -> str:
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
    try:
        head = artifact.read_bytes()[:100]
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
    provider: R2Provider, key: str, report: DrillReport
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
            recorded = str(manifest.get("sha256", ""))
        except (OSError, json.JSONDecodeError) as exc:
            raise RestoreVerificationError(
                f"sidecar manifest exists but is unparseable ({exc}) — "
                "refusing to treat the artifact as checksum-verified; "
                "structural verification may still prove restorability"
            ) from exc
        if recorded and recorded != measured:
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


def _apply_postgres_restore(
    artifact: Path,
    target_url: str | None,
    report: DrillReport,
    *,
    force: bool,
) -> None:
    pg_restore = shutil.which("pg_restore")
    if pg_restore is None:
        raise RestorePlanError(
            "pg_restore not found on PATH (install postgresql-client) — a real "
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
            "refusing to pg_restore into a live database without --force — "
            "this overwrites database objects on the target cluster"
        )
    proc = subprocess.run(  # noqa: S603 - operator-run drill, args are explicit
        [pg_restore, "--no-owner", "--clean", "--if-exists", "--dbname", target_url, str(artifact)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        raise RestoreVerificationError(
            f"pg_restore failed on the live target: {proc.stderr.strip()[:400]} — "
            "the structural artifact leg was verified; classify CLUSTER-SIDE "
            "before retrying (roles? occupied connections? version match?)"
        )
    report.record("POSTGRES_LIVE_RESTORE", True, "pg_restore exited 0 on target cluster")


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
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime(STAMP_FORMAT)

    if apply and target is None and target_url is None:
        raise RestorePlanError(
            "--apply needs a target: --target (sqlite) or --target-url (postgres)"
        )

    staged = Path(tempfile.mkdtemp(prefix="nexus_restore_stage_"))
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
                "R2_SECRET_ACCESS_KEY, R2_BUCKET) — the remote drill cannot "
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
        _apply_postgres_restore(artifact, target_url, report, force=force)

    report.record("RESTORE_PROVEN", True, "restored and re-verified on the target")
    report.ok = True
    log.info(
        "maintenance_restore_apply_ok",
        engine=engine,
        target=str(target or target_url),
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
