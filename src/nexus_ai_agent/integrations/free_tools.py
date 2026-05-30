from __future__ import annotations

import logging
from typing import Any

from duckduckgo_search import DDGS  # type: ignore

from nexus_ai_agent.core.http_client import get_http_client
from nexus_ai_agent.core.instrumentation import instrumented

logger = logging.getLogger(__name__)


class WeatherTool:
    """Weather information tool with resilient HTTP."""

    def __init__(self) -> None:
        self.client = get_http_client()

    @instrumented("tools.weather")
    async def get_weather(self, city: str) -> dict[str, Any] | None:
        """Get weather from wttr.in."""
        url = f"https://wttr.in/{city}?format=j1"
        try:
            return await self.client.get_json(url)
        except Exception as e:
            logger.error(f"Weather error: {e}")
        return None


class CurrencyTool:
    """Currency exchange rate tool with resilient HTTP."""

    def __init__(self) -> None:
        self.client = get_http_client()

    @instrumented("tools.currency")
    async def get_rate(self, base: str = "USD") -> float | None:
        """Get exchange rate from api.exchangerate-api.com."""
        url = f"https://api.exchangerate-api.com/v4/latest/{base.upper()}"
        try:
            data = await self.client.get_json(url)
            if data:
                # Example: get IRT (Toman) or IRR (Rial)
                rates = data.get("rates", {})
                return float(rates.get("IRR", 0))
        except Exception as e:
            logger.error(f"Currency error: {e}")
        return None


class NewsTool:
    """News retrieval tool with resilient HTTP and fallback."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self.ddgs = DDGS()
        self.client = get_http_client()

    @instrumented("tools.news")
    async def get_news(self, query: str) -> list[dict[str, str]]:
        """Get news from NewsAPI or DuckDuckGo."""
        if self.api_key:
            url = f"https://newsapi.org/v2/everything?q={query}&apiKey={self.api_key}"
            try:
                data = await self.client.get_json(url)
                if data:
                    articles = data.get("articles", [])
                    return [{"title": a["title"], "url": a["url"]} for a in articles[:5]]
            except Exception as e:
                logger.warning(f"NewsAPI error: {e}, falling back to DDG")

        # Fallback to DuckDuckGo
        try:
            results: Any = self.ddgs.news(query, max_results=5)
            return [{"title": r["title"], "url": r["url"]} for r in results]
        except Exception as e:
            logger.error(f"DDG News error: {e}")
            return []


class YouTubeSearchTool:
    """YouTube search tool using DuckDuckGo."""

    def __init__(self) -> None:
        self.ddgs = DDGS()

    @instrumented("tools.youtube")
    async def search(self, query: str) -> list[dict[str, str]]:
        """Search YouTube videos using DuckDuckGo."""
        try:
            # Note: ddgs.videos returns youtube results often
            results: Any = self.ddgs.videos(query, max_results=5)
            return [{"title": r["title"], "url": r["content"]} for r in results]
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
            return []
