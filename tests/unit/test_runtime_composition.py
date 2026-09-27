"""W1 — canonical runtime composition contracts.

The mission (Phase Zero plan, W1): ONE runtime context owns the settings
snapshot, the database identity decision, the LLM engine/provider, the
request queue, the conversation store, and lifecycle shutdown.  These
tests are the acceptance gate:

* database identity — ``get_session()`` with no arguments must follow
  ``settings.db_path`` (NEXUS_DB_PATH), never a hardcoded literal;
* gateway identity — every production adopter resolves the SAME engine /
  provider instance the runtime owns (shared queue, shared store);
* constructor ownership — ``build_handlers``/feature engines/knowledge
  handlers/agents receive the shared instances instead of constructing
  private ones;
* shutdown ownership — the application shutdown hook actually drains the
  queue, settles pending waiters, shuts the job queue and releases DB
  resources, in a recorded order, idempotently.

Baseline reproduction: every test in this module RED on the pre-W1 tree
(defect or missing contract), GREEN after the runtime lands.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.config.settings import get_settings

# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def fresh_settings():
    """Re-read env-backed settings exactly once, then restore the cache."""

    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


def _settings_for(tmp_path, monkeypatch, *, db_name="w1.sqlite", api_key="w1-test-key"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEXUS_DB_PATH", db_name)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if api_key is not None:
        monkeypatch.setenv("GEMINI_API_KEY", api_key)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("NEXUS_GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


# ═══════════════════════════════════════════════════════════════════════════
# 1. Database identity — one canonical decision
# ═══════════════════════════════════════════════════════════════════════════


async def test_get_session_without_arguments_follows_settings_db_path(tmp_path, monkeypatch):
    """Baseline defect (storage/db.py:get_session(None)): the no-arg fallback
    opened a hardcoded literal ``data/app.sqlite`` even when
    ``settings.db_path`` (NEXUS_DB_PATH) pointed elsewhere, so features that
    call ``get_session()`` silently diverged from the configured database.

    Contract: no-arg sessions follow the canonical identity decision and
    the literal file is never created.
    """

    settings = _settings_for(tmp_path, monkeypatch, db_name="custom.sqlite")
    from nexus_ai_agent.storage.db import get_session

    async with get_session() as session:  # no arguments — the production pattern
        url = str(session.get_bind().url)

    assert "custom.sqlite" in url, f"configured path was ignored: bind={url}"
    assert "data/app.sqlite" not in url, (
        f"silently bound the hardcoded literal while settings.db_path={settings.db_path!r}"
    )


async def test_get_session_default_path_regression_unchanged(tmp_path, monkeypatch):
    """Regression guard: with NOTHING configured, the default stays
    ``data/app.sqlite`` (relative to the working directory)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DB_PATH", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    from nexus_ai_agent.storage.db import get_session

    async with get_session() as session:
        url = str(session.get_bind().url)

    assert url.endswith("data/app.sqlite"), f"default regression: bind={url}"


def test_database_identity_resolver_reports_decision_and_source(tmp_path, monkeypatch):
    """The runtime exposes exactly ONE place where the backend is decided,
    with an auditable source for evidence (env URL > settings.db_path)."""

    from nexus_ai_agent.application.runtime import resolve_database_identity

    settings = _settings_for(tmp_path, monkeypatch, db_name="already.sqlite")
    ident = resolve_database_identity(settings)
    assert ident.backend == "sqlite"
    assert ident.dsn_normalized == "already.sqlite"
    assert ident.dsn_raw == "already.sqlite"
    assert ident.source in {"settings.db_path", "default"}

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://nexus:w1pass@db.example:5432/nexusw1")
    get_settings.cache_clear()
    ident = resolve_database_identity(get_settings())
    assert ident.backend == "postgres"
    assert ident.source == "env:NEXUS_DATABASE_URL"
    assert "w1pass" not in ident.describe(), "describe() must never leak credentials"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Gateway identity — one shared engine/provider/queue/store
# ═══════════════════════════════════════════════════════════════════════════


def _build_runtime(tmp_path, monkeypatch):
    settings = _settings_for(tmp_path, monkeypatch)
    from nexus_ai_agent.application import runtime as runtime_mod

    return settings, runtime_mod.build_runtime(settings)


