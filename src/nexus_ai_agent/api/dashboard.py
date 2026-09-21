"""Read-only dashboard API.

Privacy stance (2026-09-21, P0-security-code-batch): this router is served on a
public HTTP port and is *not* behind Telegram auth, so it must not hand out
identifiers that let a stranger contact or track a real user.  ``telegram_id``
is a direct messaging handle and ``username`` is public-but-linkable; both are
now withheld.  An optional bearer token (``NEXUS_DASHBOARD_TOKEN``) can be
configured to lock the whole router down for operator-only use.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import literal_column
from sqlmodel import func, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserActiveAgent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _mask(value: str | None) -> str:
    """Reduce a username to a non-reversible hint (``alice`` → ``a***e``)."""
    name = (value or "").strip()
    if not name:
        return "کاربر"
    if len(name) <= 2:
        return f"{name[0]}***"
    return f"{name[0]}***{name[-1]}"


async def require_dashboard_access(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Optional bearer-token gate for the dashboard router.

    * ``NEXUS_DASHBOARD_TOKEN`` unset (default) → the router stays open, but it
      only ever answers aggregate counts and masked labels, so there is no PII
      to leak.
    * ``NEXUS_DASHBOARD_TOKEN`` set → every request must carry
      ``Authorization: Bearer <token>``; anything else is a 401.  The
      comparison is constant-time.
    """
    expected = (get_settings().api_dashboard_token or "").strip()
    if not expected:
        return
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid dashboard credentials")


@router.get("/stats", dependencies=[Depends(require_dashboard_access)])
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


@router.get("/recent_users", dependencies=[Depends(require_dashboard_access)])
async def get_recent_users(limit: int = 5) -> list[dict[str, Any]]:
    """Recently joined users, with identifying fields withheld.

    ``telegram_id`` was returned verbatim until v3.13.0 on an unauthenticated
    public port; it is gone, and ``username`` is reduced to a masked hint.
    """
    # Clamp: a negative LIMIT means "unbounded" in SQLite and a huge one is a
    # cheap way to dump the user table through a public port.
    page = max(1, min(int(limit), 50))
    async with get_session() as session:
        stmt = select(User).order_by(literal_column("id").desc()).limit(page)
        users = (await session.execute(stmt)).scalars().all()
        return [{"id": u.id, "display": _mask(u.username)} for u in users]
