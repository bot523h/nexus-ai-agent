"""Application runtime lifecycle — canonical ownership of all resources.

W1 (True Runtime Closure): every resource the runtime creates MUST have a
single owner and a deterministic shutdown path.  This module is the
ONE CANONICAL LIFECYCLE authority — webhook and polling both consume it;
nothing constructs or tears down engines behind its back.

Design:
* :func:`build_runtime` is called by ``build_application`` to construct
  all engines exactly once and attach the resulting :class:`Runtime` to
  the application.
* :func:`shutdown_runtime` is wired as the PTB ``post_shutdown`` hook and
  runs **every** registered cleanup step in LIFO order.  One step raising
  is logged but never prevents later steps from running (fail-safe
  shutdown — Law 6).
* The webhook server path (:mod:`nexus_ai_agent.bot.webhook`) no longer
  duplicates ``resume_pending`` — PTB's ``post_init`` is the single
  authority for that step.

Concurrency / cancellation:
* ``shutdown_runtime`` is idempotent: calling it twice is a no-op.
* Each cleanup step is shielded from cancellation so a single
  ``CancelledError`` during step N cannot abandon steps N+1.. (a partial
  shutdown must still release the remaining resources).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: Type of a single cleanup step.  Steps receive the Runtime; they must
#: not raise.  Cleanup steps are registered in construction order and
#: invoked in reverse (LIFO) so dependencies are torn down before the
#: things they depend on.
CleanupStep = Callable[["Runtime"], Awaitable[None]]


@dataclass
class Runtime:
    """Canonical application runtime container.

    Holds every long-lived resource the bot process owns.  Callers MUST
    NOT construct engines outside of :func:`build_runtime` and MUST NOT
    close them directly — always go through :func:`add_cleanup` so the
    shutdown ordering stays in one place.
    """

    settings: Any
    engines: dict[str, Any] = field(default_factory=dict)
    _cleanup_steps: list[CleanupStep] = field(default_factory=list)
    _shutting_down: bool = False
    _shutdown_completed: bool = False

    # -- lifecycle registration -------------------------------------------------

    def add_cleanup(self, step: CleanupStep) -> None:
        """Register a cleanup step.  Steps run LIFO on shutdown."""
        self._cleanup_steps.append(step)

    # -- shutdown ---------------------------------------------------------------

    async def shutdown(self) -> None:
        """Run every registered cleanup step, in LIFO order, fail-safe.

        Idempotent: subsequent calls are no-ops.  Each step is shielded
        from cancellation, and an exception in one step is logged but
        does NOT prevent later steps from running.  This is W1 Law 6
        ("FAILURE MUST NOT STRAND RESOURCES").
        """
        if self._shutdown_completed:
            return
        if self._shutting_down:
            # Re-entrant call while shutdown is in progress: wait for
            # the in-flight shutdown to finish instead of racing it.
            for _ in range(50):
                await asyncio.sleep(0.05)
                if self._shutdown_completed:
                    return
            logger.warning("runtime_shutdown_reentrant_timeout")
            return
        self._shutting_down = True

        steps = list(reversed(self._cleanup_steps))
        self._cleanup_steps.clear()
        for i, step in enumerate(steps):
            try:
                # Shield each step so a CancelledError arriving mid-step
                # cannot strand later resources.
                await asyncio.shield(step(self))
            except asyncio.CancelledError:
                # Even a shielded CancelledError means the caller asked
                # for cancellation; but we still continue the cleanup
                # chain (resources must be released before surfacing it).
                logger.warning(
                    "runtime_cleanup_step_cancelled",
                    step_index=i,
                    total_steps=len(steps),
                )
            except Exception:  # noqa: BLE001 - fail-safe shutdown
                logger.exception(
                    "runtime_cleanup_step_failed",
                    step_index=i,
                    total_steps=len(steps),
                )

        # Dispose module-global database engines that the runtime
        # consumed via storage.db.  These are global singletons that
        # pre-date the Runtime container; we register their disposal
        # as a belt-and-braces step here so no async engine leaks on
        # shutdown.
        try:
            await _dispose_global_db_engines()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_global_db_dispose_failed")

        self._shutdown_completed = True
        self._shutting_down = False
        logger.info("runtime_shutdown_complete", total_steps=len(steps))


# ---------------------------------------------------------------------------
# Global DB engine disposal
# ---------------------------------------------------------------------------


async def _dispose_global_db_engines() -> None:
    """Dispose the module-level async engines cached in ``storage.db``.

    ``storage.db`` owns a process-global SQLite engine (``_engine``), a
    retired-engine list (``_replaced_engines``) and a PG engine cache
    (``_pg_engines``).  None of these are explicitly closed during the
    current PTB shutdown path — so a clean bot stop leaked pooled aiosqlite
    connections and PG connections.
    """
    from nexus_ai_agent.storage import db as db_module

    # First drain any replaced (retired) engines.
    while db_module._replaced_engines:
        engine = db_module._replaced_engines.pop()
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_dispose_replaced_engine_failed")

    # Dispose the current SQLite engine.
    if db_module._engine is not None:
        try:
            await db_module._engine.dispose()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_dispose_sqlite_engine_failed")
        db_module._engine = None
        db_module._session_factory = None
        db_module._engine_path = None

    # Dispose every cached PG engine.
    for url, engine in list(db_module._pg_engines.items()):
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_dispose_pg_engine_failed", url=url)
    db_module._pg_engines.clear()
    db_module._pg_session_factories.clear()
    db_module._pg_prepared_urls.clear()
    db_module._initialized_paths.clear()


# ---------------------------------------------------------------------------
# Feature-engine shutdown helpers
# ---------------------------------------------------------------------------


def shutdown_module_sync_engines() -> None:
    """Dispose @lru_cache-cached sync engines in force_join/anonymous_chat.

    Both modules expose a module-level ``_shutdown_engines`` helper (added
    as part of W1) that clears the lru_cache and disposes every cached
    engine.  Called from a worker thread during shutdown.
    """
    try:
        from nexus_ai_agent.features import anonymous_chat, force_join

        for mod in (force_join, anonymous_chat):
            closer = getattr(mod, "_shutdown_engines", None)
            if closer is not None:
                closer()
    except Exception:  # noqa: BLE001
        logger.exception("runtime_shutdown_sync_engines_failed")
