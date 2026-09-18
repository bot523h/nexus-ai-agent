"""Non-destructive lifecycle instrumentation for checkpoint savers.

The wrapper is a *delegation* layer: every saver operation is forwarded to
the wrapped saver unchanged, and lifecycle recording is attached best-effort.
It subclasses ``BaseCheckpointSaver`` (without initialising it) purely so the
runtime accepts it as a valid checkpointer; the wrapped saver remains the
owner of the connection, the serializer and all storage state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from nexus_ai_agent.domain.policies.lifecycle_health import health_gate_ok

log = logging.getLogger(__name__)
AccessContext = str
nexus_access_context: ContextVar[AccessContext] = ContextVar(
    "nexus_access_context", default="system"
)

# A pending touch older than this is dropped at flush time: an access that
# happened long before the flush is no longer a meaningful "recently used".
DEFAULT_TOUCH_DROP_WINDOW: timedelta = timedelta(hours=24)


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
    """Keep one pending access timestamp per thread and flush explicitly.

    Touches older than ``drop_window`` are dropped (not persisted) at flush
    time — state that outlived the window without being flushed is stale.
    """

    def __init__(self, drop_window: timedelta | None = DEFAULT_TOUCH_DROP_WINDOW) -> None:
        self.pending: dict[str, datetime] = {}
        self._drop_window = drop_window

    def add(self, thread_id: str, accessed_at: datetime) -> None:
        self.pending[thread_id] = accessed_at

    async def flush(self, lifecycle: Any) -> None:
        pending, self.pending = self.pending, {}
        now = datetime.now(timezone.utc)
        dropped = 0
        for thread_id, accessed_at in pending.items():
            if self._drop_window is not None and (now - accessed_at) > self._drop_window:
                dropped += 1
                continue
            try:
                await lifecycle.touch_thread(thread_id, accessed_at=accessed_at)
            except Exception:
                log.warning(
                    "lifecycle touch flush failed", extra={"operation": "touch"}, exc_info=True
                )
        if dropped:
            log.warning(
                "lifecycle touches dropped after window expiry",
                extra={"operation": "touch_drop", "dropped": dropped},
            )


class LifecycleRecordingSaver(BaseCheckpointSaver):
    """Delegate a saver while recording lifecycle metadata best-effort.

    This wrapper never changes the result or failure semantics of the wrapped
    saver. Lifecycle failures are warnings only; destructive operations are
    intentionally not exposed here.

    It subclasses ``BaseCheckpointSaver`` *without* calling its ``__init__``
    so LangGraph's ``isinstance`` validation accepts it; the wrapped saver
    stays the owner of the connection and serializer (``self.serde`` is
    pointed at the wrapped serde so Pregel serialises exactly as it would
    against the raw saver).
    """

    def __init__(
        self,
        saver: Any,
        lifecycle: Any,
        *,
        enabled: bool = True,
        drop_window: timedelta | None = DEFAULT_TOUCH_DROP_WINDOW,
    ) -> None:
        # Deliberately NOT calling super().__init__(): this is a pure
        # delegation layer and must not own storage state or a serde.
        self._saver = saver
        self._lifecycle = lifecycle
        self.enabled = enabled
        self._touches = TouchCoalescer(drop_window=drop_window)
        self._ops = 0
        self._failures = 0
        # Point the serializer at the wrapped saver so with_allowlist() and
        # Pregel serialisation behave exactly as against the raw saver.
        self.serde = self._saver.serde

    @property
    def config_specs(self) -> list:
        return self._saver.config_specs

    async def flush(self) -> None:
        await self._touches.flush(self._lifecycle)

    def flush_sync(self) -> None:
        """Best-effort flush from a synchronous context (e.g. ``atexit``).

        At interpreter shutdown no event loop is running, so a fresh loop is
        used.  If a loop *is* still running the flush is deferred with a
        warning — a blocked atexit handler is worse than a lost touch.
        """
        if not self.enabled or not self._touches.pending:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self.flush())
            except Exception:
                log.warning("lifecycle atexit flush failed", exc_info=True)
        else:
            log.warning("lifecycle flush deferred: event loop still running")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._saver, name)

    def _best_effort(self, operation: Awaitable[Any], metric: str) -> None:
        if not self.enabled:
            _close_quietly(operation)
            return
        if not health_gate_ok(failed=self._failures, total=self._ops):
            # Latched off for the process lifetime; never re-enables itself.
            self.enabled = False
            _close_quietly(operation)
            log.warning(
                "lifecycle disabled by health gate",
                extra={"operation": metric, "failed": self._failures, "total": self._ops},
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _close_quietly(operation)
            log.warning("lifecycle operation skipped", extra={"operation": metric})
            return
        loop.create_task(self._run_best_effort(operation, metric))

    async def _run_best_effort(self, operation: Awaitable[Any], metric: str) -> None:
        self._ops += 1
        outcome = "ok"
        try:
            await operation
        except Exception:
            self._failures += 1
            outcome = "failed"
            log.warning("lifecycle operation failed", extra={"operation": metric}, exc_info=True)
            # Fail-safe latch: the very sample that crosses the threshold
            # disables the lifecycle for the process lifetime.
            if not health_gate_ok(failed=self._failures, total=self._ops):
                self.enabled = False
                log.warning(
                    "lifecycle disabled by health gate",
                    extra={
                        "operation": metric,
                        "failed": self._failures,
                        "total": self._ops,
                    },
                )
        _mirror_metric(metric, outcome)

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

    # ── full delegation: the remaining checkpointer surface ─────────────
    # Every method that ``BaseCheckpointSaver`` defines is overridden here so
    # that name resolution never falls through to the base class (whose
    # virtuals raise ``NotImplementedError``).  Anything *new* upstream adds
    # later still reaches the wrapped saver via ``__getattr__``; the
    # introspective delegation test guards this contract.

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.put_writes(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.list(*args, **kwargs)

    def delete_thread(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.delete_thread(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.prune(*args, **kwargs)

    def copy_thread(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.copy_thread(*args, **kwargs)

    def delete_for_runs(self, *args: Any, **kwargs: Any) -> Any:
        return self._saver.delete_for_runs(*args, **kwargs)

    async def aget(self, config: Any) -> Any:
        return await self._saver.aget(config)

    async def alist(self, *args: Any, **kwargs: Any) -> Any:
        async for item in self._saver.alist(*args, **kwargs):
            yield item

    async def aput_writes(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.aput_writes(*args, **kwargs)

    async def adelete_thread(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.adelete_thread(*args, **kwargs)

    async def aprune(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.aprune(*args, **kwargs)

    async def aget_delta_channel_history(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.aget_delta_channel_history(*args, **kwargs)

    async def acopy_thread(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.acopy_thread(*args, **kwargs)

    async def adelete_for_runs(self, *args: Any, **kwargs: Any) -> Any:
        return await self._saver.adelete_for_runs(*args, **kwargs)

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
        if not self.enabled or nexus_access_context.get() != "user":
            return
        thread_id = _thread_id(config)
        if thread_id:
            self._touches.add(thread_id, datetime.now(timezone.utc))


def _close_quietly(operation: Awaitable[Any]) -> None:
    """Close an un-awaited coroutine so it does not leak a RuntimeWarning."""
    close = getattr(operation, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


def _mirror_metric(metric: str, outcome: str) -> None:
    """Best-effort O1 mirror; a metrics failure must never affect the runtime."""
    try:
        from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

        get_metrics_registry().increment(f"nexus_{metric}_total", labels={"outcome": outcome})
    except Exception:
        log.warning("lifecycle metrics increment failed", exc_info=True)


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
