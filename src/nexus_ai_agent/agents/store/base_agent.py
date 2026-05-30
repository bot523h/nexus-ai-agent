from __future__ import annotations

from typing import Any, Optional

from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.config.settings import get_settings


class StoreAgent:
    """Base class for all specialized agents in the Store."""

    name: str
    emoji: str
    description: str
    system_prompt: str
    category: str

    def __init__(self, gemini_provider: Optional[GeminiProvider] = None) -> None:
        settings = get_settings()
        self.gemini = gemini_provider or GeminiProvider(api_key=settings.gemini_api_key or "")

    async def respond(self, user_id: int, message: str, history: list[dict[str, str]], context: str = "") -> str:
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
