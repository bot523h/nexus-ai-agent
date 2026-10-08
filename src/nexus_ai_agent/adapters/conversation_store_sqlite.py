"""Async SQLite adapter for :class:`ConversationStorePort` (task-110).

The port existed with no implementation: the bot talked to
``features/conversation_store.py`` directly, which is synchronous (it blocks the
event loop on every message) and whose shape does not match the port. This
adapter closes that gap without rewriting the legacy store:

* **async-only** — every method is a coroutine on ``aiosqlite``, so no thread is
  blocked while a message is persisted (the direction task-108 formalises);
* **port-shaped** — ``append_message`` returns the durable message id and
  ``list_messages`` returns plain ``{"role", "content"}`` dicts;
* **durable and inspectable** — one row per message, append-only, with the
  creation timestamp kept for audit; nothing is overwritten.

The adapter owns its own table (``conversation_messages``) and never touches the
legacy ``conversation_history`` table, so both can coexist during the migration
described in ``docs/DECISION_LOG.md`` (ADR "conversation-store adapter").

Usage::

    store = SqliteConversationStore("data/app.sqlite")
    message_id = await store.append_message("tg:12345", "user", "سلام")
    history = await store.list_messages("tg:12345")
    await store.aclose()

or as an async context manager::

    async with SqliteConversationStore(path) as store:
        ...
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import aiosqlite

log = logging.getLogger(__name__)

#: Roles that mean something downstream. Anything else is rejected rather than
#: persisted, because a typo here silently corrupts the model's prompt later.
ALLOWED_ROLES: frozenset[str] = frozenset(
    {"user", "assistant", "system", "tool", "function", "developer"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_thread_seq "
    "ON conversation_messages(thread_id, seq)"
)


class SqliteConversationStore:
    """Append-only, async SQLite implementation of ``ConversationStorePort``."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        default_limit: int = 50,
        max_content_length: int = 200_000,
    ) -> None:
        self._path = Path(db_path)
        self._default_limit = max(1, default_limit)
        self._max_content_length = max(1, max_content_length)
        self._conn: aiosqlite.Connection | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def connect(self) -> aiosqlite.Connection:
        """Open (once) and return the underlying connection, creating the schema."""
        if self._conn is not None:
            return self._conn
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._path))
        conn.row_factory = aiosqlite.Row
        await conn.execute(_SCHEMA)
        await conn.execute(_INDEX)
        await conn.commit()
        self._conn = conn
        return conn

    async def aclose(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def __aenter__(self) -> SqliteConversationStore:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ── ConversationStorePort ──────────────────────────────────────────────

    async def append_message(self, thread_id: str, role: str, content: str) -> str:
        """Persist one message and return its durable id.

        Raises ``ValueError`` for empty ids/roles, an unknown role, or content
        that is not a string — failing loudly here is cheaper than discovering
        a corrupted transcript when the model is prompted with it.
        """
        canonical_role = self._validate(thread_id, role, content)
        conn = await self.connect()
        message_id = uuid4().hex
        async with conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM conversation_messages WHERE thread_id = ?",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        seq = int(row[0]) if row is not None else 1
        await conn.execute(
            "INSERT INTO conversation_messages "
            "(message_id, thread_id, role, content, seq, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_id,
                thread_id,
                canonical_role,
                content,
                seq,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await conn.commit()
        log.debug(
            "conversation_message_appended thread=%s role=%s seq=%s",
            thread_id,
            canonical_role,
            seq,
        )
        return message_id

    async def list_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> Sequence[dict[str, str]]:
        """Return the thread's messages oldest-first, capped at ``limit``.

        The cap keeps the *most recent* messages (that is what a prompt needs)
        while still returning them in chronological order.
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        effective_limit = self._default_limit if limit is None else max(1, limit)
        conn = await self.connect()
        async with conn.execute(
            "SELECT message_id, role, content, created_at FROM ("
            "  SELECT message_id, role, content, created_at, seq"
            "  FROM conversation_messages WHERE thread_id = ?"
            "  ORDER BY seq DESC LIMIT ?"
            ") ORDER BY seq ASC",
            (thread_id, effective_limit),
        ) as cursor:
            rows: list[Any] = list(await cursor.fetchall())
        return [
            {
                "message_id": str(row["message_id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # ── operations support ─────────────────────────────────────────────────

    async def clear_thread(self, thread_id: str) -> int:
        """Delete a thread's history (``/reset``); returns the rows removed."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        conn = await self.connect()
        cursor = await conn.execute(
            "DELETE FROM conversation_messages WHERE thread_id = ?", (thread_id,)
        )
        await conn.commit()
        removed = cursor.rowcount if cursor.rowcount is not None else 0
        return int(removed)

    async def threads(self) -> Sequence[str]:
        """Distinct thread ids that hold at least one message (audit support)."""
        conn = await self.connect()
        async with conn.execute(
            "SELECT DISTINCT thread_id FROM conversation_messages ORDER BY thread_id"
        ) as cursor:
            return [str(row["thread_id"]) for row in await cursor.fetchall()]

    async def count(self, thread_id: str) -> int:
        """Number of stored messages for ``thread_id``."""
        conn = await self.connect()
        async with conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE thread_id = ?", (thread_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def iter_threads(self) -> AsyncIterator[str]:
        """Yield thread ids one at a time (memory-safe for large installs)."""
        for thread_id in await self.threads():
            yield thread_id

    # ── validation ─────────────────────────────────────────────────────────

    def _validate(self, thread_id: str, role: str, content: str) -> str:
        # Returns the canonical (stripped, lower-cased) role that gets stored.
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role must be a non-empty string")
        normalized = role.strip().lower()
        if normalized not in ALLOWED_ROLES:
            raise ValueError(
                f"unknown conversation role {role!r}; expected one of {sorted(ALLOWED_ROLES)}"
            )
        if not isinstance(content, str):
            raise TypeError(f"content must be a string, got {type(content).__name__}")
        if len(content) > self._max_content_length:
            raise ValueError(
                f"content is {len(content)} characters, over the {self._max_content_length} limit"
            )
        return normalized
