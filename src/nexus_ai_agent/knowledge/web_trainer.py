import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS  # type: ignore

logger = logging.getLogger(__name__)


class WebTrainer:
    def __init__(self) -> None:
        self.ddgs = DDGS()
        self.client = httpx.AsyncClient(timeout=10.0)

    async def search_and_summarize(self, query: str) -> list[dict[str, str]]:
        """Search web using DuckDuckGo and extract content."""
        results = []
        try:
            search_results: Any = self.ddgs.text(query, max_results=3)
            for res in search_results:
                url = res.get("href")
                title = res.get("title")
                try:
                    resp = await self.client.get(url)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "lxml")
                        # Remove script and style elements
                        for script in soup(["script", "style"]):
                            script.decompose()
                        text = soup.get_text(separator=" ", strip=True)
                        results.append(
                            {
                                "title": title,
                                "url": url,
                                "content": text[:2000],  # Limit content size
                            }
                        )
                except Exception as e:
                    logger.warning(f"Failed to fetch {url}: {e}")
                    continue
            return results
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    async def close(self) -> None:
        await self.client.aclose()
