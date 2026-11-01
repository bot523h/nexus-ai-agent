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
            # Explicit, one-shot confirmation attached by a trusted caller.
            confirmed = step.pop("confirmed", None) is True
            policy = {"confirmed": True} if confirmed else {}
            result = await self.registry.run(tool_name, inputs, policy=policy)
            if result.get("needs_confirmation"):
                # The registry refused a GUARDED tool pending confirmation: the
                # tool did not run, so the step is a typed refusal — not a
                # success and not an empty-output "executed" claim.
                error_code = "confirmation_required"
                message = (
                    f"Confirmation required before tool '{tool_name}' can run. "
                    f"The tool did not run. Inputs: {inputs}"
                )
                step["status"] = "failed"
                state["current_task"] = task
                state["tool_results"] = state.get("tool_results", []) + [
                    {
                        "success": False,
                        "error_code": error_code,
                        "output": message,
                        "error": message,
                    }
                ]
                state["error"] = f"{error_code}: {message}"
                state["response"] = message
                return state

            is_success = result.get("success") is True
            if is_success:
                step["status"] = "done"
                state["tool_results"] = state.get("tool_results", []) + [result]
                state["response"] = result.get("output") or "Done."
                state["current_task"] = task
                return state

            step["status"] = "failed"
            failure = result.get("error") or result.get("output") or "Tool did not complete."
            state["error"] = f"tool_failure: {failure}"
            state["tool_results"] = state.get("tool_results", []) + [
                {**result, "success": False, "error_code": "tool_failure"}
            ]
            # Never retain a prior response or emit "Done." for a failed tool.
            state["response"] = failure
            state["current_task"] = task
            return state

        # No tool specified or registry not wired is a refusal, not an
        # execution. Never turn an unsupported plan into fake success.
        error_code = "unsupported_operation"
        message = "No registered executable tool is available for this step."
        step["status"] = "failed"
        state["current_task"] = task
        state["tool_results"] = state.get("tool_results", []) + [
            {
                "success": False,
                "error_code": error_code,
                "output": message,
                "error": message,
            }
        ]
        state["error"] = f"{error_code}: {message}"
        state["response"] = message
        return state
