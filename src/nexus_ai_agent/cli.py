from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import typer

from nexus_ai_agent.bot.app import build_application
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.observability.logging import configure_logging
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.storage.db import create_all_tables
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer

app = typer.Typer()


@app.command()
def migrate() -> None:
    "Initialize database schema"
    asyncio.run(create_all_tables())
    print("Database initialized")


@app.command()
def run_bot(mode: str = "polling") -> None:
    "Start the NEXUS AI Telegram bot"
    settings = get_settings()
    configure_logging(settings.log_level)

    # Initialize LLM
    if Path(settings.model_path).exists():
        llm = LocalLlamaCppProvider(settings.model_path)
    else:
        print("WARNING: Model not found, using FakeLLM")
        llm = FakeLLMProvider()

    # Initialize memory (created to ensure DB exists; wiring happens in graph nodes later)
    _long_term = LongTermMemory(settings.vector_path, llm)
    _ = _long_term

    # Initialize checkpointer
    checkpointer = get_checkpointer(settings.checkpoint_path)

    # Build graph
    graph = compile_graph(llm, checkpointer)

    # Run migrations
    asyncio.run(create_all_tables())

    # Build and run bot
    application = build_application(settings, graph)

    if mode == "polling":
        application.run_polling()
    else:
        raise ValueError(f"Unsupported mode: {mode}")


@app.command()
def smoke(input: str = "Hello, what can you do?") -> None:
    "Run orchestration without Telegram"
    settings = get_settings()
    _ = settings
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    graph = compile_graph(llm, checkpointer)

    state: NexusState = {
        "thread_id": "smoke-test",
        "chat_id": 0,
        "user_id": 0,
        "correlation_id": str(uuid4()),
        "messages": [{"role": "user", "content": input}],
        "intent": "unknown",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
    }

    result = asyncio.run(
        graph.ainvoke(state, config={"configurable": {"thread_id": "smoke-test"}})
    )
    print(f"\nResponse: {result['response']}")
    print(f"Intent: {result['intent']}")


if __name__ == "__main__":
    app()

