from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import typer

from nexus_ai_agent.llm.litellm_provider import build_llm_provider
from nexus_ai_agent.llm.provider import LLMProvider  # noqa: F401 — re-exported for typing

app = typer.Typer(help="NEXUS AI Agent CLI")
checkpoints_app = typer.Typer(help="Inspect checkpoint lifecycle state")
golden_app = typer.Typer(help="Schema golden management (human-triggered only)")
metrics_app = typer.Typer(help="Observability snapshots")
maintenance_app = typer.Typer(help="Stateless maintenance (R2 DB backups, housekeeping)")
jobs_app = typer.Typer(help="Durable in-process job queue operations")
packs_app = typer.Typer(help="Capability-pack manifests (data-only packs)")


def _open_checkpoint_backend() -> tuple[Any, Path]:
    """Return ``(read_adapter, default_golden_path)`` for the active backend.

    Backend selection is the same as the runtime composition root:
    ``NEXUS_DATABASE_URL`` set → PostgreSQL (e.g. Neon), otherwise the
    configured SQLite file.  Both adapters satisfy the read-only
    ``CheckpointReadAdapter`` contract, so inspect/reconcile are
    backend-agnostic (PR3).
    """
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.storage.checkpoint_adapter import (
        CheckpointReadAdapter,
        SQLiteCheckpointAdapter,
    )
    from nexus_ai_agent.storage.checkpoint_reconciler import DEFAULT_GOLDEN
    from nexus_ai_agent.storage.db import resolve_database_url

    settings = get_settings()
    database_url = resolve_database_url()
    if database_url is not None:
        from nexus_ai_agent.storage.checkpoint_pg_adapter import (
            DEFAULT_PG_GOLDEN,
            PostgresCheckpointAdapter,
        )

        adapter: CheckpointReadAdapter = PostgresCheckpointAdapter(database_url)
        return adapter, DEFAULT_PG_GOLDEN
    adapter = SQLiteCheckpointAdapter(settings.checkpoint_path)
    return cast(CheckpointReadAdapter, adapter), DEFAULT_GOLDEN


def _open_lifecycle_store(
    settings: Any,
    database_url: str | None,
    *,
    strict: bool,
    read_only: bool,
) -> Any:
    """Return the lifecycle index store for the active backend.

    PostgreSQL → the ``nexus_checkpoint_lifecycle`` table (option A).
    SQLite → the local sidecar file (pre-existing behaviour).

    * ``strict=True`` (inspect): a missing *local* index file means
      "no metadata yet" → ``None``.  A missing table on PostgreSQL is a
      migration gap, surfaced as an error by the store's first query —
      never invented data.
    * ``read_only=True`` (inspect): the PostgreSQL connection is
      server-enforced read-only; inspect never touches access data.
    """
    from nexus_ai_agent.storage.checkpoint_reconciler import lifecycle_db_path

    if database_url is not None:
        from nexus_ai_agent.storage.checkpoint_lifecycle_pg_store import (
            PostgresCheckpointLifecycleStore,
        )

        return PostgresCheckpointLifecycleStore(database_url, read_only=read_only)
    from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
        SQLiteCheckpointLifecycleStore,
    )

    lifecycle_path = lifecycle_db_path(settings.checkpoint_path)
    if strict and (lifecycle_path == ":memory:" or not Path(lifecycle_path).exists()):
        return None
    return SQLiteCheckpointLifecycleStore(lifecycle_path)


@app.command()
def migrate(
    db_path: str | None = typer.Option(
        None,
        "--db-path",
        help=(
            "[deprecated since v0.2.0-D, removal 2026-10-01] Force a legacy "
            "SQLite path. Prefer NEXUS_DB_PATH, which `nexus migrate` honours "
            "via Alembic."
        ),
    ),
) -> None:
    """Apply database migrations (alembic upgrade head).

    Backs both backends automatically: NEXUS_DATABASE_URL (PostgreSQL/Neon)
    wins; otherwise the configured SQLite file is migrated.  Existing
    un-migrated databases keep working via the legacy create_all fallback.
    """
    from nexus_ai_agent.storage.migrations import run_migrations

    if db_path is not None:
        # Legacy stopgap: explicit path still bootstraps via create_all (idempotent).
        from nexus_ai_agent.storage.db import create_all_tables

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(create_all_tables(db_path))
        typer.echo(f"✓ Database initialized (legacy create_all) at {db_path}")
    else:
        run_migrations()
        typer.echo("✓ Database migrated to head")


