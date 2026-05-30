from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlmodel import select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import UserMemory

logger = logging.getLogger(__name__)


class AIMemoryEngine:
    """Engine for analyzing user interactions and maintaining long-term memory."""

    def __init__(self, gemini_provider: GeminiProvider | None = None) -> None:
        settings = get_settings()
        self.gemini = gemini_provider or GeminiProvider(api_key=settings.gemini_api_key or "")

    async def update_from_message(self, user_id: int, message: str) -> None:
        """Extract important user information from a message."""
        prompt = f"""
        Analyze the following message from a user and extract key personal information.
        Information to look for: Name, Interests, Occupation, Personality traits.
        
        User Message: "{message}"
        
        Return ONLY a JSON object with these keys: 
        name, interests (list), occupation, personality_tags (list).
        If no new information is found, return an empty JSON object {{}}.
        """

        try:
            response_text = await self.gemini.generate(
                prompt=prompt, system="You are a personal information extractor."
            )
            # Basic JSON extraction from response
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start != -1 and end != -1:
                data = json.loads(response_text[start:end])
                if data:
                    await self._save_memory(user_id, data)
        except Exception as e:
            logger.error(f"Failed to update AI memory: {e}")

    async def _save_memory(self, user_id: int, data: dict) -> None:
        """Merge new data into persistent UserMemory."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()

            if not memory:
                memory = UserMemory(user_id=user_id, last_updated=datetime.utcnow())

            if data.get("name"):
                memory.name = data["name"]
            if data.get("occupation"):
                memory.occupation = data["occupation"]

            if data.get("interests"):
                existing_interests = json.loads(memory.interests)
                new_interests = list(set(existing_interests + data["interests"]))
                memory.interests = json.dumps(new_interests)

            if data.get("personality_tags"):
                existing_tags = json.loads(memory.personality_tags)
                new_tags = list(set(existing_tags + data["personality_tags"]))
                memory.personality_tags = json.dumps(new_tags)

            memory.last_updated = datetime.utcnow()
            session.add(memory)
            await session.commit()

    async def get_context(self, user_id: int) -> str:
        """Generate a context string for system prompt injection."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()

            if not memory:
                return ""

            parts = []
            if memory.name:
                parts.append(f"User Name: {memory.name}")
            if memory.occupation:
                parts.append(f"Occupation: {memory.occupation}")

            interests = json.loads(memory.interests)
            if interests:
                parts.append(f"Interests: {', '.join(interests)}")

            tags = json.loads(memory.personality_tags)
            if tags:
                parts.append(f"Personality: {', '.join(tags)}")

            return " | ".join(parts)

    async def forget_user(self, user_id: int) -> None:
        """Wipe all memory for a user."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory:
                await session.delete(memory)
                await session.commit()
