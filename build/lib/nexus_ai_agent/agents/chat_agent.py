from __future__ import annotations

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.orchestration.state import NexusState


class ChatAgent(BaseAgent):
    async def run(self, state: NexusState) -> NexusState:
        messages = state.get("messages", [])[-10:]
        memory_context = state.get("memory_context", "")
        prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        system = f"You are NEXUS, a helpful AI assistant.\nContext from memory: {memory_context}"
        state["response"] = await self.llm.generate(prompt=prompt, system=system)
        return state