def test_runtime_exposes_one_shared_engine_provider_queue_and_store(tmp_path, monkeypatch):
    """Gateway identity: the provider adopters receive wraps the runtime's
    single engine; the engine is wired to the runtime's single queue and
    conversation store.  A fresh provider must NOT own a private queue-less
    engine (that was the hands.py/agent/memory split-brain)."""

    _, rt = _build_runtime(tmp_path, monkeypatch)
    assert rt.gemini_engine is not None
    assert rt.llm_provider is not None
    # Same engine object everywhere — identity, not type equality.
    assert rt.llm_provider.engine is rt.gemini_engine
    assert rt.gemini_engine._queue is rt.request_queue
    assert rt.gemini_engine._store is rt.conversation_store
    assert rt.summarizer_engine is not None


def test_build_runtime_is_deterministic_per_call(tmp_path, monkeypatch):
    """Each explicit build produces a fresh, independent runtime (tests and
    embedded callers can own several), but WITHIN one runtime everything is
    shared.  The function must never return hidden module-global state."""

    _, rt_a = _build_runtime(tmp_path, monkeypatch)
    _, rt_b = _build_runtime(tmp_path, monkeypatch)
    assert rt_a is not rt_b
    assert rt_a.request_queue is not rt_b.request_queue
    assert rt_a.gemini_engine is not rt_b.gemini_engine


def test_legacy_provider_fallback_warns_and_is_flagged(tmp_path, monkeypatch):
    """Legacy seam ratchet: constructing GeminiProvider WITHOUT an engine
    still works (base_agent/ai_memory/knowledge fallbacks) but must emit an
    observability warning so production split-brains are detectable."""

    _settings_for(tmp_path, monkeypatch)
    from structlog.testing import capture_logs

    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

    with capture_logs() as logs:
        provider = GeminiProvider(api_key="legacy-key")
    assert provider is not None
    assert any("legacy" in entry.get("event", "").lower() for entry in logs), (
        "constructing GeminiProvider without an approved-factory engine must be loudly observable"
    )
    assert provider.engine is not None, "the provider exposes its engine either way"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Constructor ownership at the surfaces
# ═══════════════════════════════════════════════════════════════════════════


def test_build_handlers_uses_runtime_engine_and_constructs_nothing(tmp_path, monkeypatch):
    """Baseline defect: ``build_handlers`` ALWAYS built its own GeminiEngine
     + SummarizerEngine, duplicating whatever the application had constructed
     (two queue instances, a queue-less handler-side engine for some paths).

     Contract: when the runtime engine/summarizer are handed in, the handler
     layer performs ZERO engine constructions, and the closures expose the
    (runtime-owned) instances.
    """

    settings = _settings_for(tmp_path, monkeypatch)
    _, rt = _build_runtime(tmp_path, monkeypatch)
    from nexus_ai_agent.bot import handlers as bot_handlers
    from nexus_ai_agent.features import ai_chat

    constructed: list[Any] = []
    real_engine_ctor = ai_chat.GeminiEngine
    monkeypatch.setattr(
        ai_chat,
        "GeminiEngine",
        lambda *a, **k: constructed.append(real_engine_ctor(*a, **k)) or constructed[-1],
    )
    # bot/handlers imported the symbol directly — patch there too.
    if hasattr(bot_handlers, "GeminiEngine"):
        monkeypatch.setattr(bot_handlers, "GeminiEngine", ai_chat.GeminiEngine)

    handlers = bot_handlers.build_handlers(
        llm_engine=rt.gemini_engine,
        summarizer_engine=rt.summarizer_engine,
        **{
            "graph": SimpleNamespace(),
            "db_session_factory": None,
            "settings": settings,
            "presence": SimpleNamespace(),
            "storage": SimpleNamespace(),
            "feature_engines": None,
        },
    )
    assert handlers, "build_handlers must return the handler list"
    assert not constructed, (
        f"handler layer constructed {len(constructed)} private engine(s) "
        "despite the runtime engine being provided"
    )
    # Identity through the closure: the engine used by the chat commands IS
    # the runtime engine.
    from telegram.ext import CommandHandler

    callbacks = [h.callback for h in handlers if isinstance(h, CommandHandler)]
    engines_seen: set[int] = set()
    for cb in callbacks:
        for cell in cb.__closure__ or ():
            value = cell.cell_contents
            if value is rt.gemini_engine:
                engines_seen.add(id(value))
    assert engines_seen, "no command closure references the runtime engine"


