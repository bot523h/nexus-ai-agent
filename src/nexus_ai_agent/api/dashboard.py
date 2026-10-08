from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import literal_column
from sqlmodel import func, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserActiveAgent

logger = logging.getLogger(__name__)


def require_dashboard_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer-token gate for the whole dashboard API (P0-5).

    When ``NEXUS_DASHBOARD_TOKEN`` is configured, every ``/api/dashboard/*``
    request must send ``Authorization: Bearer <token>``; the comparison is
    constant-time. When the token is unset the API is open — deployments
    must then keep the port private (docker-compose binds 127.0.0.1 by
    default). The responses themselves are PII-free regardless of mode.
    """
    expected = get_settings().api_dashboard_token
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[7:].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid token")


router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_dashboard_token)],
)


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
    """Get list of recently joined users.

    PII-free (P0-5): returns only the internal DB surrogate id and the
    join timestamp — never ``telegram_id`` or ``username``.
    """
    async with get_session() as session:
        stmt = select(User).order_by(literal_column("id").desc()).limit(limit)
        users = (await session.execute(stmt)).scalars().all()
        return [
            {"id": u.id, "joined_at": u.created_at.isoformat() if u.created_at else None}
            for u in users
        ]
