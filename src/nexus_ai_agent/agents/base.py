"""Base persona agent — and the one place the context window is configured.

``BaseAgent`` owns two things: the LLM handle, and the *composition-aware* side
of the short-term window. The window policy itself lives in
:mod:`nexus_ai_agent.memory.short_term` and stays a pure leaf (it takes its
limits as arguments); reading ``settings`` is a composition concern, so it
happens here, once, for every persona.

That division is what makes ``max_short_term_messages`` and
``max_tokens_before_summary`` real configuration instead of decoration: they
were declared in :mod:`nexus_ai_agent.config.settings` and read by nothing while
each persona hardcoded its own slice (8, 10, 12). Now every persona renders
through the same policy, so a conversation shows the same history whichever
persona the router selected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.memory.short_term import ShortTermMemory
from nexus_ai_agent.orchestration.state import NexusState


class BaseAgent(ABC):
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    def short_term(self) -> ShortTermMemory:
        """The window policy, bounded by the configured count *and* token budget.

        ``get_settings`` is ``lru_cache``d, so this is a dict lookup rather than a
        re-read of the environment, and it is constructed per call so a test that
        overrides settings sees the override.
        """
        settings = get_settings()
        return ShortTermMemory(
            max_messages=settings.max_short_term_messages,
            max_tokens_before_summary=settings.max_tokens_before_summary,
        )

    def render_conversation(self, state: NexusState) -> str:
        """The visible conversation for one turn, windowed by policy not by taste."""
        return self.short_term().render(state.get("messages") or [])

    @abstractmethod
    async def run(self, state: NexusState) -> NexusState:
        raise NotImplementedError
