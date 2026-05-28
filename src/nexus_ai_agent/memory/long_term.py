from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import sqlite_vec

from nexus_ai_agent.llm.provider import LLMProvider


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


class LongTermMemory:
    def __init__(self, vector_path: str, llm: LLMProvider):
        self.vector_path = vector_path
        self.llm = llm
        Path(vector_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(vector_path)
        self._conn.row_factory = sqlite3.Row
        self._vec_enabled = False
        self._init_db()

    def _init_db(self) -> None:
        # Attempt to load sqlite-vec. If extension loading is not available in the runtime,
        # fall back to a pure-SQLite table and do similarity search in Python.
        try:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._vec_enabled = True
        except Exception:
            self._vec_enabled = False

        cur = self._conn.cursor()
        if self._vec_enabled:
            # sqlite-vec virtual table; we store additional metadata columns alongside the vector.
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories
                USING vec0(
                  embedding float[384],
                  thread_id TEXT,
                  text TEXT,
                  metadata TEXT
                );
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  thread_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  embedding_json TEXT NOT NULL,
                  metadata TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_thread ON memories(thread_id);")
        self._conn.commit()

    async def store(self, thread_id: str, text: str, metadata: dict[str, Any] | None = None):
        metadata = metadata or {}
        embedding = await self.llm.embed(text)
        cur = self._conn.cursor()
        if self._vec_enabled:
            cur.execute(
                "INSERT INTO memories(thread_id, text, embedding, metadata) VALUES(?, ?, vec_f32(?), ?)",
                (thread_id, text, json.dumps(embedding), json.dumps(metadata)),
            )
        else:
            cur.execute(
                "INSERT INTO memories(thread_id, text, embedding_json, metadata) VALUES(?, ?, ?, ?)",
                (thread_id, text, json.dumps(embedding), json.dumps(metadata)),
            )
        self._conn.commit()

    async def search(self, thread_id: str, query: str, top_k: int = 3) -> list[str]:
        embedding = await self.llm.embed(query)
        cur = self._conn.cursor()

        if self._vec_enabled:
            rows = cur.execute(
                """
                SELECT text
                FROM memories
                WHERE thread_id = ?
                ORDER BY embedding MATCH vec_f32(?)
                LIMIT ?
                """,
                (thread_id, json.dumps(embedding), top_k),
            ).fetchall()
            return [str(r["text"]) for r in rows]

        # Fallback path: load all per-thread memories and score in Python.
        rows = cur.execute(
            "SELECT text, embedding_json FROM memories WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        scored: list[tuple[float, str]] = []
        for r in rows:
            vec = json.loads(r["embedding_json"])
            scored.append((_cosine(embedding, vec), str(r["text"])))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [t[1] for t in scored[:top_k]]

    async def format_context(self, results: list[str]) -> str:
        if not results:
            return ""
        return "Relevant memories:\n" + "\n".join(results)

