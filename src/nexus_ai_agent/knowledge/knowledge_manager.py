from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.instrumentation import instrumented
from nexus_ai_agent.knowledge.web_trainer import WebTrainer
from nexus_ai_agent.knowledge.wikipedia_trainer import WikipediaTrainer
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import KnowledgeCache

logger = logging.getLogger(__name__)


class KnowledgeManager:
    """Knowledge orchestration layer with instrumentation."""

    def __init__(self, gemini_provider: GeminiProvider | None = None) -> None:
        self.wiki = WikipediaTrainer()
        self.web = WebTrainer()
        settings = get_settings()
        self.gemini = gemini_provider or GeminiProvider(api_key=settings.gemini_api_key or "")

    async def get_cached_knowledge(self, query: str) -> str | None:
        """Retrieve knowledge from cache if not expired."""
        async with get_session() as session:
            statement = select(KnowledgeCache).where(
                KnowledgeCache.query == query, KnowledgeCache.expires_at > datetime.utcnow()
            )
            result = await session.execute(statement)
            cache_entry = result.scalar_one_or_none()
            return cache_entry.content if cache_entry else None

    @instrumented("knowledge.learn")
    async def learn(self, query: str) -> str:
        """Learn about a topic from all sources and summarize."""
        cached = await self.get_cached_knowledge(query)
        if cached:
            return cached

        # 1. Fetch from Wikipedia
        wiki_content = await self.wiki.fetch_summary(query)

        # 2. Fetch from Web
        web_results = await self.web.search_and_summarize(query)
        web_content = "\n".join([f"Source: {r['url']}\n{r['content']}" for r in web_results])

        # 3. Combine and Summarize with Gemini
        combined_prompt = (
            f"Topic: {query}\n\n"
            f"Wikipedia Content:\n{wiki_content or 'Not found'}\n\n"
            f"Web Search Content:\n{web_content or 'Not found'}\n\n"
            "Please provide a comprehensive and concise summary of this topic in Persian (Farsi)."
        )

        summary = await self.gemini.generate(
            prompt=combined_prompt,
            system=(
                "You are an expert knowledge assistant. "
                "Summarize information accurately and professionally in Persian."
            ),
        )

        # 4. Cache the result
        async with get_session() as session:
            cache_entry = KnowledgeCache(
                query=query,
                source="combined",
                content=summary,
                expires_at=datetime.utcnow() + timedelta(hours=24),
            )
            session.add(cache_entry)
            await session.commit()

        return summary

    async def close(self) -> None:
        """Close underlying trainers."""
        await self.wiki.close()
        await self.web.close()
