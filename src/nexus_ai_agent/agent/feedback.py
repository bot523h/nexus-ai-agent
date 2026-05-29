"""Feedback collection system — inline 👍👎 after AI responses.

Collects user feedback, asks for reasons on 👎, and generates
daily reports for the bot owner.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from telegram import InlineKeyboardMarkup

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


class FeedbackCollector:
    """Collect and manage user feedback via inline keyboards."""

    def __init__(self, db_path: str = "data/feedback_cache.sqlite") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create feedback SQLite table."""
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL DEFAULT 0,
                feedback_type TEXT NOT NULL,
                reason TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_created ON feedback(created_at)")
        conn.commit()
        conn.close()

    def save_feedback(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        feedback_type: str,
        reason: str = "",
    ) -> None:
        """Save a feedback entry to the database."""
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            INSERT INTO feedback (user_id, chat_id, message_id, feedback_type, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, message_id, feedback_type, reason, time.time()),
        )
        conn.commit()
        conn.close()
        logger.info(
            "feedback_saved",
            user_id=user_id,
            feedback_type=feedback_type,
        )

    def get_daily_report(self) -> dict[str, Any]:
        """Generate a daily feedback report.

        Returns a dict with: total, positive, negative, top_reasons.
        """
        one_day_ago = time.time() - 86400
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            """
            SELECT feedback_type, reason FROM feedback
            WHERE created_at > ?
            """,
            (one_day_ago,),
        ).fetchall()
        conn.close()

        positive = sum(1 for r in rows if r[0] == "positive")
        negative = sum(1 for r in rows if r[0] == "negative")
        reasons = [r[1] for r in rows if r[0] == "negative" and r[1]]

        return {
            "total": len(rows),
            "positive": positive,
            "negative": negative,
            "top_reasons": reasons[:10],
        }

    def format_report(self, report: dict[str, Any]) -> str:
        """Format the feedback report as a Telegram message."""
        text = (
            f"📊 **گزارش بازخورد ۲۴ ساعته**\n\n"
            f"📝 کل: {report['total']}\n"
            f"👍 مثبت: {report['positive']}\n"
            f"👎 منفی: {report['negative']}\n"
        )
        if report["top_reasons"]:
            text += "\n**دلایل منفی:**\n"
            for i, reason in enumerate(report["top_reasons"], 1):
                text += f"  {i}. {reason}\n"
        if report["total"] == 0:
            text += "\nهنوز بازخوردی ثبت نشده."
        return text

    @staticmethod
    def get_feedback_keyboard(message_id: int) -> InlineKeyboardMarkup:
        """Return an inline keyboard with 👍 👎 buttons."""
        from telegram import InlineKeyboardButton

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("👍", callback_data=f"fb_pos:{message_id}"),
                    InlineKeyboardButton("👎", callback_data=f"fb_neg:{message_id}"),
                ]
            ]
        )

    @staticmethod
    def get_reason_keyboard(message_id: int) -> InlineKeyboardMarkup:
        """Return an inline keyboard for 👎 reason selection."""
        from telegram import InlineKeyboardButton

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("❌ نادرست", callback_data=f"fb_r:incorrect:{message_id}"),
                    InlineKeyboardButton("😰 بی‌ربط", callback_data=f"fb_r:irrelevant:{message_id}"),
                ],
                [
                    InlineKeyboardButton("🗨 نامفهوم", callback_data=f"fb_r:unclear:{message_id}"),
                    InlineKeyboardButton(
                        "🎭 توهین‌آمیز", callback_data=f"fb_r:offensive:{message_id}"
                    ),
                ],
            ]
        )
