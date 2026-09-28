"""Gemini LLM Provider — implements LLMProvider for Google Gemini 2.0 Flash.

This provider bridges the Gemini REST route with the abstract
:class:`~nexus_ai_agent.llm.provider.LLMProvider` interface, so Gemini can be
used as a first-class provider in the LangGraph orchestration pipeline and by the
Store agents.

W2 (Global LLM Gateway)
-----------------------
``generate()`` no longer routes through :meth:`GeminiEngine.ask`, which returns a
*rendered Persian string* for both answers and failures. That shape forced every
downstream consumer to guess whether a reply was an answer or an error — the root
cause of the substring scanning this wave removes. The provider now executes an
:class:`~nexus_ai_agent.llm.gateway.contract.LLMRequest` on the engine's gateway
and therefore:

* returns model text on success, and
* raises a typed :class:`~nexus_ai_agent.llm.errors.LLMError` on failure.

Rendering that failure into a human message is the *surface's* job
(``agents/store/base_agent.py``, the bot handlers) and it happens there, once,
from ``kind``/``status_code``.

The system prompt is now passed as a real ``systemInstruction`` instead of being
smuggled into the user turn as ``[System: …]`` text — the model sees the same
intent, in the channel the API defines for it.

``embed()`` is unchanged and deliberately local: Gemini's free tier offers no
embedding endpoint here, so this is a deterministic 384-dim hash vector, kept
byte-for-byte compatible with ``LiteLLMRoutingProvider.embed`` so vectors already
stored in the database remain comparable. It makes no provider call, so there is
nothing for the gateway to govern — and it is documented as an approximation, not
presented as a real embedding.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    LLMOperation,
    LLMRequest,
    Message,
)
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class GeminiProvider(LLMProvider):
    """``LLMProvider`` backed by Google Gemini, executed through the gateway.

    Delegates transport, retry, quota, concurrency and observability to the
    authority the underlying :class:`GeminiEngine` resolved; keeps conversation
    memory and multi-modal features on the engine, where they belong.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        max_rpm: int = 15,
        max_daily: int = 1500,
        max_history: int = 20,
        gateway: Any | None = None,
    ) -> None:
        self._engine = GeminiEngine(
            api_key=api_key,
            model=model,
            max_rpm=max_rpm,
            max_daily=max_daily,
            max_history=max_history,
            gateway=gateway,
        )
        self._model = model

    @property
    def engine(self) -> GeminiEngine:
        """Access the underlying GeminiEngine for advanced features (vision, code, etc.)."""
        return self._engine

    @property
    def gateway(self) -> Any:
        """The authority every generation from this provider goes through."""

        return self._engine.gateway

    async def generate(self, prompt: str, system: str = "") -> str:
        """Generate a response. Returns text, or raises a typed ``LLMError``."""

        request = LLMRequest(
            caller=Caller(category=CallerCategory.AGENT, name="llm.gemini_provider"),
            purpose="agent",
            operation=LLMOperation.CHAT,
            messages=(Message(role="user", content=prompt),),
            system=system or None,
            provider="gemini",
            model=self._model,
            # The agent asked for Gemini. Silently answering from another
            # provider would be a hidden fallback (LAW 8).
            allow_fallback=False,
        )
        response = await self._engine.gateway.execute(request)
        return response.text

    async def embed(self, text: str) -> list[float]:
        """Return a deterministic pseudo-embedding for *text*.

        Local by design and documented as an approximation: Gemini's free tier
        offers no embedding endpoint here, so this is a hash-based 384-dim vector
        sufficient for cosine-similarity search at small scale, and byte-for-byte
        identical to ``LiteLLMRoutingProvider.embed`` for the same input. Swap in
        a real embedding model (``LocalLlamaCppProvider`` or an embedding API)
        when retrieval quality demands it.
        """

        import hashlib
        import random

        vec_dim = 384
        h = hashlib.sha512(text.encode()).digest()
        # Expand 64 bytes of hash into 384 floats via repeated hashing
        floats: list[float] = []
        seed = int.from_bytes(h[:8], "little")

        rng = random.Random(seed)
        for _ in range(vec_dim):
            floats.append(rng.uniform(-0.1, 0.1))
        return floats
