from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import typer

from nexus_ai_agent.llm.provider import LLMProvider

app = typer.Typer(help="NEXUS AI Agent CLI")


@app.command()
def migrate(
    db_path: str | None = typer.Option(
        None,
        "--db-path",
        help=(
            "[deprecated] Force a legacy SQLite path. Prefer NEXUS_DB_PATH, "
            "which `nexus migrate` now honours via Alembic."
        ),
    ),
) -> None:
    """Apply database migrations (alembic upgrade head).

    Backs both backends automatically: NEXUS_DATABASE_URL (PostgreSQL/Neon)
    wins; otherwise the configured SQLite file is migrated.  A pre-Alembic
    SQLite file is adopted automatically (D6); a pre-Alembic *PostgreSQL*
    database fails fast and points at `nexus adopt-pg` (D10).
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


@app.command(name="adopt-pg")
def adopt_pg(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Inspect and report only; change nothing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Adopt a legacy PostgreSQL database into Alembic management (D10).

    Use this when NEXUS_DATABASE_URL points at a database that already holds
    NEXUS tables but has no `alembic_version` — typically one created by the
    pre-D7 create_all stopgap, or restored from an older dump.  Existing rows
    are preserved: missing tables are created and the database is stamped at
    head, so the initial revision is never replayed over existing tables.
    """
    from nexus_ai_agent.storage.adopt_pg import (
        ACTION_ADOPT,
        ACTION_NONE,
        ACTION_UPGRADE,
        PgAdoptionError,
        adopt_postgres,
        redact_url,
    )

    try:
        preview = adopt_postgres(dry_run=True)
    except PgAdoptionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Database : {redact_url(preview.url)}")
    typer.echo(f"State    : {preview.state.value}")
    typer.echo(f"Tables   : {len(preview.tables_before)} present")
    if preview.extra:
        typer.echo(f"Extra    : {len(preview.extra)} table(s) not model-defined (kept)")
    typer.echo(f"Planned  : {preview.action}")

    if preview.action == ACTION_NONE:
        typer.echo("✓ Already Alembic-managed — nothing to do.")
        return
    if dry_run:
        typer.echo("✓ Dry run — nothing was changed. Re-run without --dry-run to apply.")
        return

    if preview.action == ACTION_ADOPT:
        typer.echo(
            "This will CREATE any missing table and STAMP the database at head. "
            "No table is dropped and no row is deleted."
        )
    elif preview.action == ACTION_UPGRADE:
        typer.echo("The database is empty; this will run `alembic upgrade head`.")

    if not yes and not typer.confirm("Proceed?"):
        typer.echo("Aborted — nothing was changed.")
        raise typer.Exit(code=1)

    try:
        result = adopt_postgres(dry_run=False)
    except PgAdoptionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ Adopted ({result.action}); stamped at {result.stamped_revision}")


continuum_app = typer.Typer(help="Cross-turn project state (.nexus/continuum.json)")
app.add_typer(continuum_app, name="continuum")


@continuum_app.command("show")
def continuum_show(
    path: str | None = typer.Option(None, "--path", help="Snapshot path override."),
) -> None:
    """Print the committed continuum snapshot as JSON."""
    import json

    from nexus_ai_agent.continuum import ContinuumError, load

    try:
        snapshot = load(Path(path) if path else None)
    except ContinuumError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))


@continuum_app.command("verify")
def continuum_verify(
    path: str | None = typer.Option(None, "--path", help="Snapshot path override."),
    expected_tests: int | None = typer.Option(
        None,
        "--expected-tests",
        help="Also assert the collected test count matches the snapshot.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Also treat HEAD having moved past the snapshot as a problem.",
    ),
) -> None:
    """Check the snapshot against the working tree; exit 1 on any problem."""
    from nexus_ai_agent.continuum import verify

    report = verify(
        path=Path(path) if path else None,
        actual_test_count=expected_tests,
        strict=strict,
    )
    for key, value in report.checks.items():
        typer.echo(f"{key}: {value}")
    if report.ok:
        typer.echo("✓ Continuum snapshot is consistent with this checkout.")
        return
    for problem in report.problems:
        typer.echo(f"✗ {problem}", err=True)
    raise typer.Exit(code=1)


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
