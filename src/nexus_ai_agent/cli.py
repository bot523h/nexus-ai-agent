from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import typer

from nexus_ai_agent.llm.provider import LLMProvider

app = typer.Typer(help="NEXUS AI Agent CLI")
checkpoints_app = typer.Typer(help="Inspect checkpoint lifecycle state")
golden_app = typer.Typer(help="Schema golden management (human-triggered only)")
metrics_app = typer.Typer(help="Observability snapshots")


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
    from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore
    from nexus_ai_agent.storage.checkpoint_reconciler import lifecycle_db_path

    settings = get_settings()
    access_token = nexus_access_context.set("admin")
    adapter, _ = _open_checkpoint_backend()
    # Read-only by contract: inspect never creates or mutates the lifecycle
    # index.  A missing index file simply means "no metadata yet".
    lifecycle_path = lifecycle_db_path(settings.checkpoint_path)
    lifecycle = (
        SQLiteCheckpointLifecycleStore(lifecycle_path)
        if lifecycle_path != ":memory:" and Path(lifecycle_path).exists()
        else None
    )
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
    from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore
    from nexus_ai_agent.storage.checkpoint_reconciler import (
        CheckpointReconciler,
        lifecycle_db_path,
        render_report,
    )

    settings = get_settings()
    adapter, default_golden = _open_checkpoint_backend()
    lifecycle = SQLiteCheckpointLifecycleStore(lifecycle_db_path(settings.checkpoint_path))
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
            CheckpointReadAdapter,
            FINGERPRINT_ALGORITHM,
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


checkpoints_app.add_typer(golden_app, name="golden")
app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(metrics_app, name="metrics")


@app.command()
def run_bot(
    mode: str = typer.Option("polling", help="Run mode: polling or webhook"),
) -> None:
    """Start the NEXUS AI Telegram bot."""
    from nexus_ai_agent.bot.app import build_application
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

    settings = get_settings()
    configure_logging(settings.log_level)

    # Bring the schema up (Alembic-first, create_all fallback for legacy files)
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    schema = ensure_startup_schema()
    typer.echo(f"✓ Schema ready ({schema['backend']}/{schema['source']})")

    # Initialize LLM
    model_path = Path(settings.model_path)
    if model_path.exists():
        from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider

        typer.echo(f"✓ Loading model: {settings.model_path}")
        llm: LLMProvider = LocalLlamaCppProvider(
            settings.model_path,
            n_ctx=getattr(settings, "n_ctx", 2048),
            n_gpu_layers=getattr(settings, "n_gpu_layers", 0),
        )
    else:
        from nexus_ai_agent.llm.fake_llm import FakeLLMProvider

        typer.echo(
            "⚠  Model not found — using FakeLLM. Set NEXUS_MODEL_PATH to a valid .gguf file."
        )
        llm = FakeLLMProvider()

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

    if mode == "polling":
        typer.echo("✓ Starting bot in polling mode…")
        application.run_polling()
    elif mode == "webhook":
        typer.echo("Webhook mode not yet configured.")
        raise typer.Exit(code=1)
    else:
        typer.echo(f"Unknown mode: {mode}")
        raise typer.Exit(code=1)


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
