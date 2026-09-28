from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import UserActiveAgent

from .specialized_agents import AGENTS, StoreAgent

if TYPE_CHECKING:
    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)


class AgentManager:
    """Manager for activating and retrieving specialized agents.

    W1 (Law 8): :meth:`get_active` MUST receive the runtime-owned
    ``GeminiProvider``; constructing a private provider is a legacy
    escape hatch reserved for tests (``allow_legacy_fallback=True``).
    """

    @staticmethod
    def list_agents() -> list[dict[str, str]]:
        """List all available agents with their metadata."""
        return [
            {
                "id": key,
                "name": agent.name,
                "emoji": agent.emoji,
                "description": agent.description,
                "category": agent.category,
            }
            for key, agent in AGENTS.items()
        ]

    @staticmethod
    async def activate(user_id: int, agent_id: str) -> bool:
        """Activate an agent for a specific user."""
        if agent_id not in AGENTS:
            return False

        async with get_session() as session:
            stmt = select(UserActiveAgent).where(UserActiveAgent.user_id == user_id)
            existing = (await session.execute(stmt)).scalar_one_or_none()

            if existing:
                existing.agent_name = agent_id
                existing.activated_at = datetime.utcnow()
                session.add(existing)
            else:
                new_active = UserActiveAgent(user_id=user_id, agent_name=agent_id)
                session.add(new_active)

            await session.commit()
            return True

    @staticmethod
    async def deactivate(user_id: int) -> None:
        """Deactivate any active agent for a user."""
        async with get_session() as session:
            stmt = select(UserActiveAgent).where(UserActiveAgent.user_id == user_id)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                await session.delete(existing)
                await session.commit()

    @staticmethod
    async def get_active(
        user_id: int,
        *,
        provider: "GeminiProvider | None" = None,
        allow_legacy_fallback: bool = False,
    ) -> StoreAgent | None:
        """Get the currently active agent for a user.

        *provider* is the runtime-owned canonical Gemini provider
        (W1 Law 8 — one provider owned by the runtime, never a
        per-agent private instance).  When no agent is active, returns
        ``None`` so the caller routes through the standard graph
        instead.
        """
        async with get_session() as session:
            stmt = select(UserActiveAgent).where(UserActiveAgent.user_id == user_id)
            active_record = (await session.execute(stmt)).scalar_one_or_none()

            if active_record and active_record.agent_name in AGENTS:
                agent_class = AGENTS[active_record.agent_name]
                return agent_class(
                    gemini_provider=provider,
                    allow_legacy_fallback=allow_legacy_fallback,
                )
        return None
