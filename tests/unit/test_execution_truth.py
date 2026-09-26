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


def _guarded_write_state() -> dict:
    state = _state()
    state["current_task"] = {
        "goal": "write a file",
        "steps": [
            {
                "id": 1,
                "action": "write_file",
                "tool": "write_file",
                "status": "pending",
                "inputs": {"path": "hello.txt", "content": "hi"},
            }
        ],
    }
    return state


@pytest.mark.asyncio
async def test_graph_executor_needs_confirmation_is_not_reported_as_executed(
    tmp_path,
) -> None:
    """A guarded tool without a confirmation policy is a PAUSE, not a run.

    Regression for the pre-fix lie: the step was marked failed, nothing
    executed, and the user still saw ``Tool write_file executed.``
    """
    from nexus_ai_agent.tools.files import WriteFileTool

    registry = ToolRegistry(workspace_root=tmp_path)
    registry.register(WriteFileTool())
    state = _guarded_write_state()

    result = await _executor_agent(state, tool_registry=registry)

    step = result["current_task"]["steps"][0]
    assert step["status"] == "pending"  # paused, not failed, not done
    assert result["tool_results"] == []  # no execution result may be recorded
    assert "NOT been executed" in result["response"]
    assert "executed." not in result["response"].replace("NOT been executed.", "")
    assert not (tmp_path / "hello.txt").exists()  # zero mutation


@pytest.mark.asyncio
async def test_graph_executor_failed_tool_never_claims_execution(tmp_path) -> None:
    """Even with empty output, a failed tool must not say 'executed'."""
    from nexus_ai_agent.tools.files import ReadFileTool

    registry = ToolRegistry(workspace_root=tmp_path)
    registry.register(ReadFileTool())
    state = _state()
    # A boundary-violating path makes the SAFE tool fail with a typed error.
    state["current_task"] = {
        "goal": "read outside",
        "steps": [
            {
                "id": 1,
                "action": "read_file",
                "tool": "read_file",
                "status": "pending",
                "inputs": {"path": "../escape.txt"},
            }
        ],
    }

    result = await _executor_agent(state, tool_registry=registry)

    assert result["current_task"]["steps"][0]["status"] == "failed"
    assert "executed." not in result["response"]
    assert result["tool_results"][0]["success"] is False