def test_build_handlers_legacy_mode_logs_warning(tmp_path, monkeypatch):
    """Legacy seam ratchet: without injected engines the old self-construction
    keeps working (standalone/tests) but must emit a structured warning."""

    settings = _settings_for(tmp_path, monkeypatch)
    from structlog.testing import capture_logs

    from nexus_ai_agent.bot import handlers as bot_handlers

    with capture_logs() as logs:
        handlers = bot_handlers.build_handlers(
            graph=SimpleNamespace(),
            db_session_factory=None,
            settings=settings,
            presence=SimpleNamespace(),
            storage=SimpleNamespace(),
            feature_engines=None,
        )
    assert handlers
    events = [entry.get("event", "").lower() for entry in logs]
    assert any("legacy" in ev and "llm" in ev for ev in events), (
        f"legacy self-construction must be loudly observable; events={events}"
    )


def test_feature_engines_receive_the_runtime_llm_provider(tmp_path, monkeypatch):
    """Baseline defect: ``build_feature_engines`` built ``AIMemoryEngine()``
    with no provider, so the memory engine spawned its own queue-less
    GeminiProvider per application start."""

    settings = _settings_for(tmp_path, monkeypatch)
    _, rt = _build_runtime(tmp_path, monkeypatch)
    from nexus_ai_agent.bot.feature_handlers import build_feature_engines

    engines = build_feature_engines(settings, llm_provider=rt.llm_provider)
    assert engines.ai_memory.gemini is rt.llm_provider


async def test_knowledge_handlers_use_runtime_llm_provider_from_bot_data(tmp_path, monkeypatch):
    """Baseline defect: /learn /wiki /search constructed a fresh
    ``KnowledgeManager()`` per command, each owning a queue-less provider.

    Contract (without touching the actively-claimed knowledge/ zone): the
    handler resolves the runtime provider from bot_data and injects it.
    """

    settings = _settings_for(tmp_path, monkeypatch)
    sentinel = object()
    from nexus_ai_agent.bot import knowledge_handlers

    captured: dict[str, Any] = {}

    class _RecordingKM:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        async def learn(self, query: str) -> str:
            return "ok"

        async def close(self) -> None:  # pragma: no cover - trivial
            pass

    monkeypatch.setattr(knowledge_handlers, "KnowledgeManager", _RecordingKM)

    async def reply(text: str) -> None:
        captured.setdefault("replies", []).append(text)

    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=reply),
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(
        args=["black holes"],
        application=SimpleNamespace(bot_data={"llm_provider": sentinel}),
    )
    _ = settings
    await knowledge_handlers.learn_cmd(update, context)  # type: ignore[arg-type]
    assert captured.get("kwargs", {}).get("gemini_provider") is sentinel


async def test_agent_manager_instantiates_agent_with_shared_provider(tmp_path, monkeypatch):
    """Baseline defect: AgentManager.get_active() did ``agent_class()`` with
    no arguments, so EVERY active-agent message built a private queue-less
    GeminiProvider via the base_agent fallback."""

    _settings_for(tmp_path, monkeypatch)
    sentinel = object()
    from nexus_ai_agent.agents.store import agent_manager as am

    captured: dict[str, Any] = {}

    class _RecordingAgent:
        name = "rec"
        emoji = "r"
        description = "d"
        category = "c"
        system_prompt = "s"

        def __init__(self, gemini_provider=None, **kw):
            captured["provider"] = gemini_provider
            captured["kw"] = kw

    monkeypatch.setitem(am.AGENTS, "rec", _RecordingAgent)
    await am.AgentManager.activate(777, "rec")
    try:
        active = await am.AgentManager.get_active(777, gemini_provider=sentinel)
        assert active is not None
        assert captured.get("provider") is sentinel, (
            "agent instance must receive the shared provider, not fall back "
            "to constructing a private one"
        )
    finally:
        with contextlib.suppress(Exception):
            await am.AgentManager.deactivate(777)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Shutdown ownership
