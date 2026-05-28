from __future__ import annotations

import sqlite3
from pathlib import Path

from nexus_ai_agent.llm.provider import LLMProvider


class LongTermMemory:
    def __init__(self, vector_path: str, llm: LLMProvider):
        self._path = vector_path
        self._llm = llm
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            import sqlite_vec

            conn = sqlite3.connect(self._path)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL
                )
            """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS 
                idx_memories_thread ON memories(thread_id)
            """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    async def store(
        self,
        thread_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        _ = metadata
        embedding = await self._llm.embed(text)
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = self._get_conn()
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
        embedding = await self._llm.embed(query)
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT content FROM memories
            WHERE thread_id = ?
            ORDER BY vec_distance_cosine(embedding, ?) ASC
            LIMIT ?
            """,
            (thread_id, blob, top_k),
        ).fetchall()
        return [r[0] for r in rows]

    async def format_context(self, results: list[str]) -> str:
        if not results:
            return ""
        joined = "\n- ".join(results)
        return f"Relevant memories:\n- {joined}"
