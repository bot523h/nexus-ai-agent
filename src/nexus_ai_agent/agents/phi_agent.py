from __future__ import annotations

import json

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.personality.engine import PersonalityEngine


class PhiAgent(BaseAgent):
    """Logic, moderation, structured analysis."""

    def __init__(self, llm, state_path=None):
        super().__init__(llm)
        self._pe = PersonalityEngine("phi", state_path)

    async def run(self, state: NexusState) -> NexusState:
        msgs = state.get("messages", [])
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        self._pe.update(last)
        system = self._pe.build_system_prompt(
            "You analyze and reason logically. Structure answers clearly. " + self._pe.style_hint(),
            state.get("memory_context", ""),
        )
        conv = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-8:])
        resp = await self._llm.generate(conv + "\nassistant:", system=system)
        return {**state, "response": resp, "active_persona": "phi"}

    async def moderate(self, text: str) -> dict:
        system = 'Reply ONLY with JSON: {"safe": true, "reason": "ok"}'
        raw = await self._llm.generate(f"Is this content safe?\n{text}", system=system)
        try:
            return json.loads(raw)
        except Exception:
            return {"safe": True, "reason": "parse_error"}

