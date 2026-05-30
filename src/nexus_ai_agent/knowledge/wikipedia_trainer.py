from __future__ import annotations

import logging
from typing import Optional

from bs4 import BeautifulSoup

from nexus_ai_agent.core.http_client import get_http_client
from nexus_ai_agent.core.instrumentation import instrumented

logger = logging.getLogger(__name__)


class WikipediaTrainer:
    """Wikipedia content fetcher with resilient HTTP and instrumentation."""

    def __init__(self) -> None:
        self.client = get_http_client()

    @instrumented("knowledge.wikipedia.fetch")
    async def fetch_summary(self, query: str, lang: str = "fa") -> Optional[str]:
        """Fetch summary from Wikipedia without API key."""
        url = f"https://{lang}.wikipedia.org/wiki/{query.replace(' ', '_')}"
        try:
            text = await self.client.get_text(url)
            if not text:
                if lang == "fa":  # Fallback to English
                    return await self.fetch_summary(query, "en")
                return None

            soup = BeautifulSoup(text, "lxml")
            # Extract first few paragraphs
            paragraphs = soup.find_all("p")
            content = ""
            for p in paragraphs:
                p_text = p.get_text().strip()
                if p_text:
                    content += p_text + "\n"
                if len(content) > 1000:
                    break
            return content.strip() if content else None
        except Exception as e:
            logger.error(f"Wikipedia fetch error: {e}")
            return None

    async def close(self) -> None:
        """No-op as we use the shared singleton client."""
        pass
