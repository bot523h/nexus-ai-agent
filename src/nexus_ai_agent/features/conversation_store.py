"""Persistent conversation history — stores chat messages in SQLite.

Replaces GeminiEngine's in-memory _history dict so conversations
survive process restarts and can be queried/audited.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    text,
)

from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import sync_engine

log = get_logger(__name__)


def _naive_utcnow() -> datetime:
    """Current UTC time as the naive value the DATETIME columns store.

    ``datetime.utcnow()`` is deprecated (Python 3.12) and silently mixes naive
    and aware values; this keeps the stored representation unchanged (DTZ003,
    audit A-12).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


_CONVERSATION_METADATA = MetaData()
_CONVERSATION_HISTORY = Table(
    "conversation_history",
    _CONVERSATION_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conv_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("parts_json", Text, nullable=False),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("ix_conv_history_conv_id", "conv_id"),
)


class ConversationStore:
    """SQLite-backed conversation history store.

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
        ``storage.db.sync_engine`` (audit A-02)."""
        self._engine = sync_engine(db_path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create ``conversation_history`` (and its ``conv_id`` index) when missing.

        Declared as a private SQLAlchemy ``Table`` so the DDL is emitted per
        dialect (the former hand-written ``AUTOINCREMENT`` statement was
        SQLite-only); the table stays outside ``SQLModel.metadata`` because
        it is owned by this store, not by the Alembic-managed schema.
        """
        _CONVERSATION_METADATA.create_all(self._engine, checkfirst=True)

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
                    "ts": _naive_utcnow().isoformat(),
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
                        "ts": _naive_utcnow().isoformat(),
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
