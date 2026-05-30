import httpx
from typing import Optional, Dict, Any, List
import logging
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

class WeatherTool:
    async def get_weather(self, city: str) -> Optional[Dict[str, Any]]:
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
    async def get_rate(self, base: str = "USD") -> Optional[float]:
        """Get exchange rate from api.exchangerate-api.com."""
        url = f"https://api.exchangerate-api.com/v4/latest/{base.upper()}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    # Example: get IRT (Toman) or IRR (Rial)
                    return data.get("rates", {}).get("IRR")
            except Exception as e:
                logger.error(f"Currency error: {e}")
        return None

class NewsTool:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.ddgs = DDGS()

    async def get_news(self, query: str) -> List[Dict[str, str]]:
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
            results = self.ddgs.news(query, max_results=5)
            return [{"title": r["title"], "url": r["url"]} for r in results]
        except Exception as e:
            logger.error(f"DDG News error: {e}")
            return []

class YouTubeSearchTool:
    def __init__(self):
        self.ddgs = DDGS()

    async def search(self, query: str) -> List[Dict[str, str]]:
        """Search YouTube videos using DuckDuckGo."""
        try:
            # Note: ddgs.videos returns youtube results often
            results = self.ddgs.videos(query, max_results=5)
            return [{"title": r["title"], "url": r["content"]} for r in results]
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
            return []
