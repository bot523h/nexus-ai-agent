from __future__ import annotations

from nexus_ai_agent.llm.provider import LLMProvider


class FakeLLMProvider(LLMProvider):
    async def generate(self, prompt: str, system: str = "") -> str:
        _ = system  # unused in fake implementation
        snippet = prompt[:50].replace("\n", " ")
        return f"I am a fake response for: {snippet}"

    async def embed(self, text: str) -> list[float]:
        _ = text
        # all-MiniLM-L6-v2 has 384 dims
        return [0.0] * 384

