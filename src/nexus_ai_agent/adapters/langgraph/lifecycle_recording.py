"""Non-destructive lifecycle instrumentation for checkpoint savers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)
AccessContext = str
nexus_access_context: ContextVar[AccessContext] = ContextVar(
    "nexus_access_context", default="system"
)


@asynccontextmanager
async def access_context(value: AccessContext) -> AsyncIterator[None]:
    if value not in {"user", "admin", "system"}:
        raise ValueError("access context must be user, admin, or system")
    token = nexus_access_context.set(value)
    try:
        yield
    finally:
        nexus_access_context.reset(token)


class TouchCoalescer:
    """Keep one pending access timestamp per thread and flush explicitly."""

    def __init__(self) -> None:
        self.pending: dict[str, datetime] = {}

    def add(self, thread_id: str, accessed_at: datetime) -> None:
        self.pending[thread_id] = accessed_at

    async def flush(self, lifecycle: Any) -> None:
        pending, self.pending = self.pending, {}
        for thread_id, accessed_at in pending.items():
            try:
                await lifecycle.touch_thread(thread_id, accessed_at=accessed_at)
            except Exception:
                log.warning(
                    "lifecycle touch flush failed", extra={"operation": "touch"}, exc_info=True
                )


class LifecycleRecordingSaver:
    """Delegate a saver while recording lifecycle metadata best-effort.

    This wrapper never changes the result or failure semantics of the wrapped
    saver. Lifecycle failures are warnings only; destructive operations are
    intentionally not exposed here.
    """

    def __init__(self, saver: Any, lifecycle: Any, *, enabled: bool = True) -> None:
        self._saver = saver
        self._lifecycle = lifecycle
        self.enabled = enabled
        self._touches = TouchCoalescer()

    async def flush(self) -> None:
        await self._touches.flush(self._lifecycle)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._saver, name)

    def _best_effort(self, operation: Awaitable[Any], metric: str) -> None:
        if not self.enabled:
            close = getattr(operation, "close", None)
            if close is not None:
                close()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            close = getattr(operation, "close", None)
            if close is not None:
                close()
            log.warning("lifecycle operation skipped", extra={"operation": metric})
            return
        loop.create_task(self._run_best_effort(operation, metric))

    async def _run_best_effort(self, operation: Awaitable[Any], metric: str) -> None:
        try:
            await operation
        except Exception:
            log.warning("lifecycle operation failed", extra={"operation": metric}, exc_info=True)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        result = self._saver.put(*args, **kwargs)
        self._record_from_result(result, kwargs, "upsert")
        return result

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._saver.aput(*args, **kwargs)
        self._record_from_result(result, kwargs, "upsert")
        await asyncio.sleep(0)
        return result

    def get_tuple(self, config: Any) -> Any:
        result = self._saver.get_tuple(config)
        self._touch(config)
        return result

    async def aget_tuple(self, config: Any) -> Any:
        result = await self._saver.aget_tuple(config)
        self._touch(config)
        await asyncio.sleep(0)
        return result

    def _record_from_result(self, result: Any, kwargs: dict[str, Any], operation: str) -> None:
        thread_id = _thread_id(kwargs) or _thread_id(result)
        checkpoint_id = _checkpoint_id(result)
        if thread_id and checkpoint_id and hasattr(self._lifecycle, "record_checkpoint"):
            self._best_effort(
                self._lifecycle.record_checkpoint(
                    thread_id, checkpoint_id, created_at=datetime.now(timezone.utc)
                ),
                operation,
            )

    def _touch(self, config: Any) -> None:
        if nexus_access_context.get() != "user":
            return
        thread_id = _thread_id(config)
        if thread_id:
            self._touches.add(thread_id, datetime.now(timezone.utc))


def _thread_id(value: Any) -> str | None:
    if isinstance(value, dict):
        configurable = value.get("configurable", value)
        if isinstance(configurable, dict) and configurable.get("thread_id") is not None:
            return str(configurable["thread_id"])
    return None


def _checkpoint_id(value: Any) -> str | None:
    if isinstance(value, dict):
        configurable = value.get("configurable", value)
        if isinstance(configurable, dict) and configurable.get("checkpoint_id") is not None:
            return str(configurable["checkpoint_id"])
    return None
