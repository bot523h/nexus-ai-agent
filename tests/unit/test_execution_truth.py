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
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.graph import _executor_agent, compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
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


class _FailingTool(BaseTool):
    """A SAFE tool that fails with no output and no error text."""

    name = "failing_echo"
    description = "failing echo"
    risk_level = RiskLevel.SAFE

    async def execute(self, inputs: dict) -> dict:
        return {"success": False, "output": "", "error": None}


class _ModerationOKLLM(FakeLLMProvider):
    """A fake provider whose moderation verdict is always safely parseable."""

    async def generate(self, prompt: str, system: str = "", *, user_id: int | None = None) -> str:
        return '{"safe": true, "reason": "ok"}'


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
    tool = _GuardedEchoTool()
    registry = ToolRegistry()
    registry.register(tool)

    result = await ExecutorAgent(FakeLLMProvider(), registry=registry).run(_guarded_state())
    step = result["current_task"]["steps"][0]

    assert step["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "confirmation_required"
    assert result["error"].startswith("confirmation_required:")
    assert "executed" not in (result.get("response") or "").lower()
    assert tool.calls == []


@pytest.mark.asyncio
async def test_graph_executor_runs_guarded_tool_with_explicit_one_shot_confirmation() -> None:
    """A trusted step-level confirmation preserves the genuine success path."""
    tool = _GuardedEchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = _guarded_state()
    state["current_task"]["steps"][0]["confirmed"] = True

    result = await _executor_agent(state, tool_registry=registry)
    step = result["current_task"]["steps"][0]

    assert step["status"] == "done"
    assert "confirmed" not in step, "confirmation must be one-shot, never replayable"
    assert tool.calls == [{"path": "x.txt"}]
    assert result["tool_results"][0]["success"] is True
    assert result["response"] == "ran"
    assert result.get("error") in (None, "")


@pytest.mark.asyncio
async def test_executor_agent_runs_guarded_tool_with_explicit_confirmation() -> None:
    tool = _GuardedEchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = _guarded_state()
    state["current_task"]["steps"][0]["confirmed"] = True

    result = await ExecutorAgent(FakeLLMProvider(), registry=registry).run(state)
    step = result["current_task"]["steps"][0]

    assert step["status"] == "done"
    assert "confirmed" not in step
    assert tool.calls == [{"path": "x.txt"}]
    assert result["tool_results"][0]["success"] is True
    assert result["response"] == "ran"


@pytest.mark.asyncio
async def test_failed_tool_with_empty_output_never_reports_execution() -> None:
    """The generic failure branch must not emit the old false 'executed'claim."""
    registry = ToolRegistry()
    registry.register(_FailingTool())
    state = _state()
    state["current_task"] = {
        "goal": "fail",
        "steps": [{"id": 1, "action": "fail", "tool": "failing_echo", "status": "pending"}],
    }

    result = await _executor_agent(state, tool_registry=registry)
    step = result["current_task"]["steps"][0]

    assert step["status"] == "failed"
    assert result["tool_results"][0]["success"] is False
    assert result["tool_results"][0]["error_code"] == "tool_failure"
    assert "executed" not in (result.get("response") or "").lower()
    assert result["response"] != "Done."


@pytest.mark.asyncio
async def test_compiled_graph_refuses_a_real_guarded_tool_without_side_effect(
    settings_override,
) -> None:
    """A real ToolRegistry guard is honored through the full compiled graph."""
    tool = _GuardedEchoTool()
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    registry.register(tool)
    llm = _ModerationOKLLM()
    graph = compile_graph(
        llm,
        get_checkpointer(":memory:"),
        LongTermMemory(":memory:", llm),
        registry,
    )
    state = _guarded_state()
    state["messages"] = [{"role": "user", "content": "write a file"}]
    state["user_id"] = 42
    state["chat_id"] = 42
    state["memory_consent"] = False

    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "truth-graph"}})

    assert result["current_task"]["steps"][0]["status"] == "failed"
    assert result["tool_results"][-1]["error_code"] == "confirmation_required"
    assert result["tool_results"][-1]["success"] is False
    assert result["error"].startswith("confirmation_required:")
    assert "executed" not in (result.get("response") or "").lower()
    assert tool.calls == [], "guarded tool body ran without confirmation"
