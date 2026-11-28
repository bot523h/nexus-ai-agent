"""End-to-end maintenance self-test for a CI checkout with no cloud secrets.

The scheduled ``maintenance`` workflow is fail-closed: without
``NEXUS_DATABASE_URL`` plus a complete B2/R2 tier it refuses to produce a
backup, which is correct for production and red for a repository that has never
configured those secrets. That red run proves nothing about the code.

This module is the honest alternative. When the secrets are absent, CI builds a
**real** SQLite database with the application schema in WAL mode, then runs the
same three production entry points against it:

* ``create_backup`` — dump, verify, upload to the local maintenance tier,
  re-verify (no mock, no dry run);
* ``restore_drill`` — restore *the uploaded artifact* into an isolated target
  and require the application schema back;
* ``reconciler.main`` — the nightly artifact/binding reconciliation.

Every leg writes machine-readable evidence, and the process exits non-zero when
any leg fails. It refuses to run when ``NEXUS_DATABASE_URL`` is set: the CI lane
must never be pointed at production by accident.

The storage tier is pinned the same way. The self-test builds its settings with
every remote-tier field cleared **and** injects a ``LocalCacheProvider``
directly into ``create_backup``, so a complete ``NEXUS_B2_*`` or ``R2_*`` tier
in the runner's environment cannot receive a synthetic SQLite artifact: the
provider factory is never consulted. The evidence document says so explicitly
(``mode`` and ``production_postgres_dr_verified``), because a green local
self-test is not a PostgreSQL + object-storage disaster-recovery drill and must
never be read as one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus_ai_agent.config.settings import Settings

PRODUCTION_URL_ENV = "NEXUS_DATABASE_URL"

#: Environment prefixes that select a remote maintenance tier.
REMOTE_TIER_ENV_PREFIXES = ("NEXUS_B2_", "R2_")

#: Settings fields that carry remote-tier credentials; cleared on the copy the
#: self-test hands to ``create_backup``.
REMOTE_TIER_FIELDS = (
    "b2_key_id",
    "b2_application_key",
    "b2_bucket",
    "b2_endpoint",
    "b2_region",
    "r2_account_id",
    "r2_access_key_id",
    "r2_secret_access_key",
    "r2_bucket",
)

#: Evidence label for the lane. Deliberately not "production".
MODE_LABEL = "ci_local_sqlite_selftest"


class SelfTestError(RuntimeError):
    """A maintenance leg failed; the message names the leg and the evidence."""


def prepare_database(path: Path) -> dict[str, Any]:
    """Create the real application schema at *path* and require WAL mode."""
    from nexus_ai_agent.storage.db import create_all_tables

    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(create_all_tables(str(path)))
    with sqlite3.connect(str(path)) as connection:
        # ``create_all_tables`` initialises once per *path*, so a second run in
        # the same process (the self-test deletes and recreates its scratch
        # directory) returns before the WAL switch. Re-applying it here keeps the
        # promise this leg makes: the file under test is a real WAL database.
        connection.execute("PRAGMA journal_mode=WAL")
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    if journal_mode != "wal":
        raise SelfTestError(f"expected a WAL database, got journal_mode={journal_mode!r}")
    if len(tables) < 10:
        raise SelfTestError(f"application schema looks empty ({len(tables)} tables)")
    return {
        "path": str(path),
        "journal_mode": journal_mode,
        "tables": len(tables),
        "bytes": path.stat().st_size,
    }


def suppressed_remote_env() -> list[str]:
    """Names — never values — of the remote-tier variables visible to the runner."""
    return sorted(name for name in os.environ if name.startswith(REMOTE_TIER_ENV_PREFIXES))


@contextlib.contextmanager
def suppressed_remote_environment() -> Iterator[list[str]]:
    """Remove every remote-tier variable from ``os.environ`` for the block.

    ``Settings`` fills its aliases from the environment, so a runner that holds
    a complete production tier would otherwise carry those values inside the
    settings object the self-test hands to ``create_backup``. The provider is
    injected and the factory is forbidden, which makes that harmless — but the
    settings themselves should be local by construction, not by luck.
    """
    names = [
        name
        for name in sorted({*suppressed_remote_env(), PRODUCTION_URL_ENV})
        if name in os.environ
    ]
    saved = {name: os.environ.pop(name) for name in names}
    try:
        yield names
    finally:
        os.environ.update(saved)


def local_only_settings(database: Path, tier: Path) -> Settings:
    """Settings whose storage tier is *structurally* local.

    Built with every remote-tier variable removed from the environment and then
    with every remote-tier field set to ``None``, so the result is local twice
    over: nothing in the ambient environment can put a credential into it, and
    even a code path that consulted ``build_blob_provider`` would find nothing
    to select. The provider itself is injected by :func:`run_backup`.
    """
    from nexus_ai_agent.config.settings import Settings

    with suppressed_remote_environment():
        # ``model_copy`` (not constructor kwargs) because these fields are
        # declared with validation aliases, which pydantic treats as env-only.
        base = Settings.model_validate({})
    return base.model_copy(
        update={
            "db_path": str(database),
            "maintenance_local_tier": str(tier),
            "database_url": None,
            **{field: None for field in REMOTE_TIER_FIELDS},
        }
    )


def run_backup(database: Path, tier: Path) -> dict[str, Any]:
    """Dump -> verify -> upload -> re-verify through ``create_backup``.

    The provider is injected, so the CI lane never asks the environment which
    tier to use.
    """
    from nexus_ai_agent.maintenance.backup import create_backup
    from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider

    provider = LocalCacheProvider(tier)
    try:
        result = create_backup(settings=local_only_settings(database, tier), provider=provider)
    except RuntimeError as exc:
        # StorageError, ProviderUnavailable, MaintenanceOperationError and the
        # round-trip mismatch all derive from RuntimeError: one clean message
        # and exit 1 instead of a traceback.
        raise SelfTestError(f"backup leg failed: {exc}") from exc
    if not result.get("verified"):
        raise SelfTestError(f"backup artifact was not verified: {result}")
    if result.get("tier") != provider.name:
        raise SelfTestError(f"backup used tier {result.get('tier')!r}, expected {provider.name!r}")
    artifact = tier / str(result["key"])
    if not artifact.is_file():
        raise SelfTestError(f"backup artifact missing from the local tier: {artifact}")
    return {**result, "artifact_path": str(artifact), "storage_tier": provider.name}


def run_drill(artifact: Path, workdir: Path) -> dict[str, Any]:
    """Restore the *uploaded* artifact and require the application schema back."""
    from nexus_ai_agent.maintenance import restore

    workdir.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = restore.main(
            [
                "--from-local",
                str(artifact),
                "--apply",
                "--target",
                str(workdir / "restored.sqlite3"),
                "--force",
                "--expect-app-schema",
                "--json",
            ]
        )
    payload = json.loads(buffer.getvalue() or "{}")
    if code != 0 or payload.get("ok") is not True:
        raise SelfTestError(f"restore drill failed (exit {code}): {payload}")
    return {"ok": True, "engine": payload.get("engine"), "chain": payload.get("chain")}


def run_housekeeping_leg(*, database: Path, tier: Path) -> dict[str, Any]:
    """Prune a stale backup from its own local tier through ``run_housekeeping``.

    The prune uses a dedicated tier, not the backup leg's: the drill consumes the
    artifact this run uploaded, and the evidence document asserts it still
    exists. Retention 0 expires the seeded backup immediately, which exercises
    the *delete* path of the local tier against a provider passed explicitly —
    a housekeeping regression that fell back to the factory would raise inside
    the self-test guard instead of reaching a cloud bucket.
    """
    from nexus_ai_agent.maintenance.backup import BLOB_BACKUP_PREFIX
    from nexus_ai_agent.maintenance.housekeeping import run_housekeeping
    from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider

    prune_tier = tier.parent / "housekeeping-tier"
    provider = LocalCacheProvider(prune_tier)
    stale_key = f"{BLOB_BACKUP_PREFIX}/20000101-000000/nexus.sqlite3"
    stale_path = provider.path_for_key(stale_key)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(b"stale-but-structurally-valid-backup")

    temp_dir = tier.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    settings = local_only_settings(database, prune_tier).model_copy(
        update={"creative_temp_dir": str(temp_dir)}
    )
    result = run_housekeeping(
        settings=settings,
        backup_retention_days=0,
        provider=provider,
    )
    deleted = list(result["backups_deleted"])
    if stale_key not in deleted:
        raise SelfTestError(f"housekeeping did not prune the seeded backup: {result}")
    if stale_path.exists():
        raise SelfTestError("housekeeping reported a delete that did not happen")
    return {
        "provider": provider.name,
        "backups_deleted": len(deleted),
        "dry_run": result["dry_run"],
    }


def run_reconcile(database: Path, workspace_root: Path) -> dict[str, Any]:
    """The nightly artifact/binding reconciliation, dry run, fail on violation."""
    from nexus_ai_agent.maintenance import reconciler

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = reconciler.main(
            [
                "--workspace-root",
                str(workspace_root),
                "--database-path",
                str(database),
                "--queue-db",
                str(workspace_root / "nexus_jobs.db"),
                "--json",
                "--fail-on-violation",
            ]
        )
    payload: dict[str, Any] = json.loads(buffer.getvalue() or "{}")
    if code != 0:
        raise SelfTestError(f"reconciliation reported a divergence (exit {code}): {payload}")
    return payload


def run(
    *,
    only: str,
    database: Path,
    tier: Path,
    workspace_root: Path,
    artifact: Path | None = None,
) -> dict[str, Any]:
    """Run the requested legs and return one evidence document.

    Every leg executes inside :func:`forbid_remote_provider_factory`, so a code
    path that stopped injecting the local provider would fail closed here with
    :data:`~nexus_ai_agent.maintenance.backup.SELFTEST_FACTORY_FORBIDDEN`
    instead of quietly writing a synthetic artifact to a cloud bucket.
    """
    if (os.environ.get(PRODUCTION_URL_ENV) or "").strip():
        raise SelfTestError(
            f"{PRODUCTION_URL_ENV} is set: the CI self-test never runs against a configured "
            "database. Unset it (or run the production maintenance job instead)."
        )
    from nexus_ai_agent.maintenance.backup import forbid_remote_provider_factory

    evidence: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": MODE_LABEL,
        "only": only,
        # A local SQLite self-test is not a PostgreSQL + object-storage DR
        # drill; saying so in the document keeps the two from being confused.
        "production_postgres_dr_verified": False,
        "storage_tier": "local_cache",
        "remote_tier_env_suppressed": suppressed_remote_env(),
    }
    with forbid_remote_provider_factory():
        evidence["database"] = prepare_database(database)
        if only in ("backup", "all"):
            backup = run_backup(database, tier)
            evidence["backup"] = backup
            artifact = Path(backup["artifact_path"])
        if only in ("drill", "all"):
            if artifact is None:
                raise SelfTestError("the drill leg needs an artifact (run the backup leg first)")
            evidence["drill"] = run_drill(artifact, tier.parent / "restore-drill")
        if only in ("reconcile", "all"):
            evidence["reconcile"] = run_reconcile(database, workspace_root)
        if only == "all":
            evidence["housekeeping"] = run_housekeeping_leg(database=database, tier=tier)
    evidence["ok"] = True
    return evidence


def default_workdir() -> Path:
    """Deterministic scratch *parent* for the argument-less CI invocation.

    ``RUNNER_TEMP`` on GitHub Actions, the system temp directory elsewhere. The
    parent is fixed so a human always knows where to look; :func:`main` clears
    it, then creates the database in a per-run subdirectory so two runs in the
    same process cannot inherit each other's initialised path.
    """
    root = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(root) / "nexus-maintenance-selftest"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.maintenance.ci_selftest",
        description="End-to-end maintenance self-test for a CI checkout without cloud secrets.",
    )
    parser.add_argument("--only", choices=("backup", "drill", "reconcile", "all"), default="all")
    parser.add_argument("--db", default=None, help="SQLite path to create and exercise")
    parser.add_argument("--tier", default=None, help="local maintenance tier directory")
    parser.add_argument("--workspace-root", default=".", help="repository root for reconciliation")
    parser.add_argument("--json-out", default=None, help="write the evidence document here")
    args = parser.parse_args(argv)

    using_defaults = args.db is None and args.tier is None
    workdir = default_workdir()
    if using_defaults:
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        # A unique database file per run. The storage layer initialises a schema
        # once per *path* in a process, so reusing the path of a file this run
        # deleted would silently skip initialisation and leave an empty schema.
        database = Path(tempfile.mkdtemp(prefix="db-", dir=workdir)) / "nexus.sqlite3"
    else:
        database = Path(args.db).expanduser() if args.db else workdir / "nexus.sqlite3"
    tier = Path(args.tier).expanduser() if args.tier else workdir / "tier"
    tier.mkdir(parents=True, exist_ok=True)
    try:
        evidence = run(
            only=args.only,
            database=database,
            tier=tier,
            workspace_root=Path(args.workspace_root).expanduser(),
        )
    except SelfTestError as exc:
        print(f"❌ maintenance self-test failed: {exc}")
        return 1
    document = json.dumps(evidence, indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(document + "\n", encoding="utf-8")
        print(f"evidence: {args.json_out}")
    else:
        print(document)
    print(
        f"✅ maintenance self-test ({args.only}) — "
        f"backup={'verified' if 'backup' in evidence else 'skipped'}, "
        f"drill={'proven' if 'drill' in evidence else 'skipped'}, "
        f"reconcile={'clean' if 'reconcile' in evidence else 'skipped'}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
