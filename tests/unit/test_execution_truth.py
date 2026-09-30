"""Negative execution contract: unsupported plans must remain failures.

The graph executor must never report an execution that did not happen. This
file pins both halves of that contract:

* an unsupported plan (no tool, or no wired registry) is a typed refusal;
* a *guarded* tool that the registry refuses pending confirmation is also a
  typed refusal — the registry's ``needs_confirmation`` result carries no
  ``success`` key, and treating its absence as "ran, empty output" produced a
  false "Tool <name> executed." claim while the step was marked failed.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.agents.executor_agent import ExecutorAgent
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.orchestration.graph import _executor_agent
from nexus_ai_agent.tools.base import BaseTool, RiskLevel
from nexus_ai_agent.tools.registry import ToolRegistry


class _GuardedEchoTool(BaseTool):
    """A GUARDED tool: the registry refuses it until ``confirmed=True``."""

    name = "guarded_echo"
    description = "guarded echo (needs confirmation)"
    risk_level = RiskLevel.GUARDED

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, inputs: dict) -> dict:
        self.calls.append(inputs)
        return {"success": True, "output": "ran", "error": None}


class _SafeEchoTool(BaseTool):
    """A SAFE tool: the registry runs it without confirmation."""

    name = "safe_echo"
    description = "safe echo"
    risk_level = RiskLevel.SAFE

    async def execute(self, inputs: dict) -> dict:
        return {"success": True, "output": "ran", "error": None}


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


def _guarded_state() -> dict:
    state = _state()
    state["current_task"] = {
        "goal": "write a file",
        "steps": [
            {
                "id": 1,
                "action": "write",
                "tool": "guarded_echo",
                "inputs": {"path": "x.txt"},
                "status": "pending",
            }
        ],
    }
    return state


@pytest.mark.asyncio
async def test_graph_executor_never_claims_execution_of_a_guarded_tool() -> None:
    """A confirmation-gated tool is a typed refusal, not a silent "executed"."""
    tool = _GuardedEchoTool()
    registry = ToolRegistry()
    registry.register(tool)

    result = await _executor_agent(_guarded_state(), tool_registry=registry)
    step = result["current_task"]["steps"][0]

    assert step["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "confirmation_required"
    assert "executed" not in (result.get("response") or "").lower()
    # The guard was honoured: the tool body never ran.
    assert tool.calls == []


@pytest.mark.asyncio
async def test_graph_executor_still_runs_a_safe_tool() -> None:
    """No regression: a SAFE tool executes and its output is surfaced."""
    registry = ToolRegistry()
    registry.register(_SafeEchoTool())
    state = _state()
    state["current_task"] = {
        "goal": "echo",
        "steps": [{"id": 1, "action": "echo", "tool": "safe_echo", "status": "pending"}],
    }

    result = await _executor_agent(state, tool_registry=registry)
    step = result["current_task"]["steps"][0]

    assert step["status"] == "done"
    assert result["tool_results"][0]["success"] is True
    assert result["response"] == "ran"


@pytest.mark.asyncio
async def test_executor_agent_surfaces_confirmation_request_not_fake_success() -> None:
    """The ExecutorAgent wording must not claim a guarded tool executed."""
    registry = ToolRegistry()
    registry.register(_GuardedEchoTool())

    result = await ExecutorAgent(FakeLLMProvider(), registry=registry).run(_guarded_state())
    step = result["current_task"]["steps"][0]

    assert step["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "confirmation_required"
    assert "executed" not in (result.get("response") or "").lower()
