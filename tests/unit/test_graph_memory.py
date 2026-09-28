"""Unit tests for LangGraph cognitive memory persistence and tool execution."""

from __future__ import annotations

import uuid

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.registry import ToolRegistry


class DummyEchoTool(BaseTool):
    name: str = "echo_tool"
    description: str = "Echo input text"
    risk_level: RiskLevel = RiskLevel.SAFE

    async def execute(self, inputs: dict) -> dict:
        text = inputs.get("text", "")
        return {"success": True, "output": f"Echo: {text}"}


@pytest.mark.asyncio
async def test_long_term_memory_written_on_turn_end(settings_override) -> None:
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    long_term = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    graph = compile_graph(llm, checkpointer, long_term, registry)

    thread_id = f"mem-test-{uuid.uuid4().hex[:12]}"
    state = {
        "thread_id": thread_id,
        "chat_id": 12345,
        "user_id": 999,
        "correlation_id": "c_mem",
        "messages": [{"role": "user", "content": "My secret code is ALPHA-42"}],
        "intent": "chat",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }

    result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
    assert result["response"]

    # Verify that the turn was written to long_term_memory
    memories = await long_term.search(thread_id, "secret code", top_k=5)
    assert len(memories) >= 1
    assert "ALPHA-42" in memories[0]

    # Run second turn and verify memory_context is populated
    second_state = {
        "messages": [{"role": "user", "content": "What was my secret code?"}],
    }
    result2 = await graph.ainvoke(second_state, config={"configurable": {"thread_id": thread_id}})
    assert result2["turn_count"] >= 2


@pytest.mark.asyncio
async def test_executor_agent_runs_registered_tool(settings_override) -> None:
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    long_term = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    registry.register(DummyEchoTool())

    graph = compile_graph(llm, checkpointer, long_term, registry)

    thread_id = f"tool-test-{uuid.uuid4().hex[:12]}"
    task_plan = {
        "goal": "Echo something",
        "steps": [
            {
                "id": 101,
                "action": "Run echo tool",
                "tool": "echo_tool",
                "inputs": {"text": "NEXUS-V4"},
                "status": "pending",
            }
        ],
    }
    state = {
        "thread_id": thread_id,
        "chat_id": 1,
        "user_id": 1,
        "correlation_id": "c_tool",
        "messages": [{"role": "user", "content": "Run echo tool"}],
        "intent": "task",
        "active_persona": "phi",
        "current_task": task_plan,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }

    result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
    assert result["tool_results"]
    step_result = result["tool_results"][0]
    assert step_result["tool"] == "echo_tool"
    assert step_result["success"] is True
    assert "Echo: NEXUS-V4" in step_result["output"]


# --------------------------------------------------------------------------- #
# task-202: a failed durable-memory write must not be invisible
# --------------------------------------------------------------------------- #
# `_memory_writer` deliberately never aborts a conversation because of a memory
# write — that fail-safe is correct and these tests pin it.  What was wrong is
# that the failure was *silent*: a bare `except Exception: pass` meant a turn
# could be reported as a success while durable recall was quietly degrading,
# with no log line, no metric and no state marker to observe it from.


class _FailingLongTermMemory:
    """A LongTermMemory whose ``store`` always fails, like a dead embedder."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def store(self, thread_id: str, text: str, metadata: dict | None = None) -> None:
        self.calls += 1
        raise self._exc


class _RecordingLongTermMemory:
    """A LongTermMemory whose ``store`` succeeds and records what it got."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, str]] = []

    async def store(self, thread_id: str, text: str, metadata: dict | None = None) -> None:
        self.stored.append((thread_id, text))


def _turn_state(thread_id: str = "thread-202") -> dict:
    return {
        "thread_id": thread_id,
        "messages": [
            {"role": "user", "content": "My secret code is ALPHA-42"},
            {"role": "assistant", "content": "Noted."},
        ],
        "response": "Noted.",
        "error": None,
    }


@pytest.mark.asyncio
async def test_memory_write_failure_never_aborts_the_conversation() -> None:
    """The fail-safe itself: the turn still succeeds and no error is recorded."""
    from nexus_ai_agent.orchestration.graph import _memory_writer

    memory = _FailingLongTermMemory(RuntimeError("embedding backend unavailable"))
    state = await _memory_writer(_turn_state(), long_term_memory=memory)

    assert memory.calls == 1
    assert state["error"] is None, "a memory write failure must not fail the turn"
    assert state["response"] == "Noted."


@pytest.mark.asyncio
async def test_memory_write_failure_is_reported_exactly_once() -> None:
    """DEFECT: the failure produced no record at all. It must produce one warning."""
    from structlog.testing import capture_logs

    from nexus_ai_agent.orchestration.graph import _memory_writer

    memory = _FailingLongTermMemory(RuntimeError("embedding backend unavailable"))
    with capture_logs() as logs:
        await _memory_writer(_turn_state(), long_term_memory=memory)

    warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert len(warnings) == 1, (
        f"expected exactly one warning for the failed memory write, got {logs!r}"
    )
    assert warnings[0]["thread_id"] == "thread-202"
    assert warnings[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_memory_write_failure_record_leaks_no_conversation_content() -> None:
    """The record must name the failure, never the turn it was about."""
    from structlog.testing import capture_logs

    from nexus_ai_agent.orchestration.graph import _memory_writer

    memory = _FailingLongTermMemory(RuntimeError("embedding backend unavailable"))
    with capture_logs() as logs:
        await _memory_writer(_turn_state(), long_term_memory=memory)

    warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert warnings, "the failure must be recorded"
    blob = repr(warnings[0])
    assert "ALPHA-42" not in blob, "the warning must not carry message content"
    assert "My secret code" not in blob, "the warning must not carry the prompt"


@pytest.mark.asyncio
async def test_successful_memory_write_reports_nothing() -> None:
    """No false alarms: success must stay quiet, or the warning means nothing."""
    from structlog.testing import capture_logs

    from nexus_ai_agent.orchestration.graph import _memory_writer

    memory = _RecordingLongTermMemory()
    with capture_logs() as logs:
        await _memory_writer(_turn_state(), long_term_memory=memory)

    assert memory.stored, "the turn should have been stored"
    assert [entry for entry in logs if entry.get("log_level") == "warning"] == []
