from __future__ import annotations

from abc import ABC, abstractmethod

from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.orchestration.state import NexusState


class BaseAgent(ABC):
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    @abstractmethod
    async def run(self, state: NexusState) -> NexusState:
        raise NotImplementedError

