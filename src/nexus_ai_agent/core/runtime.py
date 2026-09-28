"""Application runtime lifecycle — canonical ownership of all resources.

W1 (True Runtime Closure) — Production/World-Class implementation.

Design principles (15 golden laws):
- Live Evidence > Previous Reports
- One Runtime → One Provider Identity
- One Runtime → One DB Ownership Boundary
- Failure must not strand resources
- Cancellation is first-class, must join cleanup
- Idempotent + re-entrant safe

This module is the ONE CANONICAL LIFECYCLE authority — webhook and polling both
consume it; nothing constructs or tears down engines behind its back.

- build_runtime is implicit via build_application -> _init_v2_engines
- shutdown is wired as post_shutdown hook and runs every registered cleanup
  in LIFO order, fail-safe, shielded, joining inner tasks even on CancelledError
- Webhook path explicitly calls post_init/post_shutdown because PTB
  initialize()/shutdown() do NOT call them (only run_polling/run_webhook do)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

CleanupStep = Callable[["Runtime"], Awaitable[None]]


@dataclass
class Runtime:
    """Canonical application runtime container.

    Holds every long-lived resource the bot process owns. Callers MUST NOT
    construct engines outside of build_application and MUST NOT close them
    directly — always go through add_cleanup so shutdown ordering stays in
    one place.

    Thread-safety: shutdown is protected by an async lock + event for
    re-entrant calls. Idempotent: second call is no-op.
    """

    settings: Any
    engines: dict[str, Any] = field(default_factory=dict)
    _cleanup_steps: list[CleanupStep] = field(default_factory=list)
    _shutting_down: bool = False
    _shutdown_completed: bool = False
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add_cleanup(self, step: CleanupStep) -> None:
        """Register a cleanup step. Steps run LIFO on shutdown."""
        self._cleanup_steps.append(step)

    async def shutdown(self) -> None:
        """Run every registered cleanup step, in LIFO order, fail-safe.

        - Idempotent
        - Re-entrant: concurrent call waits for in-flight shutdown (2.5s timeout)
        - Each step is shielded and joined even on CancelledError
        - One step raising never prevents later steps
        - Global DB engines disposed shielded as well
        """
        if self._shutdown_completed:
            return

        # Fast path for re-entrant call while shutdown in progress
        if self._shutting_down:
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                logger.warning("runtime_shutdown_reentrant_timeout")
            return

        async with self._shutdown_lock:
            if self._shutdown_completed:
                return
            if self._shutting_down:
                # Double-check after acquiring lock
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=2.5)
                except asyncio.TimeoutError:
                    logger.warning("runtime_shutdown_reentrant_timeout")
                return

            self._shutting_down = True

            steps = list(reversed(self._cleanup_steps))
            self._cleanup_steps.clear()

            for i, step in enumerate(steps):
                # Each step is run in its own task so we can shield and join
                task: asyncio.Task[None] = asyncio.create_task(step(self))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Outer task cancelled — shield protected inner, now join inner
                    # to ensure resource is actually released before moving on
                    logger.warning(
                        "runtime_cleanup_step_cancelled",
                        step_index=i,
                        total_steps=len(steps),
                    )
                    try:
                        await task
                    except asyncio.CancelledError:
                        # Inner itself raised CancelledError — still considered
                        # cleaned, but logged
                        logger.warning(
                            "runtime_cleanup_step_cancelled_inner",
                            step_index=i,
                            total_steps=len(steps),
                        )
                    except Exception:
                        logger.exception(
                            "runtime_cleanup_step_failed_after_cancel",
                            step_index=i,
                            total_steps=len(steps),
                        )
                except Exception:
                    logger.exception(
                        "runtime_cleanup_step_failed",
                        step_index=i,
                        total_steps=len(steps),
                    )
            # Global DB engines — must also be shielded and joined
            dispose_task = asyncio.create_task(_dispose_global_db_engines())
            try:
                await asyncio.shield(dispose_task)
            except asyncio.CancelledError:
                logger.warning("runtime_global_dispose_cancelled")
                try:
                    await dispose_task
                except asyncio.CancelledError:
                    logger.warning("runtime_global_dispose_cancelled_inner")
                except Exception:
                    logger.exception("runtime_global_db_dispose_failed_after_cancel")
            except Exception:
                logger.exception("runtime_global_db_dispose_failed")

            self._shutdown_completed = True
            self._shutdown_event.set()
            self._shutting_down = False
            logger.info("runtime_shutdown_complete", total_steps=len(steps))


async def _dispose_global_db_engines() -> None:
    """Dispose module-level async engines cached in storage.db.

    storage.db owns a process-global SQLite engine (_engine), a retired list
    (_replaced_engines) and a PG cache (_pg_engines). None were closed in
    old shutdown path — leaked pooled aiosqlite and PG connections.
    """
    from nexus_ai_agent.storage import db as db_module

    while db_module._replaced_engines:
        engine = db_module._replaced_engines.pop()
        try:
            await engine.dispose()
        except Exception:
            logger.exception("runtime_dispose_replaced_engine_failed")

    if db_module._engine is not None:
        try:
            await db_module._engine.dispose()
        except Exception:
            logger.exception("runtime_dispose_sqlite_engine_failed")
        db_module._engine = None
        db_module._session_factory = None
        db_module._engine_path = None

    for url, engine in list(db_module._pg_engines.items()):
        try:
            await engine.dispose()
        except Exception:
            logger.exception("runtime_dispose_pg_engine_failed", url=url)
    db_module._pg_engines.clear()
    db_module._pg_session_factories.clear()
    db_module._pg_prepared_urls.clear()
    db_module._initialized_paths.clear()


def shutdown_module_sync_engines() -> None:
    """Dispose lru_cache-cached sync engines in force_join/anonymous_chat.

    Called from worker thread during shutdown — must be thread-safe.
    """
    try:
        from nexus_ai_agent.features import anonymous_chat, force_join

        for mod in (force_join, anonymous_chat):
            closer = getattr(mod, "_shutdown_engines", None)
            if callable(closer):
                closer()
    except Exception:
        logger.exception("runtime_shutdown_sync_engines_failed")
