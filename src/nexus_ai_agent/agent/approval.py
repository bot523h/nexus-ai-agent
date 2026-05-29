"""Approval system — owner approval for major changes.

Changes require owner approval via inline keyboard.
If no response within 24 hours, auto-apply.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


class ApprovalSystem:
    """Manage change approvals from the bot owner."""

    def __init__(
        self,
        db_path: str = "data/approval_cache.sqlite",
        owner_id: int = 0,
        auto_apply_hours: float = 24.0,
    ) -> None:
        self._db_path = db_path
        self._owner_id = owner_id
        self._auto_apply_hours = auto_apply_hours
        self._init_db()

    def _init_db(self) -> None:
        """Create approval SQLite table."""
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                resolved_at REAL DEFAULT 0,
                auto_apply_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_status ON approvals(status)")
        conn.commit()
        conn.close()

    def request_approval(self, change_type: str, description: str) -> dict[str, Any]:
        """Create a new approval request.

        Returns a dict with the approval info.
        """
        auto_apply_at = time.time() + (self._auto_apply_hours * 3600)
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """
            INSERT INTO approvals (change_type, description, status, created_at, auto_apply_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (change_type, description, time.time(), auto_apply_at),
        )
        approval_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(
            "approval_requested",
            approval_id=approval_id,
            change_type=change_type,
        )
        return {
            "id": approval_id,
            "change_type": change_type,
            "description": description,
            "status": "pending",
            "auto_apply_at": auto_apply_at,
        }

    def approve(self, approval_id: int) -> bool:
        """Approve a pending change."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """
            UPDATE approvals SET status='approved', resolved_at=?
            WHERE id=? AND status='pending'
            """,
            (time.time(), approval_id),
        )
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        if success:
            logger.info("approval_approved", approval_id=approval_id)
        return success

    def reject(self, approval_id: int) -> bool:
        """Reject a pending change."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """
            UPDATE approvals SET status='rejected', resolved_at=?
            WHERE id=? AND status='pending'
            """,
            (time.time(), approval_id),
        )
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        if success:
            logger.info("approval_rejected", approval_id=approval_id)
        return success

    def check_auto_apply(self) -> list[dict[str, Any]]:
        """Check for pending approvals that have exceeded the auto-apply deadline.

        Returns list of auto-applied approvals.
        """
        now = time.time()
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            """
            SELECT id, change_type, description FROM approvals
            WHERE status='pending' AND auto_apply_at <= ?
            """,
            (now,),
        ).fetchall()
        applied: list[dict[str, Any]] = []
        for row in rows:
            conn.execute(
                """
                UPDATE approvals SET status='auto_approved', resolved_at=?
                WHERE id=?
                """,
                (now, row[0]),
            )
            applied.append(
                {
                    "id": row[0],
                    "change_type": row[1],
                    "description": row[2],
                    "status": "auto_approved",
                }
            )
            logger.info("approval_auto_applied", approval_id=row[0])
        conn.commit()
        conn.close()
        return applied

    def get_pending(self) -> list[dict[str, Any]]:
        """Get all pending approvals."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            """
            SELECT id, change_type, description, created_at, auto_apply_at
            FROM approvals WHERE status='pending' ORDER BY id DESC
            """
        ).fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "change_type": r[1],
                "description": r[2],
                "created_at": r[3],
                "auto_apply_at": r[4],
            }
            for r in rows
        ]

    def format_approval_request(self, approval: dict[str, Any]) -> str:
        """Format an approval request as a Telegram message."""
        hours_left = (approval.get("auto_apply_at", 0) - time.time()) / 3600
        return (
            f"🔔 **درخواست تأیید #{approval['id']}**\n\n"
            f"📋 نوع: {approval['change_type']}\n"
            f"📝 توضیحات: {approval['description']}\n"
            f"⏰ تأیید خودکار تا: {max(0, hours_left):.1f} ساعت دیگر\n\n"
            f"✅ /approve {approval['id']} — تأیید\n"
            f"❌ /reject {approval['id']} — رد"
        )
