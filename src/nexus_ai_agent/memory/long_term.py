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


def _connect(module: Any, path: str) -> Any:
    """Open one connection from a ``sqlite3``-compatible module."""
    if path == ":memory:":
        return module.connect(":memory:", check_same_thread=False)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return module.connect(path, check_same_thread=False)


def _pysqlite3_module() -> Any | None:
    """The optional bundled-SQLite driver, or ``None`` when it is not installed."""
    try:
        import pysqlite3
    except ImportError:
        return None
    return pysqlite3


def _open_sqlite_connection(path: str) -> tuple[Any, bool]:
    """Open an SQLite connection, preferring standard library sqlite3.

    Returns ``(connection, supports_extension_loading)``. Standard ``sqlite3``
    is preferred; the bundled ``pysqlite3`` build (which compiles SQLite with
    extension loading enabled) is used only when the stdlib connection cannot
    load extensions at all.
    """
    conn = _connect(sqlite3, path)
    if _test_extension_capability(conn):
        return conn, True

    # Standard sqlite3 lacks or fails extension loading; attempt pysqlite3 fallback
    module = _pysqlite3_module()
    if module is None:
        return conn, False
    try:
        fallback_conn = _connect(module, path)
    except Exception:
        # The fallback driver exists but could not open the database; keep the
        # working stdlib connection rather than losing it to a failed retry.
        return conn, False
    conn.close()
    return fallback_conn, _test_extension_capability(fallback_conn)


def _enable_sqlite_vec(conn: Any) -> bool:
    """Best-effort load of the sqlite-vec extension on an extension-capable conn.

    Returns ``True`` only when the extension actually loaded. An absent or
    incompatible extension is a *degraded capability*, never a crash — but it
    is also never reported as success.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
            return True
        finally:
            # Extension loading is disabled again even if ``load`` raised, so
            # the connection is never left in an "extension loading" state.
            conn.enable_load_extension(False)
    except Exception:
        return False


def _is_pysqlite3_connection(conn: Any) -> bool:
    return type(conn).__module__.split(".")[0] == "pysqlite3"


def _is_vec_function_unavailable(exc: Exception) -> bool:
    """True only when the failure is *the vec_* SQL functions are not loaded*.

    A genuine SQL error (missing table, locked database, disk I/O) must never
    be turned into a fake recency result: only the specific "unknown/no such
    function ... vec_*" class may fall back. The check is anchored on the
    ``vec_`` function prefix rather than a single exact phrase so it survives
    SQLite message variations across versions.
    """
    message = str(exc).lower()
    if "vec_" not in message:
        return False
    return "no such function" in message or "unknown function" in message


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

        if can_load_ext and _enable_sqlite_vec(conn):
            self._use_vec = True
        else:
            # The extension is unusable on this connection. If it is the
            # stdlib connection (and the optional bundled driver is present),
            # retry once on pysqlite3 — a compiled SQLite that commonly loads
            # the extension where the stdlib build cannot. A successful retry
            # replaces the connection; otherwise the stdlib connection stays.
            self._use_vec = False
            if can_load_ext and not _is_pysqlite3_connection(conn):
                module = _pysqlite3_module()
                if module is not None:
                    try:
                        retry = _connect(module, self._path)
                    except Exception:
                        retry = None
                    if retry is not None:
                        if _enable_sqlite_vec(retry):
                            conn.close()
                            conn = retry
                            self._use_vec = True
                        else:
                            retry.close()

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
            # Only a *vec function unavailable* failure may degrade to recency;
            # a real SQL error (missing table, lock, I/O) must surface, never be
            # silently turned into a plausible-looking empty/recency result.
            if not _is_vec_function_unavailable(exc):
                raise
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
