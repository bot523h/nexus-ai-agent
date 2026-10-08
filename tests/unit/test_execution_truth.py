"""Negative execution contract: unsupported plans must remain failures."""

from __future__ import annotations

import pytest

from nexus_ai_agent.agents.executor_agent import ExecutorAgent
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.orchestration.graph import _executor_agent
from nexus_ai_agent.tools.registry import ToolRegistry


def _state() -> dict:
    return {
        "thread_id": "truth-test",
        "chat_id": 1,
        "user_id": 1,
        "correlation_id": "corr-truth",
        "messages": [],
        "intent": "task",
        "active_persona": "phi",
        "current_task": {
            "goal": "do unsupported work",
            "steps": [{"id": 1, "action": "unknown", "tool": None, "status": "pending"}],
        },
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


@pytest.mark.asyncio
async def test_graph_executor_refuses_missing_tool_instead_of_fake_success() -> None:
    result = await _executor_agent(_state(), tool_registry=ToolRegistry())
    step = result["current_task"]["steps"][0]
    assert step["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "unsupported_operation"
    assert result["error"].startswith("unsupported_operation:")


@pytest.mark.asyncio
async def test_executor_agent_refuses_when_registry_is_not_wired() -> None:
    result = await ExecutorAgent(FakeLLMProvider()).run(_state())
    assert result["current_task"]["steps"][0]["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "unsupported_operation"
