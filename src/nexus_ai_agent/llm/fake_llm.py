from __future__ import annotations

import hashlib
import random

from nexus_ai_agent.llm.provider import LLMProvider


class FakeLLMProvider(LLMProvider):
    """Offline/test provider whose synthetic embeddings make no semantic promise."""

    async def generate(self, prompt: str, system: str = "", *, user_id: int | None = None) -> str:
        _ = system
        _ = user_id  # no quota seam in the offline provider
        return f"[FAKE] Response to: {prompt[:60]}"

    async def embed(self, text: str) -> list[float]:
        """Return a stable 384-float pseudo-vector, not a semantic embedding.

        The digest avoids Python's process-salted ``hash()`` so persisted vectors
        for the same exact text are reproducible after restart in the same
        Python/runtime platform. Different texts still receive unrelated
        pseudo-random vectors; similarity between paraphrases or related
        meanings is explicitly not guaranteed. Cross-version/platform byte
        compatibility of the PRNG or serialized float blobs is not promised.
        """
        digest = hashlib.sha512(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "little")
        rng = random.Random(seed)
        return [rng.uniform(-0.1, 0.1) for _ in range(384)]
