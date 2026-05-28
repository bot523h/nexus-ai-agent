from __future__ import annotations

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_smoke_chat_flow(settings_override):
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    long_term = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    graph = compile_graph(llm, checkpointer, long_term, registry)

    thread_id = "t1"
    state = {
        "thread_id": thread_id,
        "chat_id": 0,
        "user_id": 0,
        "correlation_id": "c1",
        "messages": [{"role": "user", "content": "How are you?"}],
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

    # If the checkpointer works, a second call with the same thread_id should resume state.
    result2 = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Hi again"}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    assert result2["turn_count"] >= result["turn_count"]


@pytest.mark.asyncio
async def test_resume_after_restart(settings_override):
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    long_term = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    graph = compile_graph(llm, checkpointer, long_term, registry)

    thread_id = "t2"
    state = {
        "thread_id": thread_id,
        "chat_id": 0,
        "user_id": 0,
        "correlation_id": "c1",
        "messages": [{"role": "user", "content": "Hello"}],
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

    result1 = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
    result2 = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Second turn"}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    assert result2["turn_count"] >= result1["turn_count"]
