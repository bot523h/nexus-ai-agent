from __future__ import annotations

from typing import TYPE_CHECKING

from nexus_ai_agent.config.settings import get_settings

if TYPE_CHECKING:
    from nexus_ai_agent.llm.gemini_provider import GeminiProvider


class StoreAgent:
    """Base class for all specialized agents in the Store.

    W1 (Law 8 — NO HIDDEN BYPASS): agents MUST be constructed with the
    runtime-owned LLM provider.  A legacy fallback exists but is
    *explicit opt-in* via ``allow_legacy_fallback=True`` so tests and
    non-runtime callers can still instantiate agents in isolation —
    production code paths go through AgentManager.get_active(user_id,
    provider=...) which always injects the canonical provider.
    """

    name: str
    emoji: str
    description: str
    system_prompt: str
    category: str

    def __init__(
        self,
        gemini_provider: GeminiProvider | None = None,
        *,
        allow_legacy_fallback: bool = False,
    ) -> None:
        if gemini_provider is None:
            if not allow_legacy_fallback:
                # Fail fast: in production runtime code a missing
                # provider means someone bypassed the canonical
                # construction path.
                raise RuntimeError(
                    "StoreAgent requires a runtime-owned GeminiProvider; "
                    "pass allow_legacy_fallback=True only for isolated "
                    "tests."
                )
            from nexus_ai_agent.llm.gemini_provider import GeminiProvider

            settings = get_settings()
            gemini_provider = GeminiProvider(api_key=settings.gemini_api_key or "")
        self.gemini = gemini_provider

    async def respond(
        self, user_id: int, message: str, history: list[dict[str, str]], context: str = ""
    ) -> str:
        """Generate a response using the agent's unique personality."""
        full_system_prompt = self.system_prompt
        if context:
            full_system_prompt += f"\n\nContext about user:\n{context}"

        # history should be list of {"role": "user/assistant", "content": "..."}
        response = await self.gemini.generate(
            prompt=message,
            system=full_system_prompt,
            # history=history  # If GeminiProvider supports history
        )
        return response
