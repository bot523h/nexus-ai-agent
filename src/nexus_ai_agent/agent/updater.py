"""Self-update tooling (git pull + reinstall) behind owner approval."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import literal_column
from sqlmodel import select

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval

logger = logging.getLogger(__name__)

SELF_UPDATE_TYPE = "self_update"
_APPROVAL_WINDOW = timedelta(minutes=30)
_GIT_TIMEOUT_S = 300
_PIP_TIMEOUT_S = 1200
_RELEASE_CHECK_TIMEOUT_S = 15.0


class AutoUpdater:
    def __init__(self, current_version: str, repo: str = "bot523h/nexus-ai-agent") -> None:
        self.current_version = current_version
        self.repo = repo

    async def check_for_update(self) -> tuple[bool, str | None]:
        """Check GitHub for latest release."""
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=_RELEASE_CHECK_TIMEOUT_S) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    latest = resp.json().get("tag_name")
                    if latest != self.current_version:
                        return True, latest
            except Exception as e:
                logger.error("Update check error: %s", e)
        return False, None

    async def do_update(self, approval: ApprovalSystem | None = None) -> tuple[bool, str]:
        """Perform git pull and reinstall.

        If *approval* (an :class:`ApprovalSystem`) is provided, an *approved*
        ``PendingApproval`` with ``change_type="self_update"`` must exist and
        be newer than :data:`_APPROVAL_WINDOW`; otherwise a pending request is
        created (the owner is notified and can approve via ``/approve <id>``)
        and the update is refused.

        Passing ``approval=None`` skips the gate (dev/CLI use only — the bot
        command handler always passes a live ApprovalSystem).

        Returns a ``(success, detail)`` tuple.
        """
        if approval is not None:
            approved, detail = await self._ensure_self_update_approval(approval)
            if not approved:
                return False, detail

        try:
            # Off the event loop (audit A-07): `git pull` + `pip install .` take
            # minutes and used to freeze every other handler meanwhile. Each
            # step is bounded so a hung network call cannot wedge the bot.
            await asyncio.to_thread(
                subprocess.run, ["git", "pull"], check=True, timeout=_GIT_TIMEOUT_S
            )
            # The project has no requirements.txt — install from pyproject,
            # exactly like the Dockerfile does.
            await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", "."],
                check=True,
                timeout=_PIP_TIMEOUT_S,
            )
            logger.info("Update completed. Please restart the bot.")
            return True, "update completed"
        except Exception as e:  # noqa: BLE001 — every failure mode is reported to the owner
            logger.error("Update failed: %s", e)
            return False, f"update failed: {e}"

    async def _ensure_self_update_approval(self, approval: ApprovalSystem) -> tuple[bool, str]:
        """Return (approved, detail) for the self-update approval gate."""
        # naive UTC — matches PendingApproval.created_at
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with get_session() as session:
            result = await session.exec(
                select(PendingApproval)
                .where(
                    PendingApproval.change_type == SELF_UPDATE_TYPE,
                    PendingApproval.status == "approved",
                )
                .order_by(literal_column("created_at").desc())
            )
            latest = result.first()

        if latest is not None and latest.created_at >= now - _APPROVAL_WINDOW:
            return True, "approved"

        approval_id = await approval.request_approval(
            SELF_UPDATE_TYPE,
            f"Auto-update {self.current_version} -> latest (git pull + pip install .). "
            f"Approve with /approve <id>, then run /update again.",
        )
        return False, f"update requires owner approval (pending id={approval_id})"
