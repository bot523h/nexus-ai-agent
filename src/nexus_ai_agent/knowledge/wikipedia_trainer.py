"""Wikipedia knowledge source — supports fa.wikipedia.org and en.wikipedia.org.

Uses the MediaWiki API (free, no key required) with 24-hour SQLite cache.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import httpx

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
    """Fetch and cache Wikipedia articles in Persian and English."""

    def __init__(self, cache_path: str = "data/wiki_cache.sqlite") -> None:
        self._cache_path = cache_path
        self._init_db()

    # ── internal cache ──────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the SQLite cache table if it does not exist."""
        conn = sqlite3.connect(self._cache_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_cache (
                query   TEXT NOT NULL,
                lang    TEXT NOT NULL DEFAULT 'fa',
                content TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (query, lang)
            )
            """
        )
        conn.commit()
        conn.close()

    def _get_cached(self, query: str, lang: str) -> str | None:
        """Return cached content if it exists and is not expired."""
        conn = sqlite3.connect(self._cache_path)
        row = conn.execute(
            "SELECT content, fetched_at FROM wiki_cache WHERE query=? AND lang=?",
            (query, lang),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        content, fetched_at = row
        if time.time() - fetched_at > _CACHE_TTL:
            return None  # expired
        return content

    def _set_cached(self, query: str, lang: str, content: str) -> None:
        """Store content in the cache with current timestamp."""
        conn = sqlite3.connect(self._cache_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO wiki_cache
                (query, lang, content, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (query, lang, content, time.time()),
        )
        conn.commit()
        conn.close()

    # ── public API ──────────────────────────────────────────────

    async def fetch(self, query: str, lang: str = "fa") -> dict[str, Any]:
        """Fetch a Wikipedia article summary.

        Returns a dict with keys: title, summary, url, lang, source.
        If the article is not found, summary will be an empty string.
        """
        # Check cache first
        cached = self._get_cached(query, lang)
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
        params: dict[str, Any] = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": 1,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: Search for the article
            resp = await client.get(endpoint, params=params)
            data = resp.json()
            search_results = data.get("query", {}).get("search", [])
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
            summary_params: dict[str, Any] = {
                "action": "query",
                "pageids": page_id,
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "format": "json",
            }
            resp2 = await client.get(endpoint, params=summary_params)
            data2 = resp2.json()
            pages = data2.get("query", {}).get("pages", {})
            page_data = pages.get(str(page_id), {})
            summary = page_data.get("extract", "")

            # Build URL
            base_url = endpoint.replace("/w/api.php", "/wiki/")
            url = f"{base_url}{page_title.replace(' ', '_')}"

        # Cache the result
        self._set_cached(query, lang, summary)

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