# ═══════════════════════════════════════════════════════════════════════════


async def test_aclose_settles_pending_waiters_and_marks_queue_closed(tmp_path, monkeypatch):
    """Shutdown ownership: closing the runtime must settle every pending
    queue waiter (no silent leak past application lifetime) and put the
    queue into its observable closed state."""

    import asyncio

    from nexus_ai_agent.features.request_queue import RequestQueueClosedError

    _, rt = _build_runtime(tmp_path, monkeypatch)

    started = asyncio.Event()

    async def _stalled() -> str:
        started.set()
        await asyncio.sleep(60)  # never resolves without external close
        return "unreachable"

    task = asyncio.create_task(rt.request_queue.submit(_stalled, user_id=1))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert not task.done()

    await rt.aclose()

    with pytest.raises((RequestQueueClosedError, asyncio.CancelledError)):
        await asyncio.wait_for(task, timeout=5.0)
    assert rt.request_queue.get_status()["closed"] is True


async def test_aclose_runs_components_in_recorded_order_and_is_idempotent(tmp_path, monkeypatch):
    """The shutdown sequence is ordered: drain/settle the LLM queue first,
    then the job queue, then storage; and a second close is a no-op."""

    _, rt = _build_runtime(tmp_path, monkeypatch)
    order: list[str] = []

    class _RecordingJobQueue:
        async def shutdown(self) -> None:
            order.append("job_queue")

    rt.job_queue = _RecordingJobQueue()

    real_queue_close = rt.request_queue.close

    async def _rec_close() -> None:
        order.append("request_queue")
        await real_queue_close()

    rt.request_queue.close = _rec_close  # type: ignore[method-assign]

    await rt.aclose()
    first = list(order)
    await rt.aclose()
    assert first == order, "aclose must be idempotent"
    assert order[0] == "request_queue"
    assert order.index("request_queue") < order.index("job_queue")


async def test_post_shutdown_hook_invokes_runtime_aclose(tmp_path, monkeypatch):
    """The hook PTB calls at Application shutdown goes through the runtime
    (not just feature_engines.reminders as before)."""

    _, rt = _build_runtime(tmp_path, monkeypatch)
    from nexus_ai_agent.bot.app import make_post_shutdown

    closed: list[str] = []

    class _FeaturingEngines:
        class reminders:  # noqa: N801 - mirrors the real dataclass field
            @staticmethod
            def close() -> None:
                closed.append("reminders")

    hook = make_post_shutdown(rt, _FeaturingEngines())
    assert hook is not None
    await hook(SimpleNamespace(bot_data={}))
    assert rt.request_queue.get_status()["closed"] is True
    assert closed == ["reminders"]


def test_create_application_wires_post_shutdown_through_runtime():
    """Structural guard: the application builder must construct the shutdown
    hook via make_post_shutdown(runtime, feature_engines) — this guards the
    wiring, complementing the behavioral hook test above."""

    import inspect

    from nexus_ai_agent.bot import app as bot_app

    source = inspect.getsource(bot_app)
    assert "make_post_shutdown(" in source
    assert ".post_shutdown(make_post_shutdown(" in source


# ── Settings-cache isolation contract (conftest autouse fixture) ────────
#
# ``_isolate_settings_cache`` (tests/conftest.py) clears the process-wide
# ``get_settings`` lru-cache after every test.  The pair below is the LAW-7
# detector for that fixture: part one deliberately pins a tmp NEXUS_DB_PATH
# in the cache and leaves it; part two (running immediately after) must see
# its own environment.  Removing the conftest fixture makes part two read
# part one's leaked path → RED.


def test_settings_isolation_part_one_pins_env_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "pinned.sqlite"))
    get_settings.cache_clear()
    assert get_settings().db_path == str(tmp_path / "pinned.sqlite")


def test_settings_isolation_part_two_sees_own_environment() -> None:
    assert get_settings().db_path == "data/app.sqlite"
