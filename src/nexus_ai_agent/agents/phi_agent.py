from __future__ import annotations

import json

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.personality.engine import PersonalityEngine

log = get_logger(__name__)


def moderation_allows(verdict: object) -> bool:
    """Return ``True`` only when ``verdict`` *positively asserts* that content is safe.

    The moderation gate fails closed. Anything that is not an explicit
    ``{"safe": true}`` — an unparseable answer, a missing key, a string,
    ``None`` — blocks the content. Silence is not consent: a verdict we cannot
    read must never be treated as permission.
    """
    return isinstance(verdict, dict) and verdict.get("safe") is True


class PhiAgent(BaseAgent):
    """Logic, moderation, structured analysis."""

    def __init__(self, llm: LLMProvider, state_path: str | None = None) -> None:
        super().__init__(llm)
        self._pe = PersonalityEngine("phi", state_path)

    async def run(self, state: NexusState) -> NexusState:
        msgs = state.get("messages", [])
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        self._pe.update(last)
        system = self._pe.build_system_prompt(
            "You analyze and reason logically. Structure answers clearly. " + self._pe.style_hint(),
            state.get("memory_context", ""),
        )
        conv = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-8:])
        resp = await self.llm.generate(conv + "\nassistant:", system=system)
        return {**state, "response": resp, "active_persona": "phi"}

    async def moderate(self, text: str) -> dict:
        """Classify ``text`` and return ``{"safe": bool, "reason": str}``.

        This gate **fails closed**. A verdict the runtime cannot parse — or one
        that is not a JSON object carrying a boolean ``safe`` — is not evidence
        of safety, so it is reported as unsafe with the reason attached instead
        of letting the content through. (The previous ``{"safe": True}``
        fallback meant a malformed model answer silently approved the message.)
        """
        system = 'Reply ONLY with JSON: {"safe": true, "reason": "ok"}'
        raw = await self.llm.generate(f"Is this content safe?\n{text}", system=system)
        try:
            verdict = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - moderation must never raise
            # Never echo ``raw``: it quotes user content.
            log.warning("moderation_verdict_unparseable", error_type=type(exc).__name__)
            return {"safe": False, "reason": "verdict_unparseable"}
        if not isinstance(verdict, dict) or not isinstance(verdict.get("safe"), bool):
            log.warning("moderation_verdict_malformed", verdict_type=type(verdict).__name__)
            return {"safe": False, "reason": "verdict_malformed"}
        return verdict
