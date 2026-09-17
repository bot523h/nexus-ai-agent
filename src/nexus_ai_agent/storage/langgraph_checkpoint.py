from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import psycopg
from langgraph.checkpoint.sqlite import SqliteSaver

from nexus_ai_agent.storage.db import normalize_database_url, resolve_database_url

# Factory returning the underlying saver plus the pool that owns its
# connection (``None`` when the saver manages its own connection).
SaverFactory = Callable[[], Awaitable[tuple[Any, Any | None]]]

# Construction retry policy (exponential backoff: base_delay * 2**attempt).
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BASE_DELAY = 0.5


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


class PostgresCheckpointer:
    """Lazy, self-healing LangGraph checkpointer backed by PostgreSQL.

    The psycopg connection pool and the underlying ``AsyncPostgresSaver`` are
    created on first use, inside the process event loop (psycopg pools must be
    built from a running loop).  Creation is attempted up to ``max_attempts``
    times with exponential backoff, after which ``saver.setup()`` is awaited
    explicitly.  If a live operation hits a connection-level psycopg error
    (``OperationalError`` / ``InterfaceError``) the old pool is closed via
    ``reset()`` and the operation is retried exactly once; any other error is
    re-raised immediately (no retry).
    """

    def __init__(
        self,
        database_url: str,
        *,
        saver_factory: SaverFactory | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        base_delay: float = _DEFAULT_BASE_DELAY,
    ) -> None:
        self._database_url = normalize_database_url(database_url)
        self._saver_factory = saver_factory
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._saver: Any | None = None
        self._pool: Any | None = None

    @property
    def database_url(self) -> str:
        """The normalized PostgreSQL URL this checkpointer targets."""
        return self._database_url

    # ── lifecycle ────────────────────────────────────────────────────────

    async def _create_saver(self) -> tuple[Any, Any | None]:
        """Build one (saver, pool) pair, via the injected or default factory."""
        if self._saver_factory is not None:
            return await self._saver_factory()
        return await _default_saver_factory(self._database_url)

    async def _ensure_saver(self) -> None:
        """Create the pool + saver lazily, in the running event loop.

        Retries connection-level failures up to ``max_attempts`` times with
        exponential backoff.  Non-connection errors are raised immediately.
        """
        if self._saver is not None:
            return
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            pool: Any | None = None
            try:
                saver, pool = await self._create_saver()
                await saver.setup()
                self._saver = saver
                self._pool = pool
                return
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                last_error = exc
                await self._close_quietly(pool)
                if attempt + 1 < self._max_attempts:
                    await asyncio.sleep(self._base_delay * (2**attempt))
            except Exception:
                await self._close_quietly(pool)
                raise
        raise RuntimeError(
            f"could not create the Postgres checkpointer after "
            f"{self._max_attempts} attempts: {last_error}"
        ) from last_error

    async def warmup(self) -> None:
        """Build the pool and checkpoint tables eagerly at startup.

        Call once from an async context (e.g. bot startup) so the first graph
        run does not pay the connection-setup cost.
        """
        await self._ensure_saver()

    async def reset(self) -> None:
        """Close the current pool (if any) and drop the saver.

        The next operation rebuilds everything lazily.
        """
        self._saver = None
        pool, self._pool = self._pool, None
        await self._close_quietly(pool)

    async def _close_quietly(self, pool: Any | None) -> None:
        if pool is None:
            return
        try:
            result = pool.close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Closing a broken pool must never mask the original error.
            pass

    # ── operations ───────────────────────────────────────────────────────

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        await self._ensure_saver()
        assert self._saver is not None
        result = getattr(self._saver, method_name)(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _call_with_reconnect(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._call(method_name, *args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            await self.reset()
            return await self._call(method_name, *args, **kwargs)

    async def aget_tuple(self, config: Any) -> Any:
        return await self._call_with_reconnect("aget_tuple", config)

    async def aget(self, config: Any) -> Any:
        return await self._call_with_reconnect("aget", config)

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_reconnect("aput", *args, **kwargs)

    async def aput_writes(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_reconnect("aput_writes", *args, **kwargs)

    async def adelete_thread(self, *args: Any, **kwargs: Any) -> None:
        await self._call_with_reconnect("adelete_thread", *args, **kwargs)

    async def aprune(self, *args: Any, **kwargs: Any) -> None:
        await self._call_with_reconnect("aprune", *args, **kwargs)

    async def aget_delta_channel_history(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_reconnect("aget_delta_channel_history", *args, **kwargs)

    async def alist(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            iterator = await self._call("alist", *args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            await self.reset()
            iterator = await self._call("alist", *args, **kwargs)
        async for item in iterator:
            yield item


async def _default_saver_factory(database_url: str) -> tuple[Any, Any]:
    """Build the real psycopg pool + AsyncPostgresSaver (imported lazily)."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=database_url,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict},
        min_size=1,
        max_size=10,
    )
    opened = pool.open(wait=False)
    if inspect.isawaitable(opened):
        await opened
    # row_factory=dict (above) makes the pool hand out dict-row connections,
    # which is the connection type AsyncPostgresSaver's type signature wants.
    saver = AsyncPostgresSaver(
        conn=cast("AsyncConnectionPool[AsyncConnection[dict[str, Any]]]", pool)
    )
    return saver, pool


def get_checkpointer(path: str) -> AsyncCompatibleSqliteSaver | PostgresCheckpointer:
    """Return the LangGraph checkpointer for this process (sync, as before).

    When ``NEXUS_DATABASE_URL`` is configured the checkpoints live in
    PostgreSQL (e.g. Neon) behind a lazy :class:`PostgresCheckpointer`;
    otherwise the SQLite checkpointer is returned, exactly as before.
    """
    database_url = resolve_database_url()
    if database_url is not None:
        return PostgresCheckpointer(database_url)

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
