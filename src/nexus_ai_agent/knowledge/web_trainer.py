"""Web search knowledge source using DuckDuckGo (free, no API key required).

Uses duckduckgo_search for queries and BeautifulSoup for HTML parsing.
"""

from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"


class WebTrainer:
    """Search the web via DuckDuckGo and parse results with BeautifulSoup."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search DuckDuckGo and return a list of results.

        Each result dict has keys: title, snippet, url.
        """
        results: list[dict[str, str]] = []
        try:
            resp = await self._client.post(_DDG_URL, data={"q": query, "kl": "wt-wt"})
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("ddg_search_error", query=query, error=str(exc))
            return results

        soup = BeautifulSoup(resp.text, "html.parser")
        for item in soup.select(".result"):
            if len(results) >= max_results:
                break
            title_el = item.select_one(".result__a")
            snippet_el = item.select_one(".result__snippet")
            if title_el is None:
                continue
            title = title_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            # DuckDuckGo wraps URLs; extract the actual URL
            raw_url: str = str(title_el.get("href", ""))
            if "uddg=" in raw_url:
                import urllib.parse

                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                raw_url = parsed.get("uddg", [raw_url])[0]

            results.append({"title": title, "snippet": snippet, "url": raw_url})

        logger.info("ddg_search_done", query=query, count=len(results))
        return results

    async def fetch_page(self, url: str, max_chars: int = 3000) -> str:
        """Fetch and extract text content from a web page.

        Returns the first *max_chars* characters of cleaned text.
        """
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("web_fetch_error", url=url, error=str(exc))
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:max_chars]

    async def learn(self, query: str, max_results: int = 3) -> dict[str, Any]:
        """Search and return combined summaries from top results.

        Returns a dict with keys: query, results (list), combined_text.
        """
        search_results = await self.search(query, max_results=max_results)
        combined_parts: list[str] = []
        for r in search_results:
            combined_parts.append(f"**{r['title']}**\n{r['snippet']}\n{r['url']}")

        combined = "\n\n---\n\n".join(combined_parts)
        return {
            "query": query,
            "results": search_results,
            "combined_text": combined,
            "source": "web",
        }
