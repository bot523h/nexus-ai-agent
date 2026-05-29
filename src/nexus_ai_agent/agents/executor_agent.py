from __future__ import annotations

from typing import Any

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.orchestration.state import NexusState


class ExecutorAgent(BaseAgent):
    def __init__(self, llm: LLMProvider, registry: Any | None = None) -> None:
        # registry is intentionally typed as Any to avoid importing tools at import time.
        # It is expected to be a ToolRegistry from nexus_ai_agent.tools.registry.
        super().__init__(llm)
        self.registry = registry

    async def run(self, state: NexusState) -> NexusState:
        task = state.get("current_task") or {}
        steps = task.get("steps", [])
        step = next((s for s in steps if s.get("status") == "pending"), None)
        if not step:
            state["response"] = state.get("response") or "No pending steps."
            return state

        tool_name = step.get("tool")
        inputs = step.get("inputs", {}) if isinstance(step.get("inputs"), dict) else {}

        if tool_name and self.registry is not None:
            result = await self.registry.run(tool_name, inputs, policy={})
            if result.get("needs_confirmation"):
                state["response"] = (
                    f"Confirmation required to run tool '{tool_name}'. "
                    f"Reply 'confirm' to proceed.\nInputs: {inputs}"
                )
                return state

            if result.get("success"):
                step["status"] = "done"
            else:
                step["status"] = "failed"
                state["error"] = result.get("error") or "Tool failed"

            state["tool_results"] = state.get("tool_results", []) + [result]
            state["current_task"] = task
            state["response"] = result.get("output") or state.get("response") or "Done."
            return state

        # No tool specified or registry not wired yet.
        step["status"] = "done"
        state["current_task"] = task
        state["tool_results"] = state.get("tool_results", []) + [
            {"success": True, "output": "noop", "error": None}
        ]
        state["response"] = state.get("response") or "Step executed (noop)."
        return state
