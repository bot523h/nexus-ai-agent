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
