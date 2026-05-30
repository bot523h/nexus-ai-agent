import httpx
from bs4 import BeautifulSoup
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class WikipediaTrainer:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)

    async def fetch_summary(self, query: str, lang: str = "fa") -> Optional[str]:
        """Fetch summary from Wikipedia without API key."""
        url = f"https://{lang}.wikipedia.org/wiki/{query.replace(' ', '_')}"
        try:
            response = await self.client.get(url)
            if response.status_code != 200:
                if lang == "fa": # Fallback to English
                    return await self.fetch_summary(query, "en")
                return None
            
            soup = BeautifulSoup(response.text, "lxml")
            # Extract first few paragraphs
            paragraphs = soup.find_all("p")
            content = ""
            for p in paragraphs:
                text = p.get_text().strip()
                if text:
                    content += text + "\n"
                if len(content) > 1000:
                    break
            return content.strip() if content else None
        except Exception as e:
            logger.error(f"Wikipedia fetch error: {e}")
            return None

    async def close(self):
        await self.client.aclose()
