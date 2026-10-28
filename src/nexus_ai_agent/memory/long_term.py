from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Any

from nexus_ai_agent.llm.provider import LLMProvider


def _test_extension_capability(conn: Any) -> bool:
    """Test if an SQLite connection supports enable_load_extension safely."""
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        conn.enable_load_extension(True)
    except Exception:
        return False

    try:
        conn.enable_load_extension(False)
        return True
    except Exception:
        # Disabling failed — do not return connection in unknown extension state
        return False


def _open_sqlite_connection(path: str) -> tuple[Any, bool]:
    """Open an SQLite connection, preferring standard library sqlite3.

    Returns (connection, supports_extension_loading).
    """
    if path == ":memory:":
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)

    if _test_extension_capability(conn):
        return conn, True

    # Standard sqlite3 lacks or fails extension loading; attempt pysqlite3 fallback
    try:
        import pysqlite3

        conn.close()
        if path == ":memory:":
            fallback_conn = pysqlite3.connect(":memory:", check_same_thread=False)
        else:
            fallback_conn = pysqlite3.connect(path, check_same_thread=False)

        if _test_extension_capability(fallback_conn):
            return fallback_conn, True
        return fallback_conn, False
    except ImportError:
        return conn, False

def _is_real_user_id(user_id: object) -> bool:
    """A personal principal is a positive, non-bool integer — never a group id."""
    return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0


def memory_scope_id(user_id: int | None) -> str | None:
    """Return the deterministic personal-memory scope for a user.

    The scope is stable across requests (``mem:u:<id>``) and belongs to exactly
    one user — it is never a shared or group scope.  A missing or ambiguous
    identity (``None``, ``0``, a boolean, a non-integer, or a negative group
    id) returns ``None``, which callers must treat as *fail closed*: no personal
    read, no personal write, no shared fallback.
    """
    if not _is_real_user_id(user_id):
        return None
    return f"mem:u:{user_id}"


def is_private_audience(chat_id: int | None, user_id: int | None) -> bool:
    """True only for a provable 1:1 chat (Telegram private chat_id == user_id).

    Group and unknown audiences are not private: the prompt context is shared
    by every member of a group, so personal memory must never be injected
    there (most restrictive safe default).  A missing or ambiguous identity is
    never a private audience.
    """
    return _is_real_user_id(chat_id) and _is_real_user_id(user_id) and chat_id == user_id


class LongTermMemory:
    DIM = 384

    def __init__(self, vector_path: str, llm: LLMProvider) -> None:
        self._path = vector_path
        self._llm = llm
        self._conn: Any | None = None
        self._use_vec: bool = False

    def _conn_(self) -> Any:
        """Create/open the sqlite store and best-effort enable sqlite-vec.

        This module must be offline-safe:
          - If sqlite-vec can't be loaded, we still store content and allow basic retrieval.
        """
        if self._conn is not None:
            return self._conn

        conn, can_load_ext = _open_sqlite_connection(self._path)

        if can_load_ext:
            try:
                import sqlite_vec

                conn.enable_load_extension(True)
                try:
                    sqlite_vec.load(conn)
                    self._use_vec = True
                finally:
                    conn.enable_load_extension(False)
            except (ImportError, sqlite3.OperationalError, AttributeError):
                self._use_vec = False
            except Exception as exc:
                err_msg = str(exc).lower()
                if "sqlite_vec" in err_msg or "extension" in err_msg or "symbol" in err_msg:
                    self._use_vec = False
                else:
                    raise
        else:
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
        except Exception as exc:
            err_msg = str(exc).lower()
            if (
                "no such function: vec_distance_cosine" in err_msg
                or "unknown function: vec_distance_cosine" in err_msg
            ):
                rows = conn.execute(
                    "SELECT content FROM memories WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                    (thread_id, top_k),
                ).fetchall()
                return [r[0] for r in rows]
            raise

    async def format_context(self, results: list[str]) -> str:
        if not results:
            return ""
        joined = "\n- ".join(results)
        return f"Relevant memories:\n- {joined}"
