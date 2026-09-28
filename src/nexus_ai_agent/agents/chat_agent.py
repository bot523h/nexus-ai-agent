from __future__ import annotations

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState


class ChatAgent(BaseAgent):
    async def run(self, state: NexusState) -> NexusState:
        memory_context = state.get("memory_context", "")
        prompt = self.render_conversation(state)
        system = f"You are NEXUS, a helpful AI assistant.\nContext from memory: {memory_context}"
        state["response"] = await self.llm.generate(prompt=prompt, system=system)
        return state
