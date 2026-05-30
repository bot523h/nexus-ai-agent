from __future__ import annotations

import random

from nexus_ai_agent.llm.provider import LLMProvider


class FakeLLMProvider(LLMProvider):
    async def generate(self, prompt: str, system: str = "") -> str:
        _ = system
        return f"[FAKE] Response to: {prompt[:60]}"

    async def embed(self, text: str) -> list[float]:
        # Small random values so sqlite-vec doesn't reject them
        rng = random.Random(hash(text) & 0xFFFFFFFF)
        return [rng.uniform(-0.1, 0.1) for _ in range(384)]
