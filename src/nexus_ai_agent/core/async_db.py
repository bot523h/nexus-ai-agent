"""Async SQLite database utilities — context-managed connections, no leaks.

This module provides the foundation for all async DB operations in v3.1.0+.
It wraps aiosqlite with safe context managers to prevent event-loop blocking
and file-descriptor exhaustion.

Usage:
    db = AsyncDB("data/cache.sqlite")
    await db.execute("INSERT INTO ...", (...,))
    rows = await db.fetchall("SELECT ...")

Why this exists:
- All v3.0.0 SQLite calls were sync (sqlite3.connect) inside async handlers,
  which blocks the event loop and freezes the bot under concurrent load.
- This module migrates everything to aiosqlite with WAL mode + try/finally
  semantics, so concurrent reads scale and connections never leak.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


class AsyncDB:
    """Async SQLite wrapper with safe context-managed connections.

    Each call opens one connection (lightweight on SQLite), enables WAL mode
    for concurrent reads, and guarantees close() via try/finally. Use this
    everywhere instead of `sqlite3.connect()`.
    """

    def __init__(self, db_path: str, *, timeout: float = 10.0) -> None:
        self._db_path = db_path
        self._timeout = timeout
        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> str:
        """Return the underlying database path."""
        return self._db_path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield an aiosqlite connection with WAL mode, guaranteed close."""
        conn = await aiosqlite.connect(self._db_path, timeout=self._timeout)
        try:
            # WAL = Write-Ahead Logging → concurrent readers + one writer
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            await conn.close()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Execute a single statement and commit."""
        async with self.connect() as conn:
            await conn.execute(sql, params)
            await conn.commit()

    async def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        """Execute a statement repeatedly with different params."""
        async with self.connect() as conn:
            await conn.executemany(sql, seq_of_params)
            await conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Any | None:
        """Execute and return the first row (or None)."""
        async with self.connect() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Execute and return all rows."""
        async with self.connect() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def script(self, sql_script: str) -> None:
        """Execute a multi-statement SQL script (used for schema setup)."""
        async with self.connect() as conn:
            await conn.executescript(sql_script)
            await conn.commit()
