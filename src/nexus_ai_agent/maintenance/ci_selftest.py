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
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sqlite3
import time
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

PRODUCTION_URL_ENV = "NEXUS_DATABASE_URL"


class SelfTestError(RuntimeError):
    """A maintenance leg failed; the message names the leg and the evidence."""


def prepare_database(path: Path) -> dict[str, Any]:
    """Create the real application schema at *path* and require WAL mode."""
    from nexus_ai_agent.storage.db import create_all_tables

    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(create_all_tables(str(path)))
    with sqlite3.connect(str(path)) as connection:
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


def run_backup(database: Path, tier: Path) -> dict[str, Any]:
    """Dump -> verify -> upload -> re-verify through ``create_backup``."""
    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.maintenance.backup import create_backup

    # ``model_copy`` (not constructor kwargs) because these fields are declared
    # with validation aliases, which pydantic treats as env-only inputs.
    settings = Settings.model_validate({}).model_copy(
        update={
            "db_path": str(database),
            "maintenance_local_tier": str(tier),
            "database_url": None,
        }
    )
    result = create_backup(settings=settings)
    if not result.get("verified"):
        raise SelfTestError(f"backup artifact was not verified: {result}")
    artifact = tier / str(result["key"])
    if not artifact.is_file():
        raise SelfTestError(f"backup artifact missing from the local tier: {artifact}")
    return {**result, "artifact_path": str(artifact)}


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
    payload = json.loads(buffer.getvalue() or "{}")
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
    """Run the requested legs and return one evidence document."""
    if (os.environ.get(PRODUCTION_URL_ENV) or "").strip():
        raise SelfTestError(
            f"{PRODUCTION_URL_ENV} is set: the CI self-test never runs against a configured "
            "database. Unset it (or run the production maintenance job instead)."
        )
    evidence: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "ci-selftest",
        "only": only,
    }
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
    evidence["ok"] = True
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_ai_agent.maintenance.ci_selftest",
        description="End-to-end maintenance self-test for a CI checkout without cloud secrets.",
    )
    parser.add_argument("--only", choices=("backup", "drill", "reconcile", "all"), default="all")
    parser.add_argument("--db", required=True, help="SQLite path to create and exercise")
    parser.add_argument("--tier", required=True, help="local maintenance tier directory")
    parser.add_argument("--workspace-root", default=".", help="repository root for reconciliation")
    parser.add_argument("--json-out", default=None, help="write the evidence document here")
    args = parser.parse_args(argv)

    database = Path(args.db).expanduser()
    tier = Path(args.tier).expanduser()
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


if __name__ == "__main__":
    raise SystemExit(main())
