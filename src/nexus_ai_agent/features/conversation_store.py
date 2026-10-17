"""Persistent conversation history — stores chat messages in the configured database.

Replaces GeminiEngine's in-memory _history dict so conversations
survive process restarts and can be queried/audited.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.clock import naive_utcnow
from nexus_ai_agent.storage.db import sync_engine

log = get_logger(__name__)

# The ``conversation_history`` table this store reads and writes is declared
# once, in ``storage.models.ConversationHistory``, and provisioned by the
# Alembic chain (revision ``c7a1e5d2b9f3``) — never by this module (audit
# 2026-10-15, schema authority).


class ConversationStore:
    """Conversation history store on the configured database (SQLite or PostgreSQL).

    Each conversation is identified by a ``conv_id`` (e.g. ``"tg:12345"``).
    Messages are stored as JSON-serialised dicts with ``role`` and ``parts``.

    Usage::

        store = ConversationStore()  # or ConversationStore(db_path="…sqlite")
        store.append(conv_id, {"role": "user", "parts": [{"text": "Hi"}]})
        history = store.get_history(conv_id, limit=20)
        store.clear(conv_id)
    """

    def __init__(self, db_path: str | None = None) -> None:
        """``db_path`` pins a SQLite file; ``None`` follows the configured
        backend (``NEXUS_DATABASE_URL`` or ``NEXUS_DB_PATH``) through
        ``storage.db.sync_engine`` (audit A-02).

        The constructor never touches the schema: ``conversation_history`` is
        created by the migration chain — ``ensure_startup_schema`` before the
        bot polls (SQLite) or ``nexus migrate`` / the lazy PostgreSQL
        preparation (D7).  A missing table therefore surfaces as a database
        error from the first query, which is the correct, loud failure for an
        un-migrated deployment.
        """
        self._engine = sync_engine(db_path)

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
                    "ts": naive_utcnow().isoformat(),
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
                        "ts": naive_utcnow().isoformat(),
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
