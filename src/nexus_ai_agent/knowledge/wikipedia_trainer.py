"""Wikipedia knowledge source — supports fa.wikipedia.org and en.wikipedia.org.

Uses the MediaWiki API (free, no key required) with 24-hour cache.
v3.1.0: migrated to AsyncDB + ResilientHttpClient + @instrumented.
"""

from __future__ import annotations

import time
from typing import Any

from nexus_ai_agent.core.async_db import AsyncDB
from nexus_ai_agent.core.http_client import ResilientHttpClient, get_http_client
from nexus_ai_agent.core.instrumentation import instrumented
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

# 24-hour cache TTL in seconds
_CACHE_TTL = 24 * 60 * 60

# Wikipedia API endpoints
_ENDPOINTS = {
    "fa": "https://fa.wikipedia.org/w/api.php",
    "en": "https://en.wikipedia.org/w/api.php",
}


class WikipediaTrainer:
    """Fetch and cache Wikipedia articles in Persian and English (async-safe)."""

    def __init__(
        self,
        cache_path: str = "data/wiki_cache.sqlite",
        *,
        http_client: ResilientHttpClient | None = None,
    ) -> None:
        self._db = AsyncDB(cache_path)
        self._http = http_client or get_http_client()
        self._initialized = False

    async def _ensure_init(self) -> None:
        """Lazy-initialize cache schema."""
        if self._initialized:
            return
        await self._db.script(
            """
            CREATE TABLE IF NOT EXISTS wiki_cache (
                query   TEXT NOT NULL,
                lang    TEXT NOT NULL DEFAULT 'fa',
                content TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (query, lang)
            );
            """
        )
        self._initialized = True

    # ── internal cache ───────────────────────────────────────

    async def _get_cached(self, query: str, lang: str) -> str | None:
        """Return cached content if it exists and is not expired."""
        await self._ensure_init()
        row = await self._db.fetchone(
            "SELECT content, fetched_at FROM wiki_cache WHERE query=? AND lang=?",
            (query, lang),
        )
        if row is None:
            return None
        content, fetched_at = row
        if time.time() - fetched_at > _CACHE_TTL:
            return None  # expired
        content_str: str = content
        return content_str

    async def _set_cached(self, query: str, lang: str, content: str) -> None:
        """Store content in the cache with current timestamp."""
        await self._ensure_init()
        await self._db.execute(
            "INSERT OR REPLACE INTO wiki_cache (query, lang, content, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (query, lang, content, time.time()),
        )

    # ── public API ───────────────────────────────────────────

    @instrumented("wikipedia.fetch")
    async def fetch(self, query: str, lang: str = "fa") -> dict[str, Any]:
        """Fetch a Wikipedia article summary.

        Returns a dict with keys: title, summary, url, lang, source.
        If the article is not found, summary will be an empty string.
        """
        # Check cache first
        cached = await self._get_cached(query, lang)
        if cached is not None:
            logger.info("wiki_cache_hit", query=query, lang=lang)
            return {
                "title": query,
                "summary": cached,
                "url": "",
                "lang": lang,
                "source": "wikipedia",
            }

        endpoint = _ENDPOINTS.get(lang, _ENDPOINTS["en"])

        # Step 1: Search for the article (resilient HTTP with retry+breaker)
        search_data = await self._http.get_json(
            endpoint,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 1,
            },
        )
        search_results = search_data.get("query", {}).get("search", [])
        if not search_results:
            logger.info("wiki_not_found", query=query, lang=lang)
            return {
                "title": query,
                "summary": "",
                "url": "",
                "lang": lang,
                "source": "wikipedia",
            }

        page_title = search_results[0]["title"]
        page_id = search_results[0]["pageid"]

        # Step 2: Get the article extract/summary
        summary_data = await self._http.get_json(
            endpoint,
            params={
                "action": "query",
                "pageids": page_id,
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "format": "json",
            },
        )
        pages = summary_data.get("query", {}).get("pages", {})
        page_data = pages.get(str(page_id), {})
        summary = page_data.get("extract", "")

        # Build URL
        base_url = endpoint.replace("/w/api.php", "/wiki/")
        url = f"{base_url}{page_title.replace(' ', '_')}"

        # Cache the result
        await self._set_cached(query, lang, summary)

        logger.info("wiki_fetched", query=query, lang=lang, title=page_title)
        return {
            "title": page_title,
            "summary": summary,
            "url": url,
            "lang": lang,
            "source": "wikipedia",
        }

    async def fetch_both(self, query: str) -> dict[str, Any]:
        """Fetch the article in both Persian and English, return combined result."""
        fa_result = await self.fetch(query, lang="fa")
        en_result = await self.fetch(query, lang="en")
        return {
            "query": query,
            "fa": fa_result,
            "en": en_result,
            "source": "wikipedia",
        }
