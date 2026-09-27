"""Canonical runtime composition (W1).

This module owns the ONE place where the application's stateful runtime
components are constructed:

* settings snapshot (the same object every adopter sees),
* the canonical database identity (env ``NEXUS_DATABASE_URL`` for
  PostgreSQL, otherwise ``settings.db_path`` for SQLite — decided once,
  auditable via :class:`DatabaseIdentity`),
* the Gemini request queue, engine, provider and summarizer,
* the persistent conversation store,
* ordered lifecycle shutdown (:meth:`RuntimeContext.aclose`).

Why: before W1, ``bot/app.py`` and ``bot/handlers.py`` each built their own
GeminiEngine/SummarizerEngine independently, ``AIMemoryEngine``/agents/
knowledge handlers spawned queue-less private providers, and nothing closed
the queue or job queue at shutdown — multiple split-brains and a silent
lifecycle leak.  The constructor-ownership ratchet
(``tests/architecture/test_constructor_ownership.py``) pins every
construction site; direct construction outside this factory shrinks to the
explicitly listed legacy fallback seams (each of which warns loudly).

Out of scope (honest residuals, recorded for later waves):
* the typed/policy LLM gateway contract (W2),
* knowledge/ zone internals (active claim by another agent),
* ``ConversationStore`` remains SQLite-file based even when the canonical
  backend is PostgreSQL (historical behavior preserved).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.conversation_store import ConversationStore
from nexus_ai_agent.features.request_queue import GeminiRequestQueue
from nexus_ai_agent.features.summarizer import SummarizerEngine
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import normalize_database_url, resolve_database_url

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Database identity — exactly one canonical decision
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DatabaseIdentity:
    """The single, auditable record of which database the application uses."""

    backend: str  # "sqlite" | "postgres"
    dsn_raw: str  # value to open with (path or URL — may contain credentials)
    dsn_normalized: str  # normalized comparison key
    source: str  # "env:NEXUS_DATABASE_URL" | "settings.db_path" | "default"

    def describe(self) -> str:
        """Human-loggable description that NEVER contains credentials."""

        if self.backend == "postgres":
            host = self.dsn_normalized.split("@")[-1]
            return f"postgres://<redacted>@{host}"
        return f"sqlite:///{self.dsn_raw}"


def resolve_database_identity(settings: Settings) -> DatabaseIdentity:
    """Decide the database backend exactly once.

    Precedence: ``NEXUS_DATABASE_URL``/``DATABASE_URL`` (non-blank) →
    PostgreSQL; otherwise SQLite at ``settings.db_path`` (which callers may
    customise through ``NEXUS_DB_PATH``; the field default is the historical
    ``data/app.sqlite`` and MUST remain so).
    """

    url = resolve_database_url()
    if url is not None:
        normalized = normalize_database_url(url)
        return DatabaseIdentity(
            backend="postgres",
            dsn_raw=url,
            dsn_normalized=normalized,
            source="env:NEXUS_DATABASE_URL",
        )
    db_path = settings.db_path
    source = "settings.db_path" if db_path != "data/app.sqlite" else "default"
    return DatabaseIdentity(
        backend="sqlite",
        dsn_raw=db_path,
        dsn_normalized=db_path,
        source=source,
    )


# ═══════════════════════════════════════════════════════════════════════════
# RuntimeContext
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RuntimeContext:
    """One owner for the stateful runtime.  See module docstring."""

    settings: Settings
    db: DatabaseIdentity
    request_queue: GeminiRequestQueue
    conversation_store: ConversationStore
    gemini_engine: GeminiEngine | None
    llm_provider: GeminiProvider | None
    summarizer_engine: SummarizerEngine | None
    # Late-bound after the application builds the infrastructure adapters.
    job_queue: Any | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        """Run the ordered shutdown, exactly once.

        Order: settle the LLM queue first (pending waiters get their
        explicit cancellation), then the job queue, then release storage
        (the conversation store's SQLite engine).  Every component is
        guarded: shutdown must surface other failures but never abort the
        remaining cleanup, and a second call is a no-op.
        """

        if self._closed:
            return
        self._closed = True

        # 1. LLM request queue — settles every pending waiter.
        try:
            await self.request_queue.close()
        except Exception:  # noqa: BLE001 — shutdown continues regardless
            log.exception("runtime_close_queue_failed")

        # 2. Job queue — stops worker retention of new jobs.
        job_queue = self.job_queue
        if job_queue is not None:
            try:
                shutdown = getattr(job_queue, "shutdown", None)
                if shutdown is not None:
                    await shutdown()
            except Exception:  # noqa: BLE001
                log.exception("runtime_close_job_queue_failed")

        # 3. Storage — the conversation store's SQLite engine.
        # ConversationStore has no public close() yet (shrink-only seam for
        # the next wave); the runtime, as its owner, releases the engine.
        try:
            engine = getattr(self.conversation_store, "_engine", None)
            if engine is not None:
                engine.dispose()
        except Exception:  # noqa: BLE001
            log.exception("runtime_close_store_failed")

        log.info(
            "runtime_closed",
            extra={"db_backend": self.db.backend, "db_source": self.db.source},
        )


def build_runtime(settings: Settings) -> RuntimeContext:
    """The approved factory.  Every construction site lives HERE.

    Each call returns an independent runtime (tests may own several); within
    one runtime, all LLM/DB components are shared — the engine wraps the
    queue and store, and the provider wraps the same engine.
    """

    db = resolve_database_identity(settings)
    log.info("runtime_db_identity", extra={"identity": db.describe(), "source": db.source})

    # Persistent conversation store: historically SQLite at settings.db_path
    # regardless of the canonical backend (PostgreSQL deployments included) —
    # preserved verbatim, documented residual.
    conversation_store = ConversationStore(db_path=settings.db_path)

    # Fair FIFO request queue feeding the Gemini engine.
    request_queue = GeminiRequestQueue(
        max_rpm=settings.gemini_max_rpm,
        max_daily=settings.gemini_max_daily,
    )

    gemini_engine: GeminiEngine | None = None
    llm_provider: GeminiProvider | None = None
    summarizer_engine: SummarizerEngine | None = None
    if settings.gemini_api_key:
        gemini_engine = GeminiEngine(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_rpm=settings.gemini_max_rpm,
            max_daily=settings.gemini_max_daily,
            conversation_store=conversation_store,
            request_queue=request_queue,
        )
        llm_provider = GeminiProvider(engine=gemini_engine)
        summarizer_engine = SummarizerEngine(
            gemini_api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
    else:
        log.info(
            "runtime_gemini_unconfigured",
            extra={"components": "engine/provider/summarizer skipped"},
        )

    return RuntimeContext(
        settings=settings,
        db=db,
        request_queue=request_queue,
        conversation_store=conversation_store,
        gemini_engine=gemini_engine,
        llm_provider=llm_provider,
        summarizer_engine=summarizer_engine,
    )
