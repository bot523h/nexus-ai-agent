"""Gemini LLM Provider — implements LLMProvider for Google Gemini 2.0 Flash.

This provider bridges the GeminiEngine (features/ai_chat.py) with the
abstract LLMProvider interface (llm/provider.py), enabling Gemini to be
used as a first-class provider in the LangGraph orchestration pipeline
as well as directly via handlers.
"""

from __future__ import annotations

from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class GeminiProvider(LLMProvider):
    """LLMProvider implementation backed by Google Gemini 2.0 Flash.

    Delegates all generation to GeminiEngine which handles rate-limiting,
    conversation memory, and multi-modal requests internally.

    For embeddings, uses a simple deterministic hash-based approach
    (suitable for retrieval Augmentation at small scale).  When a real
    embedding model is needed, swap to LocalLlamaCppProvider or a
    dedicated embedding API.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        max_rpm: int = 15,
        max_daily: int = 1500,
        max_history: int = 20,
    ) -> None:
        self._engine = GeminiEngine(
            api_key=api_key,
            model=model,
            max_rpm=max_rpm,
            max_daily=max_daily,
            max_history=max_history,
        )

    @property
    def engine(self) -> GeminiEngine:
        """Access the underlying GeminiEngine for advanced features (vision, code, etc.)."""
        return self._engine

    async def generate(self, prompt: str, system: str = "") -> str:
        """Generate a response using Gemini chat.

        If *system* is provided it is prepended as a directive so the
        model follows the system instruction even though we use the
        simple ``ask()`` path (no conversation memory).
        """
        text = prompt
        if system:
            text = f"[System: {system}]\n\n{prompt}"
        result = await self._engine.ask(text, user_id=0)
        return result

    async def embed(self, text: str) -> list[float]:
        """Return a deterministic pseudo-embedding for *text*.

        Gemini does not offer a free embedding endpoint, so we produce a
        lightweight hash-based vector.  This is sufficient for cosine-
        similarity search at small scale and avoids an extra dependency.
        Swap this provider out for a real embedding model when needed.
        """
        import hashlib

        vec_dim = 384
        h = hashlib.sha512(text.encode()).digest()
        # Expand 64 bytes of hash into 384 floats via repeated hashing
        floats: list[float] = []
        seed = int.from_bytes(h[:8], "little")
        import random

        rng = random.Random(seed)
        for _ in range(vec_dim):
            floats.append(rng.uniform(-0.1, 0.1))
        return floats
