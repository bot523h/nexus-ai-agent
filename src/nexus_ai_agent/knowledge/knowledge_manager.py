"""Unified Knowledge Manager — aggregates Wikipedia and web sources.

Provides:
  - learn(query): fetch from all sources and summarise with Gemini
  - scheduled_learn: periodic learning every 6 hours
  - KnowledgeCache table for persistent caching
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from nexus_ai_agent.knowledge.web_trainer import WebTrainer
from nexus_ai_agent.knowledge.wikipedia_trainer import WikipediaTrainer
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

# Cache TTL: 6 hours for knowledge cache (different from wiki 24h)
_KNOWLEDGE_CACHE_TTL = 6 * 60 * 60


class KnowledgeManager:
    """Orchestrate multiple knowledge sources and produce Gemini summaries."""

    def __init__(
        self,
        wiki_cache_path: str = "data/wiki_cache.sqlite",
        knowledge_cache_path: str = "data/knowledge_cache.sqlite",
        gemini_api_key: str | None = None,
    ) -> None:
        self._wiki = WikipediaTrainer(cache_path=wiki_cache_path)
        self._web = WebTrainer()
        self._knowledge_cache_path = knowledge_cache_path
        self._gemini_api_key = gemini_api_key
        self._init_db()
        self._scheduled_task: asyncio.Task[Any] | None = None

    def _init_db(self) -> None:
        """Create the knowledge_cache SQLite table."""
        conn = sqlite3.connect(self._knowledge_cache_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                query      TEXT NOT NULL,
                source     TEXT NOT NULL,
                content    TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_query ON knowledge_cache(query)")
        conn.commit()
        conn.close()

    def _get_knowledge_cached(self, query: str) -> str | None:
        """Return cached knowledge summary if not expired."""
        conn = sqlite3.connect(self._knowledge_cache_path)
        row = conn.execute(
            """
            SELECT content, expires_at
            FROM knowledge_cache
            WHERE query=? ORDER BY id DESC LIMIT 1
            """,
            (query,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        content, expires_at = row
        if time.time() > expires_at:
            return None
        return content

    def _set_knowledge_cached(self, query: str, source: str, content: str) -> None:
        """Store a knowledge summary in cache."""
        expires_at = time.time() + _KNOWLEDGE_CACHE_TTL
        conn = sqlite3.connect(self._knowledge_cache_path)
        conn.execute(
            """
            INSERT INTO knowledge_cache
                (query, source, content, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (query, source, content, expires_at),
        )
        conn.commit()
        conn.close()

    # ── Gemini summarisation ────────────────────────────────────

    async def _summarise_with_gemini(self, query: str, raw_text: str) -> str:
        """Use Gemini to produce a concise summary of the raw knowledge text."""
        if not self._gemini_api_key:
            # Fallback: return the raw text truncated
            return raw_text[:2000] if len(raw_text) > 2000 else raw_text

        try:
            import httpx as _httpx

            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash:generateContent"
                f"?key={self._gemini_api_key}"
            )
            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": (
                                    f"موضوع: {query}\n\n"
                                    f"اطلاعات خام:\n{raw_text[:4000]}\n\n"
                                    "لطفاً یک خلاصه جامع و مفید به زبان فارسی تهیه کن. "
                                    "شامل نکات کلیدی و مهم باشد."
                                )
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": 1024,
                    "temperature": 0.3,
                },
            }
            async with _httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", raw_text[:2000])
        except Exception as exc:
            logger.warning("gemini_summarise_error", error=str(exc))

        return raw_text[:2000] if len(raw_text) > 2000 else raw_text

    # ── public API ──────────────────────────────────────────────

    async def learn(self, query: str) -> dict[str, Any]:
        """Learn about a topic from all sources, summarise with Gemini, and cache.

        Returns a dict: query, summary, sources (list of source dicts).
        """
        # Check cache
        cached = self._get_knowledge_cached(query)
        if cached:
            logger.info("knowledge_cache_hit", query=query)
            return {
                "query": query,
                "summary": cached,
                "sources": [],
                "from_cache": True,
            }

        # Fetch from all sources concurrently
        wiki_task = self._wiki.fetch_both(query)
        web_task = self._web.learn(query)
        wiki_result, web_result = await asyncio.gather(wiki_task, web_task)

        # Combine raw texts
        raw_parts: list[str] = []
        sources: list[dict[str, Any]] = []

        # Wikipedia results
        for lang_key in ("fa", "en"):
            wiki_data = wiki_result.get(lang_key, {})
            if wiki_data.get("summary"):
                raw_parts.append(f"[Wikipedia {lang_key}] {wiki_data['summary']}")
                sources.append(
                    {
                        "source": f"wikipedia_{lang_key}",
                        "title": wiki_data.get("title", query),
                    }
                )

        # Web results
        for wr in web_result.get("results", []):
            raw_parts.append(f"[Web] {wr['title']}\n{wr['snippet']}")
            sources.append(
                {
                    "source": "web",
                    "title": wr["title"],
                    "url": wr.get("url", ""),
                }
            )

        raw_text = "\n\n".join(raw_parts) if raw_parts else "اطلاعاتی یافت نشد."

        # Summarise with Gemini
        summary = await self._summarise_with_gemini(query, raw_text)

        # Cache the result
        self._set_knowledge_cached(query, "knowledge_manager", summary)

        logger.info("knowledge_learned", query=query, num_sources=len(sources))
        return {
            "query": query,
            "summary": summary,
            "sources": sources,
            "from_cache": False,
        }

    async def wiki(self, query: str, lang: str = "fa") -> dict[str, Any]:
        """Fetch from Wikipedia only (shortcut)."""
        return await self._wiki.fetch(query, lang=lang)

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search the web only (shortcut)."""
        return await self._web.search(query, max_results=max_results)

    # ── scheduled learning ──────────────────────────────────────

    async def start_scheduled_learn(self, topics: list[str], interval_hours: float = 6.0) -> None:
        """Start a background task that re-learns topics every *interval_hours*."""

        async def _loop() -> None:
            while True:
                for topic in topics:
                    try:
                        await self.learn(topic)
                    except Exception as exc:
                        logger.warning("scheduled_learn_error", topic=topic, error=str(exc))
                await asyncio.sleep(interval_hours * 3600)

        self._scheduled_task = asyncio.create_task(_loop())
        logger.info("scheduled_learn_started", topics=topics, interval_hours=interval_hours)

    async def stop_scheduled_learn(self) -> None:
        """Stop the scheduled learning task."""
        if self._scheduled_task and not self._scheduled_task.done():
            self._scheduled_task.cancel()
            logger.info("scheduled_learn_stopped")

    async def close(self) -> None:
        """Clean up resources."""
        await self.stop_scheduled_learn()
        await self._web.close()
