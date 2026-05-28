from __future__ import annotations

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.personality.engine import PersonalityEngine


class GemmaAgent(BaseAgent):
    """Social, emotional support, companion."""

    def __init__(self, llm, state_path=None):
        super().__init__(llm)
        self._pe = PersonalityEngine("gemma", state_path)

    async def run(self, state: NexusState) -> NexusState:
        msgs = state.get("messages", [])
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        self._pe.update(last)
        e = self._pe.es
        note = ""
        if e.valence < 0.35:
            note = "User seems upset. Be extra empathetic."
        elif e.valence > 0.65:
            note = "User is positive. Match their energy."
        system = self._pe.build_system_prompt(
            "You are a warm, empathetic AI companion. Listen actively. "
            f"{note} {self._pe.style_hint()}",
            state.get("memory_context", ""),
        )
        conv = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-12:])
        resp = await self._llm.generate(conv + "\nassistant:", system=system)
        return {**state, "response": resp, "active_persona": "gemma"}

