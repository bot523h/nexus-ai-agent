"""Adapt CognitionPort → legacy LLMProvider-shaped surface for graph migration."""

from __future__ import annotations

import uuid
from typing import Protocol

from nexus_ai_agent.cognition.port import CognitionPort, CognitionRequest, TaskClass


class EmbeddingUnsupportedError(RuntimeError):
    """Embeddings are not part of the Gate B cognition surface."""


class _LLMProviderLike(Protocol):
    async def generate(self, prompt: str, system: str = "") -> str: ...
    async def embed(self, text: str) -> list[float]: ...


class CognitionLLMProvider:
    """Graph/legacy callers keep LLMProvider shape; authority is CognitionPort."""

    def __init__(self, cognition: CognitionPort) -> None:
        self._cognition = cognition

    async def generate(self, prompt: str, system: str = "") -> str:
        result = await self._cognition.propose(
            CognitionRequest(
                prompt=prompt,
                system=system,
                task_class=TaskClass.CHAT,
                correlation_id=str(uuid.uuid4()),
            )
        )
        return result.text

    async def embed(self, text: str) -> list[float]:
        _ = text
        raise EmbeddingUnsupportedError(
            "embed() is not supported on CognitionLLMProvider (Gate B); "
            "use a dedicated embedding provider when available"
        )