@app.command("adopt-pg")
def adopt_pg(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--yes",
        help=(
            "Inspect the PostgreSQL database and report what adopt would do, "
            "without changing anything. Pass --yes to actually stamp it."
        ),
    ),
) -> None:
    """Adopt a pre-Alembic PostgreSQL/Neon database (D10).

    A database created by the old create_all stopgap has tables but no
    ``alembic_version`` marker.  ``nexus adopt-pg`` inspects it and:

    * zero drift  → stamps it at head (--yes), preserving all data;
    * schema drift → refuses with a clear error and leaves data untouched.

    Defaults to --dry-run so the outcome is always visible before any change.
    """
    from nexus_ai_agent.storage.adopt_pg import decide, inspect_postgres
    from nexus_ai_agent.storage.db import normalize_database_url, resolve_database_url

    url = resolve_database_url()
    if url is None:
        raise typer.BadParameter(
            "NEXUS_DATABASE_URL is not set; adopt-pg only targets PostgreSQL/Neon."
        )
    normalized = normalize_database_url(url)

    if dry_run:
        from nexus_ai_agent.storage.adopt_pg import ACTION_ADOPT, ACTION_FAIL, ACTION_MANAGED

        report = inspect_postgres(normalized)
        action = decide(report)
        if action == ACTION_ADOPT:
            typer.echo(
                f"dry-run: would ADOPT (stamp head) — {report.table_count} tables, "
                "zero drift. Re-run with --yes to apply."
            )
        elif action == ACTION_MANAGED:
            typer.echo(f"dry-run: already Alembic-managed ({report.table_count} tables).")
        elif action == ACTION_FAIL:
            typer.echo(
                f"dry-run: REFUSED — schema drift detected "
                f"(missing={report.missing_tables}, extra={report.extra_tables})."
            )
        else:
            typer.echo(
                f"dry-run: empty database ({report.table_count} tables). "
                "Run `nexus migrate` instead."
            )
        return

    from nexus_ai_agent.storage.adopt_pg import adopt_postgres

    report = adopt_postgres(normalized)
    typer.echo(f"✓ {report.action} — {report.table_count} tables at head")


