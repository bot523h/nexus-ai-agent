from __future__ import annotations

import logging
from datetime import datetime

from telegram import Bot

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval

logger = logging.getLogger(__name__)


class ApprovalSystem:
    """Persistent approval system for sensitive actions."""

    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self.settings = get_settings()

    async def request_approval(
        self, change_type: str, description: str, auto_apply_at: datetime | None = None
    ) -> int:
        """Request owner approval for an action and persist to DB."""
        async with get_session() as session:
            approval = PendingApproval(
                change_type=change_type,
                description=description,
                auto_apply_at=auto_apply_at,
            )
            session.add(approval)
            await session.commit()
            await session.refresh(approval)

            # Notify owner
            if self.bot and self.settings.owner_telegram_id:
                try:
                    msg = (
                        f"🚨 *درخواست تایید جدید*\n"
                        f"نوع: {change_type}\n"
                        f"توضیحات: {description}\n"
                        f"شناسه: `{approval.id}`\n\n"
                        f"برای تایید: `/approve {approval.id}`\n"
                        f"برای رد: `/reject {approval.id}`"
                    )
                    await self.bot.send_message(
                        chat_id=self.settings.owner_telegram_id,
                        text=msg,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Failed to notify owner: {e}")

            return approval.id or 0

    async def approve(self, approval_id: int) -> bool:
        """Approve a pending request."""
        async with get_session() as session:
            approval = await session.get(PendingApproval, approval_id)
            if approval and approval.status == "pending":
                approval.status = "approved"
                session.add(approval)
                await session.commit()
                logger.info(f"Action {approval_id} approved: {approval.description}")
                return True
        return False

    async def reject(self, approval_id: int) -> bool:
        """Reject a pending request."""
        async with get_session() as session:
            approval = await session.get(PendingApproval, approval_id)
            if approval and approval.status == "pending":
                approval.status = "rejected"
                session.add(approval)
                await session.commit()
                logger.info(f"Action {approval_id} rejected: {approval.description}")
                return True
        return False
