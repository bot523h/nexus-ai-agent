from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import literal_column
from sqlmodel import func, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserActiveAgent

logger = logging.getLogger(__name__)

#: ``/recent_users`` page-size ceiling: the endpoint is a dashboard widget, not
#: an export. Before the 2026-10-15 audit (finding A-06) ``?limit=`` was
#: unbounded and could pull the whole ``user`` table per request.
MAX_RECENT_USERS = 100

_DISABLED_DETAIL = (
    "dashboard API disabled: set NEXUS_DASHBOARD_TOKEN to enable it "
    "(requests must then send 'Authorization: Bearer <token>')"
)


def require_dashboard_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer-token gate for the whole dashboard API (P0-5, hardened by A-06).

    * ``NEXUS_DASHBOARD_TOKEN`` configured → every ``/api/dashboard/*`` request
      must send ``Authorization: Bearer <token>``; the comparison is
      constant-time and a bad/missing header is ``401``.
    * ``NEXUS_DASHBOARD_TOKEN`` **unset → the API is closed (``503``)**. The
      pre-audit behaviour was "open when unset, keep the port private", which
      silently published aggregate user data on any host that exposes the API
      port (Koyeb does). Fail-closed is the deny-by-default posture of the
      rest of the project; docker-compose users set the token in ``.env``.
    The responses themselves stay PII-free regardless of mode.
    """
    expected = get_settings().api_dashboard_token
    if not expected:
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL)
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


async def _count(session: Any, model: type[Any], key: str = "id") -> int:
    statement = select(func.count(literal_column(key))).select_from(model)
    return int((await session.exec(statement)).one())


@router.get("/stats")
async def get_global_stats() -> dict[str, int]:
    """Get high-level statistics for the dashboard."""
    async with get_session() as session:
        return {
            "total_users": await _count(session, User),
            "total_chats": await _count(session, Chat),
            "total_files": await _count(session, CloudFile),
            # composite PK: user_id — the table has no "id" column
            "active_specialized_agents": await _count(session, UserActiveAgent, "user_id"),
        }


@router.get("/recent_users")
async def get_recent_users(
    limit: int = Query(default=5, ge=1, le=MAX_RECENT_USERS),
) -> list[dict[str, Any]]:
    """Get list of recently joined users.

    PII-free (P0-5): returns only the internal DB surrogate id and the
    join timestamp — never ``telegram_id`` or ``username``. ``limit`` is
    validated to ``1..MAX_RECENT_USERS`` (``422`` outside the range).
    """
    async with get_session() as session:
        stmt = select(User).order_by(literal_column("id").desc()).limit(limit)
        users = (await session.exec(stmt)).all()
        return [
            {"id": u.id, "joined_at": u.created_at.isoformat() if u.created_at else None}
            for u in users
        ]
