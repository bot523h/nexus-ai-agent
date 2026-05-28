from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import typer

app = typer.Typer(help="NEXUS AI Agent CLI")


@app.command()
def migrate(
    db_path: str = typer.Option("data/app.sqlite", help="SQLite DB path"),
) -> None:
    """Initialize database schema."""
    from nexus_ai_agent.storage.db import create_all_tables

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(create_all_tables(db_path))
    typer.echo(f"✓ Database initialized at {db_path}")


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
    from nexus_ai_agent.storage.db import create_all_tables
    from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
    from nexus_ai_agent.tools.files import (
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from nexus_ai_agent.tools.registry import ToolRegistry

    settings = get_settings()
    configure_logging(settings.log_level)

    # Ensure data directory exists
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(create_all_tables(settings.db_path))

    # Initialize LLM
    model_path = Path(settings.model_path)
    if model_path.exists():
        from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider

        typer.echo(f"✓ Loading model: {settings.model_path}")
        llm = LocalLlamaCppProvider(
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

        registry.register(ShellTool())
        typer.echo("⚠  Shell tool ENABLED")

    # Memory + checkpointer
    long_term = LongTermMemory(settings.vector_path, llm)
    checkpointer = get_checkpointer(settings.checkpoint_path)

    # Graph
    graph = compile_graph(llm, checkpointer, long_term, registry)

    # Bot
    application = build_application(settings, graph, long_term)

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
