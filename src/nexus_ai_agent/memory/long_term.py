from __future__ import annotations

import asyncio
import sqlite3
import struct
from pathlib import Path

from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


class LongTermMemory:
    """Legacy vector memory — marked as legacy attack surface until replaced by Memory Trust.

    W1 Fix: owns sqlite3.Connection that must be closed deterministically.
    W3: This class bypasses owner isolation and provenance. New code must use
    memory.trust.MemoryTrustService. This class remains for backward compat
    but is explicitly marked LEGACY.
    """

    DIM = 384
    IS_LEGACY = True  # Law 13: legacy path explicit

    def __init__(self, vector_path: str, llm: LLMProvider) -> None:
        self._path = vector_path
        self._llm = llm
        self._conn: sqlite3.Connection | None = None
        self._use_vec: bool = False
        self._closed = False

    def _conn_(self) -> sqlite3.Connection:
        """
        Create/open the sqlite store and best-effort enable sqlite-vec.

        This module must be offline-safe:
          - If sqlite-vec can't be loaded, we still store content and allow basic retrieval.
        """
        if self._closed:
            raise RuntimeError("LongTermMemory closed")
        if self._conn is not None:
            return self._conn

        if self._path == ":memory:":
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)

        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._use_vec = True
        except Exception:
            self._use_vec = False

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_thread
            ON memories(thread_id)
            """
        )
        conn.commit()

        self._conn = conn
        return conn

    async def store(
        self,
        thread_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        _ = metadata
        embedding = await self._llm.embed(text)
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = self._conn_()
        conn.execute(
            "INSERT INTO memories (thread_id, content, embedding) VALUES (?,?,?)",
            (thread_id, text, blob),
        )
        conn.commit()

    async def search(
        self,
        thread_id: str,
        query: str,
        top_k: int = 3,
    ) -> list[str]:
        conn = self._conn_()

        # Fallback: simple recency-based retrieval if vec unavailable.
        if not self._use_vec:
            rows = conn.execute(
                "SELECT content FROM memories WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, top_k),
            ).fetchall()
            return [r[0] for r in rows]

        embedding = await self._llm.embed(query)
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        try:
            rows = conn.execute(
                """
                SELECT content FROM memories
                WHERE thread_id=?
                ORDER BY vec_distance_cosine(embedding, ?) ASC
                LIMIT ?
                """,
                (thread_id, blob, top_k),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            rows = conn.execute(
                "SELECT content FROM memories WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, top_k),
            ).fetchall()
            return [r[0] for r in rows]

    async def format_context(self, results: list[str]) -> str:
        if not results:
            return ""
        joined = "\n- ".join(results)
        return f"Relevant memories:\n- {joined}"

    async def aclose(self) -> None:
        """W1 True Runtime Closure: close sqlite3 connection in thread (blocking)."""
        if self._closed:
            return
        self._closed = True
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close)
            except Exception:
                logger.warning("long_term_memory_close_failed", exc_info=True)

    def close(self) -> None:
        """Sync best-effort close (legacy)."""
        if self._closed:
            return
        self._closed = True
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
