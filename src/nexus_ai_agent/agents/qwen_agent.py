from __future__ import annotations

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.personality.engine import PersonalityEngine


class QwenAgent(BaseAgent):
    """Storytelling and creative writing."""

    def __init__(self, llm, state_path=None):
        super().__init__(llm)
        self._pe = PersonalityEngine("qwen", state_path)

    async def run(self, state: NexusState) -> NexusState:
        msgs = state.get("messages", [])
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        self._pe.update(last)
        system = self._pe.build_system_prompt(
            "You are a master storyteller. Weave vivid narratives with compelling characters. "
            + self._pe.style_hint(),
            state.get("memory_context", ""),
        )
        conv = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-10:])
        resp = await self._llm.generate(conv + "\nassistant:", system=system)
        return {**state, "response": resp, "active_persona": "qwen"}

