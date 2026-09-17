from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import literal_column
from sqlmodel import func, select

from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserActiveAgent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def get_global_stats() -> dict[str, int]:
    """Get high-level statistics for the dashboard."""
    async with get_session() as session:
        # Total Users
        user_count = (
            await session.execute(select(func.count(literal_column("id"))).select_from(User))
        ).scalar_one()
        # Total Chats
        chat_count = (
            await session.execute(select(func.count(literal_column("id"))).select_from(Chat))
        ).scalar_one()
        # Total Files
        file_count = (
            await session.execute(select(func.count(literal_column("id"))).select_from(CloudFile))
        ).scalar_one()
        # Active Agents (composite PK: user_id — the table has no "id" column)
        agent_count = (
            await session.execute(
                select(func.count(literal_column("user_id"))).select_from(UserActiveAgent)
            )
        ).scalar_one()

        return {
            "total_users": user_count,
            "total_chats": chat_count,
            "total_files": file_count,
            "active_specialized_agents": agent_count,
        }


@router.get("/recent_users")
async def get_recent_users(limit: int = 5) -> list[dict[str, Any]]:
    """Get list of recently joined users."""
    async with get_session() as session:
        stmt = select(User).order_by(literal_column("id").desc()).limit(limit)
        users = (await session.execute(stmt)).scalars().all()
        return [{"id": u.id, "username": u.username, "telegram_id": u.telegram_id} for u in users]
