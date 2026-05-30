"""Persistent conversation history — stores chat messages in SQLite.

Replaces GeminiEngine's in-memory _history dict so conversations
survive process restarts and can be queried/audited.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class ConversationStore:
    """SQLite-backed conversation history store.

    Each conversation is identified by a ``conv_id`` (e.g. ``"tg:12345"``).
    Messages are stored as JSON-serialised dicts with ``role`` and ``parts``.

    Usage::

        store = ConversationStore(db_path="data/app.sqlite")
        store.append(conv_id, {"role": "user", "parts": [{"text": "Hi"}]})
        history = store.get_history(conv_id, limit=20)
        store.clear(conv_id)
    """

    def __init__(self, db_path: str = "data/app.sqlite") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create the conversation_history table if it doesn't exist."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS conversation_history ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "conv_id VARCHAR NOT NULL, "
                    "role VARCHAR NOT NULL, "
                    "parts_json TEXT NOT NULL, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            # Index for fast lookup by conv_id
            try:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_conv_history_conv_id "
                        "ON conversation_history(conv_id)"
                    )
                )
            except Exception:
                pass  # Index may already exist

    def get_history(self, conv_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent conversation history for *conv_id*.

        Returns messages ordered chronologically (oldest first), up to *limit*.
        """
        with self._engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT role, parts_json FROM conversation_history "
                    "WHERE conv_id = :cid ORDER BY id DESC LIMIT :lim"
                ),
                {"cid": conv_id, "lim": limit},
            )
            rows = list(result.fetchall())
            # Reverse to get chronological order
            rows.reverse()
            messages: list[dict[str, Any]] = []
            for role, parts_json in rows:
                try:
                    parts = json.loads(parts_json)
                except (json.JSONDecodeError, TypeError):
                    parts = [{"text": parts_json}]
                messages.append({"role": role, "parts": parts})
            return messages

    def append(self, conv_id: str, message: dict[str, Any]) -> None:
        """Append a single message to the conversation history."""
        role = message.get("role", "user")
        parts = message.get("parts", [])
        parts_json = json.dumps(parts, ensure_ascii=False)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversation_history (conv_id, role, parts_json, created_at) "
                    "VALUES (:cid, :role, :parts, :ts)"
                ),
                {
                    "cid": conv_id,
                    "role": role,
                    "parts": parts_json,
                    "ts": datetime.utcnow().isoformat(),
                },
            )

    def append_batch(self, conv_id: str, messages: list[dict[str, Any]]) -> None:
        """Append multiple messages to the conversation history."""
        with self._engine.begin() as conn:
            for msg in messages:
                role = msg.get("role", "user")
                parts = msg.get("parts", [])
                parts_json = json.dumps(parts, ensure_ascii=False)
                conn.execute(
                    text(
                        "INSERT INTO conversation_history (conv_id, role, parts_json, created_at) "
                        "VALUES (:cid, :role, :parts, :ts)"
                    ),
                    {
                        "cid": conv_id,
                        "role": role,
                        "parts": parts_json,
                        "ts": datetime.utcnow().isoformat(),
                    },
                )

    def clear(self, conv_id: str) -> None:
        """Delete all messages for a conversation."""
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM conversation_history WHERE conv_id = :cid"),
                {"cid": conv_id},
            )
        log.info("conv_history_cleared", conv_id=conv_id)

    def active_conversations(self) -> int:
        """Count distinct conversations with at least one message."""
        with self._engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(DISTINCT conv_id) FROM conversation_history"))
            row = result.fetchone()
            return row[0] if row else 0

    def trim_to_limit(self, conv_id: str, limit: int = 20) -> None:
        """Keep only the most recent *limit* messages for *conv_id*.

        Removes older messages to prevent unbounded growth.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM conversation_history "
                    "WHERE conv_id = :cid AND id NOT IN ("
                    "  SELECT id FROM conversation_history "
                    "  WHERE conv_id = :cid ORDER BY id DESC LIMIT :lim"
                    ")"
                ),
                {"cid": conv_id, "lim": limit},
            )
