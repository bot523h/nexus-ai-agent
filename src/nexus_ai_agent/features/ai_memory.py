"""Long-term user memory extracted by the LLM — **opt-in only**.

Privacy contract (audit P0-9 / task B7):

* Nothing is sent to Gemini unless the user has explicitly enabled memory
  with :meth:`AIMemoryEngine.enable` (``/memory on``).  The consent record is
  the ``UserMemory`` row itself: it exists iff memory is switched on, so no
  schema change is needed and ``/forget_me`` (= :meth:`disable`) both wipes
  the profile and stops further learning in one step.
* The provider is created lazily, so constructing an engine per message (as
  the legacy handler does) costs nothing for users who never opted in.
* Commands and very short messages are never analysed.

.. note:: Rows written before this change (when memory was always on) are
   treated as consent so those users keep their profile; the owner can purge
   them (``DELETE FROM usermemory``) or users can run ``/forget_me``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlmodel import select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import UserMemory

logger = logging.getLogger(__name__)

#: Messages shorter than this carry no profile information worth an LLM call.
MIN_MESSAGE_CHARS = 12
#: Longer messages are truncated before extraction (cost cap, prompt safety).
MAX_MESSAGE_CHARS = 2000
#: Bound for lists merged into the profile.
_MAX_LIST_ITEMS = 30
_MAX_FIELD_CHARS = 120

_EXTRACTION_SYSTEM = "You are a personal information extractor."
_EXTRACTION_PROMPT = """
Analyze the following message from a user and extract key personal information.
Information to look for: Name, Interests, Occupation, Personality traits.

User Message: {message!r}

Return ONLY a JSON object with these keys:
name, interests (list), occupation, personality_tags (list).
If no new information is found, return an empty JSON object {{}}.
"""


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_MAX_FIELD_CHARS] if value else None


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        cleaned = _clean_str(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:_MAX_LIST_ITEMS]


def _merge_list(existing_json: str, new_items: list[str]) -> str:
    try:
        existing = json.loads(existing_json or "[]")
    except (TypeError, ValueError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    merged = [str(x) for x in existing]
    for item in new_items:
        if item not in merged:
            merged.append(item)
    return json.dumps(merged[-_MAX_LIST_ITEMS:], ensure_ascii=False)


def parse_extraction(response_text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply; ``{}`` when absent/invalid."""
    start = response_text.find("{")
    end = response_text.rfind("}") + 1
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(response_text[start:end])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class AIMemoryEngine:
    """Engine for analyzing user interactions and maintaining long-term memory."""

    def __init__(self, gemini_provider: Any | None = None) -> None:
        self._gemini = gemini_provider

    @property
    def gemini(self) -> Any:
        """The LLM provider, created on first use (never for non-consenting users)."""
        if self._gemini is None:
            from nexus_ai_agent.llm.gemini_provider import GeminiProvider

            settings = get_settings()
            self._gemini = GeminiProvider(api_key=settings.gemini_api_key or "")
        return self._gemini

    # ── consent ────────────────────────────────────────────────────

    async def is_enabled(self, user_id: int) -> bool:
        """True when *user_id* has opted in to long-term memory."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def enable(self, user_id: int) -> bool:
        """Opt in. Returns ``False`` when memory was already on."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            if (await session.execute(stmt)).scalar_one_or_none() is not None:
                return False
            session.add(UserMemory(user_id=user_id, last_updated=datetime.utcnow()))
            await session.commit()
            return True

    async def disable(self, user_id: int) -> None:
        """Opt out: wipes the profile and stops learning (alias of :meth:`forget_user`)."""
        await self.forget_user(user_id)

    # ── learning ───────────────────────────────────────────────────

    @staticmethod
    def should_analyse(message: str) -> bool:
        """Cheap pre-filter: commands and tiny messages never reach the LLM."""
        text = (message or "").strip()
        return len(text) >= MIN_MESSAGE_CHARS and not text.startswith("/")

    async def update_from_message(self, user_id: int, message: str) -> bool:
        """Extract profile facts from *message* — only for opted-in users.

        Returns ``True`` when something new was stored.
        """
        if not self.should_analyse(message):
            return False
        try:
            if not await self.is_enabled(user_id):
                return False
        except Exception:  # noqa: BLE001 - storage hiccup must not spam Gemini
            logger.warning("ai_memory consent lookup failed for %s", user_id, exc_info=True)
            return False

        prompt = _EXTRACTION_PROMPT.format(message=message.strip()[:MAX_MESSAGE_CHARS])
        try:
            response_text = await self.gemini.generate(prompt=prompt, system=_EXTRACTION_SYSTEM)
            data = parse_extraction(response_text or "")
            if not data:
                return False
            return await self._save_memory(user_id, data)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to update AI memory: %s", e)
            return False

    async def _save_memory(self, user_id: int, data: dict[str, Any]) -> bool:
        """Merge new data into persistent UserMemory. Returns True when a row changed."""
        name = _clean_str(data.get("name"))
        occupation = _clean_str(data.get("occupation"))
        interests = _clean_list(data.get("interests"))
        tags = _clean_list(data.get("personality_tags"))
        if not any((name, occupation, interests, tags)):
            return False

        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if not memory:
                memory = UserMemory(user_id=user_id, last_updated=datetime.utcnow())

            if name:
                memory.name = name
            if occupation:
                memory.occupation = occupation
            if interests:
                memory.interests = _merge_list(memory.interests, interests)
            if tags:
                memory.personality_tags = _merge_list(memory.personality_tags, tags)

            memory.last_updated = datetime.utcnow()
            session.add(memory)
            await session.commit()
            return True

    # ── read ───────────────────────────────────────────────────────

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

            interests = _clean_list(json.loads(memory.interests or "[]"))
            if interests:
                parts.append(f"Interests: {', '.join(interests)}")

            tags = _clean_list(json.loads(memory.personality_tags or "[]"))
            if tags:
                parts.append(f"Personality: {', '.join(tags)}")

            return " | ".join(parts)

    async def forget_user(self, user_id: int) -> None:
        """Wipe all memory for a user (and revoke consent)."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory:
                await session.delete(memory)
                await session.commit()