@app.command()
def continuum(
    mode: str = typer.Argument("show", help="show | verify"),
) -> None:
    """Show or verify the committed project-state snapshot (.nexus/continuum.json).

    ``show`` prints the snapshot; ``verify`` compares HEAD against the
    recorded last-good commit and exits non-zero on drift.
    """
    from nexus_ai_agent.continuum.snapshot import verify_snapshot

    if mode == "show":
        from nexus_ai_agent.continuum.snapshot import SNAPSHOT_PATH

        typer.echo(f"# {SNAPSHOT_PATH}")
        typer.echo(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return
    if mode == "verify":
        problems = verify_snapshot()
        if problems:
            for problem in problems:
                typer.echo(f"✗ {problem}", err=True)
            raise typer.Exit(code=1)
        typer.echo("✓ continuum snapshot matches checkout")
        return
    raise typer.BadParameter("mode must be 'show' or 'verify'")


@metrics_app.command("snapshot")
def metrics_snapshot(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the current low-cardinality metrics snapshot."""
    import json

    from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

    snapshot = get_metrics_registry().snapshot()
    typer.echo(
        json.dumps(snapshot)
        if json_output
        else "\\n".join(f"{key} {value}" for key, value in snapshot.items())
    )


@checkpoints_app.command("inspect")
def inspect_checkpoints(
    json_output: bool = typer.Option(False, "--json"),
    thread: str | None = typer.Option(None, "--thread"),
) -> None:
    """Read checkpoint lifecycle state without touching access timestamps."""
    import json
    from datetime import datetime, timezone

    from nexus_ai_agent.adapters.langgraph.lifecycle_recording import nexus_access_context
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.domain.policies.retention import RetentionRecord, deletable, pinned
    from nexus_ai_agent.storage.db import resolve_database_url

    settings = get_settings()
    access_token = nexus_access_context.set("admin")
    adapter, _ = _open_checkpoint_backend()
    # Read-only by contract: inspect never creates or mutates the lifecycle
    # index (on PostgreSQL the read-only connection is server-enforced).
    # A missing local index file simply means "no metadata yet".
    database_url = resolve_database_url()
    lifecycle = _open_lifecycle_store(settings, database_url, strict=True, read_only=True)
    try:
        now = datetime.now(timezone.utc)
        records = lifecycle.records() if lifecycle is not None else []
        grouped: dict[str, list] = {}
        for record in records:
            grouped.setdefault(record.thread_id, []).append(record)
        threads = (
            [thread] if thread is not None else sorted(set(adapter.list_threads()) | set(grouped))
        )
        output = []
        for thread_id in threads:
            checkpoints = adapter.list_checkpoints(thread_id)
            metadata = grouped.get(thread_id, [])
            oldest = min((item.created_at for item in metadata), default=None)
            newest = max((item.created_at for item in metadata), default=None)
            last_accessed = max(
                (item.last_accessed_at for item in metadata if item.last_accessed_at), default=None
            )
            lifecycle_record = metadata[-1] if metadata else None
            retention = (
                RetentionRecord(
                    lifecycle_record.created_at,
                    lifecycle_record.last_accessed_at,
                    lifecycle_record.active_until,
                )
                if lifecycle_record is not None
                else None
            )
            pinned_value = pinned(retention, now=now) if retention is not None else "unknown"
            deletable_value = deletable(retention, now=now) if retention is not None else False
            entry: dict[str, Any] = {
                "schema": "inspect-v1",
                "thread_id": thread_id,
                "checkpoint_count": len(checkpoints),
                "oldest_created_at": oldest.isoformat() if oldest else "unknown",
                "newest_created_at": newest.isoformat() if newest else "unknown",
                "last_accessed_at": last_accessed.isoformat() if last_accessed else "unknown",
                "pinned": pinned_value,
                "active": bool(
                    lifecycle_record
                    and lifecycle_record.active_until
                    and lifecycle_record.active_until > now
                ),
                "resumable_within_window": bool(retention and not deletable_value),
                "would_delete": deletable_value,
                "would_free_bytes_estimate": adapter.estimate_thread_bytes(thread_id),
                "missing_lifecycle": not bool(metadata),
                "orphan_candidate_count": max(0, len(metadata) - len(checkpoints)),
            }
            # inspect-v1 contract: every undeterminable field is named here
            # (unknown values stay live in the output; see I10).
            entry["unknown_fields"] = sorted(
                name for name, value in entry.items() if value == "unknown"
            )
            output.append(entry)
        if json_output:
            typer.echo(json.dumps(output, default=str))
        else:
            for item in output:
                typer.echo(json.dumps(item, default=str, sort_keys=True))
    finally:
        adapter.close()
        if lifecycle is not None:
            lifecycle.close()
        nexus_access_context.reset(access_token)


@checkpoints_app.command("reconcile")
def reconcile_checkpoints(
    apply: bool = typer.Option(
        False, "--apply", help="Apply safe mutations (default: dry-run, no writes)"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile checkpoints and lifecycle metadata (dry-run by default).

    Two-way, eventual-consistency reconciliation.  Dry-run only measures and
    reports.  With ``--apply`` the reconciler may (a) backfill checkpoints
    missing from the lifecycle index with a protected estimated age, and
    (b) delete orphaned *lifecycle index rows* whose 24h block has elapsed
    while every anomaly guard holds.  It never deletes LangGraph rows.
    """
    import json

    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.storage.checkpoint_reconciler import (
        CheckpointReconciler,
        render_report,
    )
    from nexus_ai_agent.storage.db import resolve_database_url

    settings = get_settings()
    adapter, default_golden = _open_checkpoint_backend()
    # The reconciler owns the only lifecycle-mutation path in the CLI
    # (backfill/purge under the full guard stack); on PostgreSQL that
    # means the nexus_checkpoint_lifecycle table, never LangGraph rows.
    database_url = resolve_database_url()
    lifecycle = _open_lifecycle_store(settings, database_url, strict=False, read_only=False)
    try:
        reconciler = CheckpointReconciler(
            adapter,
            lifecycle,
            golden_path=default_golden,
            enabled=settings.lifecycle_hooks_enabled,
        )
        report = reconciler.run(apply=apply)
        if json_output:
            typer.echo(json.dumps(report.to_dict(), default=str))
        else:
            typer.echo(render_report(report))
    finally:
        adapter.close()
        lifecycle.close()


@golden_app.command("update")
def golden_update(
    backend: str = typer.Option("postgres", "--backend", help="postgres | sqlite"),
    url: str | None = typer.Option(
        None, "--url", help="PostgreSQL URL (default: NEXUS_DATABASE_URL)"
    ),
    path: str | None = typer.Option(
        None, "--path", help="SQLite checkpoint file (default: configured)"
    ),
    output: str | None = typer.Option(
        None, "--output", help="Write here instead of the canonical golden path"
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm the write (human-in-the-loop)"),
) -> None:
    """Write the schema golden for a backend. HUMAN-TRIGGERED ONLY.

    This is the *only* code path allowed to write a schema golden file.
    CI never runs it; ``reconcile`` only *asserts* against the committed
    golden.  Review the printed fingerprint before committing the file.
    """
    import json

    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.storage.db import resolve_database_url

    if backend not in ("postgres", "sqlite"):
        typer.echo(f"unknown backend: {backend} (use postgres | sqlite)", err=True)
        raise typer.Exit(code=2)

    if backend == "postgres":
        from nexus_ai_agent.storage.checkpoint_pg_adapter import (
            DEFAULT_PG_GOLDEN,
            FINGERPRINT_ALGORITHM,
            PostgresCheckpointAdapter,
        )

        database_url = url or resolve_database_url()
        if database_url is None:
            typer.echo("no PostgreSQL URL (set NEXUS_DATABASE_URL or pass --url)", err=True)
            raise typer.Exit(code=2)
        adapter: CheckpointReadAdapter = PostgresCheckpointAdapter(database_url)
        golden_path = DEFAULT_PG_GOLDEN
    else:
        from nexus_ai_agent.storage.checkpoint_adapter import (
            FINGERPRINT_ALGORITHM,
            CheckpointReadAdapter,
            SQLiteCheckpointAdapter,
        )
        from nexus_ai_agent.storage.checkpoint_reconciler import DEFAULT_GOLDEN

        sqlite_path = path or get_settings().checkpoint_path
        adapter = SQLiteCheckpointAdapter(sqlite_path)
        golden_path = DEFAULT_GOLDEN

    try:
        fingerprint = adapter.schema_fingerprint()
    finally:
        adapter.close()

    if output is not None:
        golden_path = Path(output)
    payload = json.dumps({"algorithm": FINGERPRINT_ALGORITHM, "fingerprint": fingerprint}, indent=2)
    previous = json.loads(golden_path.read_text(encoding="utf-8")) if golden_path.exists() else None
    if previous is not None and previous.get("fingerprint") != fingerprint:
        typer.echo("⚠ golden fingerprint CHANGES:")
        typer.echo(f"  old: {previous.get('fingerprint')}")
        typer.echo(f"  new: {fingerprint}")
    if not yes:
        typer.echo(f"would write {golden_path}:")
        typer.echo(payload)
        typer.echo("re-run with --yes to confirm (human-in-the-loop)")
        return
    golden_path.write_text(payload + "\n", encoding="utf-8")
    typer.echo(f"✓ wrote {golden_path}")
    typer.echo(payload)
    typer.echo("commit this file after review; reconcile will enforce it")


# ── Wave 2a: nexus packs — capability-pack manifests (data-only packs) ──


def _packs_registry() -> Any:
    """The runtime registry a pack must fit into (Wave 1 catalog today)."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as distribution_version

    from nexus_ai_agent.creative.packs.registry import PackRegistry
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    try:
        current = distribution_version("nexus-ai-agent")
    except PackageNotFoundError:  # pragma: no cover - uninstalled source checkout
        current = None
    return PackRegistry(build_wave1_registry(), current_version=current)


@packs_app.command("list")
def packs_list(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List the capability packs that ship with this runtime."""
    from nexus_ai_agent.creative.packs.manifest import PackManifestError

    registry = _packs_registry()
    try:
        packs = registry.register_builtin()
    except PackManifestError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "package_id": pack.package_id,
                        "version": pack.manifest.version,
                        "display_name": pack.manifest.display_name,
                        "capabilities": list(pack.manifest.capabilities),
                        "pending_capabilities": list(pack.pending_capabilities),
                        "signature_state": pack.report.signature_state,
                        "external_binaries": list(pack.manifest.external_binaries),
                        "active": pack.active,
                        "source": pack.source,
                    }
                    for pack in packs
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    typer.echo(f"{len(packs)} capability pack(s) registered")
    for pack in packs:
        pending = (
            f" · pending={len(pack.pending_capabilities)}" if pack.pending_capabilities else ""
        )
        binaries = (
            " · binaries=" + ",".join(pack.manifest.external_binaries)
            if pack.manifest.external_binaries
            else ""
        )
        typer.echo(f"  • {pack.package_id} {pack.manifest.version} — {pack.manifest.display_name}")
        typer.echo(
            f"    capabilities={len(pack.manifest.capabilities)}{pending}"
            f" · signature={pack.report.signature_state}{binaries}"
        )


@packs_app.command("verify")
def packs_verify(
    path: Annotated[Path, typer.Argument(help="Path to a pack.manifest.json file.")],
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify a manifest: schema, policy, and the runtime capability allow-list."""
    from nexus_ai_agent.creative.packs.manifest import PackManifestError
    from nexus_ai_agent.creative.packs.verify import verify_manifest_file

    registry = _packs_registry()
    try:
        report = verify_manifest_file(
            path,
            known_operations=registry.runtime_registry.list_operations(),
            current_version=registry.current_version,
            anchor="external",
        )
    except PackManifestError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "package_id": report.package_id,
                    "version": report.package_version,
                    "ok": report.ok,
                    "capabilities": list(report.capabilities),
                    "pending_capabilities": list(report.pending_capabilities),
                    "signature_state": report.signature_state,
                    "external_binaries": list(report.external_binaries),
                    "issues": [
                        {"code": i.code, "severity": i.severity, "message": i.message}
                        for i in report.issues
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(report.summary())
        for issue in report.issues:
            marker = "✗" if issue.severity == "error" else "!"
            typer.echo(f"  {marker} {issue.code}: {issue.message}")

    if not report.ok:
        raise typer.Exit(code=1)


checkpoints_app.add_typer(golden_app, name="golden")
app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(metrics_app, name="metrics")
app.add_typer(maintenance_app, name="maintenance")
app.add_typer(jobs_app, name="jobs")
app.add_typer(packs_app, name="packs")


# ── D1: nexus jobs resume — operator drain of the durable in-process queue ──


@jobs_app.command("resume")
def jobs_resume(
    timeout: float = typer.Option(
        120.0,
        "--timeout",
        min=1.0,
        help="Max seconds to wait for requeued jobs to reach a terminal state.",
    ),
) -> None:
    """Requeue leftover pending jobs and drain them in this process.

    Only ``pending`` rows are claimed; ``processing`` rows belong to the
    live owner process (usually the bot) and are never duplicated here.
    Handlers are the standard application ones, so resumed jobs run exactly
    as they would inside the bot.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.application.ports.job_queue import JobStatus
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.worker import default_job_handlers, job_queue_db_path

    async def _run() -> None:
        queue = InProcessJobQueue(job_queue_db_path(get_settings().db_path))
        for job_type, handler in default_job_handlers().items():
            queue.register_handler(job_type, handler)
        job_ids = await queue.resume_pending_jobs()
        if not job_ids:
            typer.echo("No pending jobs to resume.")
            return
        typer.echo(f"Requeued {len(job_ids)} job(s): {', '.join(job_ids)}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        remaining = set(job_ids)
        while remaining:
            for job_id in sorted(remaining):
                status = await queue.get_status(job_id)
                if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                    marker = "✅" if status is JobStatus.COMPLETED else "❌"
                    typer.echo(f"{marker} {job_id}: {status.value}")
                    remaining.discard(job_id)
            if remaining and loop.time() >= deadline:
                for job_id in sorted(remaining):
                    typer.echo(f"⏳ {job_id}: still unfinished after {timeout:g}s")
                raise typer.Exit(1)
            if remaining:
                await asyncio.sleep(0.2)

    asyncio.run(_run())


# ── v3.9.0: nexus maintenance — stateless scheduled jobs (Phase 5) ─────


@maintenance_app.command()
def backup(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be dumped and uploaded without doing it.",
    ),
) -> None:
    """Dump the database and upload it to the R2 blob tier (stateless)."""
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.maintenance.backup import create_backup
    from nexus_ai_agent.storage.providers.base import ProviderUnavailable

    settings = get_settings()
    try:
        result = create_backup(settings=settings, dry_run=dry_run)
    except (ProviderUnavailable, RuntimeError) as e:
        typer.echo(f"❌ backup failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"source: {result['source']}")
    typer.echo(f"key:    {result['key']}")
    if result["dry_run"]:
        typer.echo("dry-run: nothing dumped or uploaded")
    else:
        typer.echo(f"size:   {result['size_bytes']} bytes")
        typer.echo("✅ uploaded to R2")


@maintenance_app.command()
def housekeeping(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what would be removed/deleted without touching anything.",
    ),
    temp_max_age_hours: int = typer.Option(
        48,
        "--temp-max-age-hours",
        help="Creative temp files older than this are removed.",
    ),
    backup_retention_days: int = typer.Option(
        30,
        "--backup-retention-days",
        help="R2 database backups older than this are deleted.",
    ),
) -> None:
    """Remove stale creative temp files and prune old R2 backups (idempotent)."""
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.maintenance.housekeeping import run_housekeeping

    result = run_housekeeping(
        settings=get_settings(),
        dry_run=dry_run,
        temp_max_age_hours=temp_max_age_hours,
        backup_retention_days=backup_retention_days,
    )
    typer.echo(f"temp files removed: {len(result['temp_files_removed'])}")
    for path in result["temp_files_removed"]:
        typer.echo(f"  - {path}")
    if result["backups_deleted"]:
        typer.echo(f"R2 backups deleted: {len(result['backups_deleted'])}")
        for key in result["backups_deleted"]:
            typer.echo(f"  - {key}")
    else:
        typer.echo("R2 backups deleted: 0")
    if result["r2_skipped_reason"]:
        typer.echo(f"note: {result['r2_skipped_reason']}")
    if result["dry_run"]:
        typer.echo("dry-run: nothing was changed")


@app.command()
def run_bot(
    mode: str | None = typer.Option(
        None,
        help=(
            "Run mode: polling or webhook. Overrides NEXUS_RUN_MODE; "
            "defaults to polling when neither is set."
        ),
    ),
) -> None:
    """Start the NEXUS AI Telegram bot."""
    from nexus_ai_agent.bot.app import build_application
    from nexus_ai_agent.bot.webhook import resolve_run_mode, run_webhook
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.memory.long_term import LongTermMemory
    from nexus_ai_agent.observability.logging import configure_logging
    from nexus_ai_agent.orchestration.graph import compile_graph
    from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
    from nexus_ai_agent.storage.migrations import ensure_startup_schema
    from nexus_ai_agent.tools.files import (
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from nexus_ai_agent.tools.registry import ToolRegistry

    try:
        run_mode = resolve_run_mode(mode)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    settings = get_settings()
    configure_logging(settings.log_level)

    # Bring the schema up (Alembic-first, create_all fallback for legacy files)
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    schema = ensure_startup_schema()
    typer.echo(f"✓ Schema ready ({schema['backend']}/{schema['source']})")

    # Initialize LLM — v3.7.0 multi-provider routing chain:
    #   litellm chain (Ollama → Groq → Gemini → OpenRouter:free, wrapped in
    #   FallbackProvider → FakeLLM) with legacy local-GGUF/FakeLLM as the
    #   fallback path when routing is disabled/unavailable.
    llm, llm_label = build_llm_provider(settings)
    typer.echo(f"✓ LLM engine: {llm_label}")

    # Tools
    workspace = getattr(settings, "workspace_root", ".")
    os.environ["NEXUS_WORKSPACE_ROOT"] = str(workspace)
    registry = ToolRegistry(
        enable_shell=settings.enable_shell,
        workspace_root=workspace,
    )
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ListDirTool())

    if settings.enable_shell:
        from nexus_ai_agent.tools.system_shell import ShellTool

        registry.register(ShellTool(enable_shell=True))
        typer.echo("⚠  Shell tool ENABLED")

    # Memory + checkpointer
    long_term = LongTermMemory(settings.vector_path, llm)
    checkpointer = get_checkpointer(settings.checkpoint_path)

    # Graph
    graph = compile_graph(llm, checkpointer, long_term, registry)

    # Bot
    application = build_application(settings, graph)

    if run_mode == "webhook":
        typer.echo("✓ Starting bot in webhook mode…")
        run_webhook(application)
    else:
        typer.echo("✓ Starting bot in polling mode…")
        application.run_polling()


@app.command()
def smoke(
    input: str = typer.Argument(
        "Hello, what can you do?",
        help="Message to send through the graph",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full state"),
) -> None:
    """Run the full AI graph without Telegram (for testing)."""
    from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
    from nexus_ai_agent.memory.long_term import LongTermMemory
    from nexus_ai_agent.orchestration.graph import compile_graph
    from nexus_ai_agent.orchestration.state import NexusState
    from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
    from nexus_ai_agent.tools.files import (
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from nexus_ai_agent.tools.registry import ToolRegistry

    llm = FakeLLMProvider()
    os.environ["NEXUS_WORKSPACE_ROOT"] = "."
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ListDirTool())

    long_term = LongTermMemory(":memory:", llm)

    async def _run() -> dict:
        checkpointer = get_checkpointer(":memory:")
        graph = compile_graph(llm, checkpointer, long_term, registry)
        initial_state: NexusState = {
            "thread_id": "smoke-test",
            "chat_id": 0,
            "user_id": 0,
            "correlation_id": str(uuid4()),
            "messages": [{"role": "user", "content": input}],
            "intent": "unknown",
            "active_persona": "gemma",
            "current_task": None,
            "tool_results": [],
            "memory_context": "",
            "response": "",
            "error": None,
            "turn_count": 0,
            "moderation_passed": True,
        }
        return await graph.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": "smoke-test"}},
        )

    result = asyncio.run(_run())

    typer.echo("\n" + "─" * 40)
    typer.echo(f"Intent:   {result.get('intent', '?')}")
    typer.echo(f"Persona:  {result.get('active_persona', '?')}")
    typer.echo(f"Turns:    {result.get('turn_count', 0)}")
    typer.echo(f"Response: {result.get('response', '')}")

    if verbose:
        typer.echo("\n── Full state ──")
        for k, v in result.items():
            if k != "messages":
                typer.echo(f"  {k}: {v}")

    typer.echo("─" * 40)


if __name__ == "__main__":
    app()
