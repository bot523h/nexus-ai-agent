from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, func

from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import User, Chat, CloudFile, UserActiveAgent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def get_global_stats():
    """Get high-level statistics for the dashboard."""
    async with get_session() as session:
        # Total Users
        user_count = (await session.exec(select(func.count(User.id)))).one()
        # Total Chats
        chat_count = (await session.exec(select(func.count(Chat.id)))).one()
        # Total Files
        file_count = (await session.exec(select(func.count(CloudFile.id)))).one()
        # Active Agents
        agent_count = (await session.exec(select(func.count(UserActiveAgent.id)))).one()
        
        return {
            "total_users": user_count,
            "total_chats": chat_count,
            "total_files": file_count,
            "active_specialized_agents": agent_count
        }


@router.get("/recent_users")
async def get_recent_users(limit: int = 5):
    """Get list of recently joined users."""
    async with get_session() as session:
        stmt = select(User).order_by(User.id.desc()).limit(limit)
        users = (await session.exec(stmt)).all()
        return [{"id": u.id, "username": u.username, "telegram_id": u.telegram_id} for u in users]
