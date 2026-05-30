import logging
from typing import Any

import httpx
from duckduckgo_search import DDGS  # type: ignore

logger = logging.getLogger(__name__)


class WeatherTool:
    async def get_weather(self, city: str) -> dict[str, Any] | None:
        """Get weather from wttr.in."""
        url = f"https://wttr.in/{city}?format=j1"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.error(f"Weather error: {e}")
        return None


class CurrencyTool:
    async def get_rate(self, base: str = "USD") -> float | None:
        """Get exchange rate from api.exchangerate-api.com."""
        url = f"https://api.exchangerate-api.com/v4/latest/{base.upper()}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    # Example: get IRT (Toman) or IRR (Rial)
                    rates = data.get("rates", {})
                    return float(rates.get("IRR", 0))
            except Exception as e:
                logger.error(f"Currency error: {e}")
        return None


class NewsTool:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self.ddgs = DDGS()

    async def get_news(self, query: str) -> list[dict[str, str]]:
        """Get news from NewsAPI or DuckDuckGo."""
        if self.api_key:
            url = f"https://newsapi.org/v2/everything?q={query}&apiKey={self.api_key}"
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        articles = resp.json().get("articles", [])
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
    def __init__(self) -> None:
        self.ddgs = DDGS()

    async def search(self, query: str) -> list[dict[str, str]]:
        """Search YouTube videos using DuckDuckGo."""
        try:
            # Note: ddgs.videos returns youtube results often
            results: Any = self.ddgs.videos(query, max_results=5)
            return [{"title": r["title"], "url": r["content"]} for r in results]
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
            return []
