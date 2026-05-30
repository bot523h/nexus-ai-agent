from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup
from duckduckgo_search import DDGS  # type: ignore

from nexus_ai_agent.core.http_client import get_http_client
from nexus_ai_agent.core.instrumentation import instrumented

logger = logging.getLogger(__name__)


class WebTrainer:
    """Web search and content extractor with resilient HTTP and instrumentation."""

    def __init__(self) -> None:
        self.ddgs = DDGS()
        self.client = get_http_client()

    @instrumented("knowledge.web.search")
    async def search_and_summarize(self, query: str) -> list[dict[str, str]]:
        """Search web using DuckDuckGo and extract content."""
        results = []
        try:
            search_results: Any = self.ddgs.text(query, max_results=3)
            for res in search_results:
                url = res.get("href")
                title = res.get("title")
                try:
                    text = await self.client.get_text(url)
                    if text:
                        soup = BeautifulSoup(text, "lxml")
                        # Remove script and style elements
                        for script in soup(["script", "style"]):
                            script.decompose()
                        content = soup.get_text(separator=" ", strip=True)
                        results.append(
                            {
                                "title": title,
                                "url": url,
                                "content": content[:2000],  # Limit content size
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
        """No-op as we use the shared singleton client."""
        pass
