"""Adapt CognitionPort → legacy LLMProvider surface for graph migration."""

from __future__ import annotations

import uuid

from nexus_ai_agent.cognition.port import CognitionPort, CognitionRequest, TaskClass
from nexus_ai_agent.llm.provider import LLMProvider


class CognitionLLMProvider(LLMProvider):
    """Graph/legacy callers keep LLMProvider; authority is CognitionPort."""

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
        # Embedding remains deferred; empty vector keeps call sites from crashing.
        _ = text
        return []
