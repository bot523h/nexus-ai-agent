from __future__ import annotations

import struct
import sqlite3
from pathlib import Path

from nexus_ai_agent.llm.provider import LLMProvider


class LongTermMemory:
    DIM = 384

    def __init__(self, vector_path: str, llm: LLMProvider) -> None:
        self._path = vector_path
        self._llm = llm
        self._conn: sqlite3.Connection | None = None
        self._use_vec: bool = False

    def _conn_(self) -> sqlite3.Connection:
        """
        Create/open the sqlite store and best-effort enable sqlite-vec.

        This module must be offline-safe:
          - If sqlite-vec can't be loaded, we still store content and allow basic retrieval.
        """
        if self._conn is not None:
            return self._conn

        if self._path == ":memory:":
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)

        try:
            import sqlite_vec  # type: ignore

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
