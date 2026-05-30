from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver


class AsyncCompatibleSqliteSaver(SqliteSaver):
    """
    LangGraph's async graph execution expects async checkpointer methods.
    The upstream SqliteSaver is sync-only; its async methods raise.

    This subclass implements the async methods by delegating to the sync
    implementation in a worker thread. This keeps the API stable across
    LangGraph versions and avoids requiring AsyncSqliteSaver's context manager
    lifecycle in CLIs/tests.
    """

    async def aget_tuple(self, config: Any) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aget(self, config: Any) -> Any:
        return await asyncio.to_thread(self.get, config)

    async def alist(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        items = await asyncio.to_thread(lambda: list(self.list(*args, **kwargs)))
        for item in items:
            yield item

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.put, *args, **kwargs)

    async def aput_writes(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.put_writes, *args, **kwargs)

    async def adelete_thread(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.delete_thread, *args, **kwargs)

    async def aprune(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.prune, *args, **kwargs)

    async def aget_delta_channel_history(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.get_delta_channel_history, *args, **kwargs)


def get_checkpointer(path: str) -> AsyncCompatibleSqliteSaver:
    # Ensure parent directories exist for file-backed DBs.
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # In-memory DBs don't support WAL.
        pass
    return AsyncCompatibleSqliteSaver(conn)
