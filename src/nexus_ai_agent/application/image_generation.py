"""Composition for the bot and queue: settings stay outside the provider pack."""

from functools import lru_cache

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.creative.image_gen import ImageGenProvider
from nexus_ai_agent.creative.image_gen.gemini_adapter import GeminiAdapter
from nexus_ai_agent.creative.image_gen.pollinations_adapter import PollinationsAdapter


@lru_cache(maxsize=1)
def get_image_gen_provider() -> ImageGenProvider:
    """One provider/cache for this process; operator changes require a restart."""
    settings = get_settings()
    if settings.image_gen_provider == "gemini":
        return GeminiAdapter(
            api_key=settings.creative_gemini_api_key or "",
            paid_tier=settings.image_gen_paid_tier,
            model=settings.image_gen_model,
            estimated_cost_usd=settings.image_gen_estimated_cost_usd,
        )
    return PollinationsAdapter()
