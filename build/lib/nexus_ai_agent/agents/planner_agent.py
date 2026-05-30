from __future__ import annotations

import json

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState


class PlannerAgent(BaseAgent):
    async def run(self, state: NexusState) -> NexusState:
        user_msg = state.get("messages", [])[-1]["content"] if state.get("messages") else ""

        # MVP: deterministic plan generation to avoid brittle JSON parsing. This can be upgraded
        # to a model-generated plan later without changing the state schema.
        plan = {
            "goal": user_msg,
            "steps": [
                {"id": 1, "action": user_msg, "tool": None, "status": "pending"},
            ],
        }

        state["current_task"] = plan
        state["response"] = (
            "Task plan:\n"
            + json.dumps(plan, indent=2, ensure_ascii=False)
            + "\n\nI will execute the first step next."
        )
        return state
