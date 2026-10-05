"""Single composition root for cognition runtime."""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.cognition.port import CognitionPort
from nexus_ai_agent.cognition.router import DeterministicRouter
from nexus_ai_agent.cognition.service import LocalCognition
from nexus_ai_agent.config.settings import Settings


def build_cognition(
    settings: Settings,
    *,
    gemini_engine: Any | None = None,
) -> CognitionPort:
    """Construct the one process-wide cognition authority."""
    gemini_available = gemini_engine is not None and bool(
        getattr(settings, "gemini_api_key", None)
    )

    router = DeterministicRouter(
        gemini_available=gemini_available,
        local_available=False,
        allow_cloud=True,
    )

    async def _gemini_generate(prompt: str, system: str) -> str:
        assert gemini_engine is not None
        result = await gemini_engine.chat(prompt, conv_id="cognition", user_id=0)
        if isinstance(result, dict):
            return str(result.get("text") or result.get("response") or result)
        return str(result)

    return LocalCognition(
        router,
        gemini_generate=_gemini_generate if gemini_available else None,
    )
