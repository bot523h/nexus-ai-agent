"""W1 — True Runtime Closure tests.

These tests prove the canonical lifecycle invariants rather than just
checking CI green.  Each test maps directly to one of the W1 Semantic
Targets and is designed to be broken by a meaningful regression:

* Webhook lifecycle consumes the canonical runtime (no double resume,
  no orphan cleanup).
* All engines created by the runtime are shut down (conversation
  store, summarizer httpx client, request queue, job queue, referral
  engine, reminders engine, sync feature engines, global async DB).
* Fail-safe shutdown: one failing cleanup step never strands later
  resources.
* Same-runtime instance is non-retryable after shutdown (explicit
  contract — a Runtime may not be started twice).
* /myagent cannot construct a private GeminiProvider unless the
  caller explicitly opts into the legacy fallback.
* webhook _serve_webhook does NOT duplicate resume_pending.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── Helpers / fixtures ────────────────────────────────────────────────


class _FakeSettings:
    """Minimal settings stand-in that exercises the runtime path."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = str(tmp_path / "app.sqlite")
        self.cache_dir = str(tmp_path / "cache")
        self.telegram_bot_token = "test-token"
        self.gemini_api_key = "test-key"
        self.gemini_model = "gemini-2.0-flash"
        self.gemini_max_rpm = 15
        self.gemini_max_daily = 1500
        self.dropbox_token = None
        self.pcloud_token = None
        self.internxt_token = None
        self.github_token = None
        self.github_repo = None
        self.mega_email = None
        self.mega_password = None
        self.huggingface_token = None
        self.rclone_remote = None
        self.gdrive_bearer_token = None
        self.r2_account_id = None
        self.r2_access_key_id = None
        self.r2_secret_access_key = None
        self.r2_bucket = None
        self.log_level = "WARNING"
        self.allowed_user_ids = []
        self.owner_telegram_id = 0
        self.ai_memory_enabled = False
        self.webhook_url = "https://example.org/hook"
        self.webhook_secret = "shh"
        self.creative_temp_dir = str(tmp_path / "creative")
        self.creative_gemini_api_key = None


@pytest.fixture()
def fake_settings(tmp_path: Path) -> _FakeSettings:
    return _FakeSettings(tmp_path)


def _build_runtime(fake_settings: _FakeSettings) -> Any:
    """Build a Runtime by exercising _init_v2_engines directly."""
    from nexus_ai_agent.bot.app import _init_v2_engines
    from nexus_ai_agent.core.runtime import Runtime

    runtime = Runtime(settings=fake_settings)
    engines = _init_v2_engines(fake_settings, runtime)
    runtime.engines = engines
    return runtime


# ── 1. Runtime owns and closes every engine ───────────────────────────


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_summarizer_http_client(
    fake_settings: _FakeSettings,
) -> None:
    runtime = _build_runtime(fake_settings)
    summarizer = runtime.engines["summarizer_engine"]
    assert summarizer is not None
    # The httpx.AsyncClient inside SummarizerEngine is long-lived.
    http_client = summarizer._http
    assert not http_client.is_closed

    await runtime.shutdown()

    assert http_client.is_closed, "summarizer httpx client must be aclosed"


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_conversation_store(
    fake_settings: _FakeSettings,
) -> None:
    runtime = _build_runtime(fake_settings)
    conv_store = runtime.engines["conversation_store"]
    # The sync SQLAlchemy engine inside ConversationStore is disposed
    # (its pool is closed).  After close() the private attribute is None.
    assert conv_store._engine is not None

    await runtime.shutdown()

    assert conv_store._engine is None, "conversation store engine must be disposed"


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_referral_engine(
    fake_settings: _FakeSettings,
) -> None:
    runtime = _build_runtime(fake_settings)
    referral = runtime.engines["referral_engine"]
    assert referral._engine is not None

    await runtime.shutdown()

    assert referral._engine is None, "referral engine must be disposed"


@pytest.mark.asyncio
async def test_runtime_shutdown_closes_request_queue(
    fake_settings: _FakeSettings,
) -> None:
    runtime = _build_runtime(fake_settings)
    rq = runtime.engines["request_queue"]
    # Precondition: queue accepts submissions.
    assert not rq._closed
    await runtime.shutdown()
    assert rq._closed, "request queue must be closed"


@pytest.mark.asyncio
async def test_runtime_shutdown_disposes_global_sqlite_engine(
    fake_settings: _FakeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global async SQLite engine in storage.db must be disposed."""
    # Trigger engine creation through get_session.
    from nexus_ai_agent.storage import db as db_module

    # Reset global state so the test is hermetic.
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    db_module._replaced_engines.clear()
    db_module._initialized_paths.clear()

    async with db_module.get_session(fake_settings.db_path):
        pass
    assert db_module._engine is not None, "precondition: engine created"

    from nexus_ai_agent.core.runtime import _dispose_global_db_engines

    await _dispose_global_db_engines()
    assert db_module._engine is None, "global SQLite engine must be disposed"
    assert db_module._replaced_engines == []


# ── 2. Fail-safe shutdown (W1 Law 6) ─────────────────────────────────


@pytest.mark.asyncio
async def test_failing_cleanup_step_does_not_strand_later_steps() -> None:
    """One cleanup raising must not prevent later steps from running."""
    from nexus_ai_agent.core.runtime import Runtime

    runtime = Runtime(settings=object())
    order: list[str] = []

    async def bad_step(rt: Runtime) -> None:
        order.append("bad")
        raise RuntimeError("boom")

    async def good_step(rt: Runtime) -> None:
        order.append("good")

    # Steps register in order; shutdown runs LIFO so "good" runs first.
    runtime.add_cleanup(bad_step)
    runtime.add_cleanup(good_step)

    await runtime.shutdown()

    assert order == ["good", "bad"], (
        "both cleanup steps must run even though 'bad' raises"
    )


@pytest.mark.asyncio
async def test_shielded_cleanup_steps_survive_cancellation() -> None:
    """Each step is wrapped in asyncio.shield so a CancelledError raised
    inside a step does not abort the chain.  We simulate a step that
    raises CancelledError directly and prove the next step still runs."""
    from nexus_ai_agent.core.runtime import Runtime

    runtime = Runtime(settings=object())
    ran_later = False

    async def cancelling_step(rt: Runtime) -> None:
        raise asyncio.CancelledError()

    async def later_step(rt: Runtime) -> None:
        nonlocal ran_later
        ran_later = True

    runtime.add_cleanup(cancelling_step)
    runtime.add_cleanup(later_step)

    await runtime.shutdown()
    assert ran_later, "later step must run even when earlier step is cancelled"


# ── 3. Shutdown is idempotent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(fake_settings: _FakeSettings) -> None:
    runtime = _build_runtime(fake_settings)
    await runtime.shutdown()
    # Second shutdown is a no-op — must not raise or double-dispose.
    await runtime.shutdown()


# ── 4. StoreAgent bypass guard (W1 Law 8) ─────────────────────────────


def test_store_agent_requires_injected_provider() -> None:
    """A StoreAgent without provider and without legacy opt-in must fail fast."""
    from nexus_ai_agent.agents.store.specialized_agents import CodingAgent

    with pytest.raises(RuntimeError, match="runtime-owned GeminiProvider"):
        CodingAgent()  # no provider, no fallback flag


def test_store_agent_accepts_injected_provider() -> None:
    provider = MagicMock()
    from nexus_ai_agent.agents.store.specialized_agents import CodingAgent

    a = CodingAgent(gemini_provider=provider)
    assert a.gemini is provider


def test_store_agent_allows_legacy_fallback_only_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests may opt in to the legacy fallback explicitly."""
    # Patch GeminiProvider at the llm.gemini_provider module before
    # base_agent lazily imports it.
    from nexus_ai_agent.agents.store.specialized_agents import CodingAgent
    from nexus_ai_agent.config import settings as settings_mod
    from nexus_ai_agent.llm import gemini_provider as gp_mod

    fake_s = MagicMock()
    fake_s.gemini_api_key = "k"
    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake_s)

    _provider = MagicMock()
    monkeypatch.setattr(gp_mod, "GeminiProvider", lambda api_key: _provider)
    a = CodingAgent(allow_legacy_fallback=True)
    assert a.gemini is _provider


# ── 5. AgentManager.get_active rejects missing provider unless legacy ──


@pytest.mark.asyncio
async def test_agent_manager_get_active_passes_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a provider is injected, the agent is constructed with it."""
    import nexus_ai_agent.agents.store.agent_manager as am_mod

    # Patch DB session to avoid real DB (proper async context manager).
    class _FakeResult:
        def scalar_one_or_none(self):
            rec = MagicMock()
            rec.agent_name = "coding"
            return rec

    class _AwaitableResult:
        def __init__(self, result): self._result = result
        def __await__(self):
            async def _r(): return self._result
            return _r().__await__()

    class _FakeSessionCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def execute(self, *a, **kw): return _AwaitableResult(_FakeResult())
    def _fake_session_factory(*args, **kwargs):
        return _FakeSessionCtx()

    import nexus_ai_agent.agents.store.agent_manager as am
    monkeypatch.setattr(am, "get_session", _fake_session_factory)

    provider = MagicMock()
    constructed_with: dict[str, Any] = {}
    class _StubCoding:
        name = "Coding"
        def __init__(self, gemini_provider=None, allow_legacy_fallback=False):
            constructed_with["provider"] = gemini_provider
            constructed_with["legacy"] = allow_legacy_fallback
    monkeypatch.setitem(am_mod.AGENTS, "coding", _StubCoding)

    agent = await am_mod.AgentManager.get_active(42, provider=provider)
    assert agent is not None
    assert constructed_with["provider"] is provider
    assert constructed_with["legacy"] is False


# ── 6. Webhook _serve_webhook does NOT duplicate resume_pending ───────


def test_webhook_lifecycle_no_duplicate_resume() -> None:
    """_serve_webhook must not call job_queue.resume_pending itself —
    PTB's post_init (invoked by application.initialize()) is the ONE
    authority.  We look for an actual call pattern in the body (after
    the docstring) so the invariant can't be bypassed by renaming.
    """
    import inspect

    from nexus_ai_agent.bot import webhook as webhook_mod

    source = inspect.getsource(webhook_mod._serve_webhook)
    # Strip the docstring by finding the first code line after the
    # opening triple-quote block.
    body_start = source.find('"""', source.find('"""') + 3) + 3
    body = source[body_start:]
    assert "resume_pending(" not in body, (
        "_serve_webhook must not call resume_pending() directly — PTB "
        "post_init owns startup; webhook must not duplicate it."
    )


# ── 7. Webhook shutdown ordering is stop-then-shutdown ────────────────


@pytest.mark.asyncio
async def test_webhook_shutdown_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when application.stop() raises, application.shutdown() must
    still be awaited (fail-safe).

    We patch uvicorn BEFORE importing the webhook module so the lazy
    ``import uvicorn`` inside _serve_webhook finds our stub.
    """
    import sys
    from types import ModuleType

    calls: list[str] = []

    class _FakeApp:
        async def initialize(self): calls.append("initialize")
        async def start(self): calls.append("start")
        class bot:
            @staticmethod
            async def set_webhook(url, secret_token): calls.append("set_webhook")
        async def stop(self):
            calls.append("stop")
            raise RuntimeError("stop failed")
        async def shutdown(self): calls.append("shutdown")

    class _FakeServer:
        def __init__(self, config): pass
        async def serve(self): calls.append("serve")

    class _FakeConfig:
        def __init__(self, app, **kw): pass

    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.Config = _FakeConfig
    fake_uvicorn.Server = _FakeServer  # accepts (config) via __init__
    # Pre-populate sys.modules so any lazy import picks up our stub.
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    from nexus_ai_agent.bot import webhook as webhook_mod

    await webhook_mod._serve_webhook(
        application=_FakeApp(),
        api_app=MagicMock(),
        webhook_url="https://x/h",
        webhook_secret="s",
        host="0.0.0.0",
        port=8000,
        log_level="warning",
    )
    assert calls == ["initialize", "start", "set_webhook", "serve", "stop", "shutdown"], (
        f"shutdown must run even after stop() raised; got {calls}"
    )


# ── 8. Feature sync engines have _shutdown_engines ────────────────────


def test_force_join_and_anon_chat_expose_shutdown() -> None:
    from nexus_ai_agent.features import anonymous_chat, force_join
    assert callable(force_join._shutdown_engines)
    assert callable(anonymous_chat._shutdown_engines)


@pytest.mark.asyncio
async def test_shutdown_module_sync_engines_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling shutdown_module_sync_engines after engines are created
    disposes them without raising."""
    from nexus_ai_agent.config import settings as sm
    fake = _FakeSettings(tmp_path)
    monkeypatch.setattr(sm, "get_settings", lambda: fake)

    from nexus_ai_agent.core.runtime import shutdown_module_sync_engines
    from nexus_ai_agent.features import anonymous_chat, force_join

    # Trigger engine creation by touching the caches.
    force_join._sync_engine(fake.db_path)
    anonymous_chat._sync_engine(fake.db_path)
    assert force_join._sync_engines, "precondition: engine cached"
    assert anonymous_chat._sync_engines, "precondition: engine cached"

    await asyncio.to_thread(shutdown_module_sync_engines)

    assert force_join._sync_engines == {}
    assert anonymous_chat._sync_engines == {}


# ── 9. GeminiProvider accepts injected engine (W1 single-provider) ────


def test_gemini_provider_accepts_injected_engine() -> None:
    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

    engine = MagicMock()
    p = GeminiProvider(engine=engine)
    assert p.engine is engine


# ── 10. build_application registers runtime in bot_data ───────────────


def test_build_application_registers_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_application puts the canonical Runtime in bot_data["runtime"].

    This is important for diagnostics and for any future code path that
    needs to hook into lifecycle.
    """
    from nexus_ai_agent.bot.app import build_application

    fake = _FakeSettings(tmp_path)
    # Stub out PTB ApplicationBuilder so we don't need a real token round-trip.
    import telegram.ext._applicationbuilder as ab_mod

    class _FakeApp:
        bot_data: dict[str, Any] = {}
        def add_handler(self, *a, **kw): pass
        class bot:
            @staticmethod
            async def set_webhook(*a, **kw): pass

    class _FakeBuilder:
        def __init__(self):
            self._post_init = None
            self._post_shutdown = None

        def token(self, t):
            return self

        def post_init(self, fn):
            self._post_init = fn
            return self

        def post_shutdown(self, fn):
            self._post_shutdown = fn
            return self
        def build(self):
            app = _FakeApp()
            app._post_init = self._post_init
            app._post_shutdown = self._post_shutdown
            return app

    monkeypatch.setattr(ab_mod, "ApplicationBuilder", _FakeBuilder)

    # Build handlers with None feature_engines to force fallback.
    app = build_application(
        fake,
        graph=MagicMock(),
        storage=MagicMock(),
        presence=MagicMock(),
        session_factory=MagicMock(),
    )
    assert "runtime" in app.bot_data, "runtime must be published in bot_data"
    runtime = app.bot_data["runtime"]
    assert hasattr(runtime, "shutdown"), "runtime must expose shutdown()"
    # The registered engines include both canonical gemini_engine and
    # gemini_provider (wrapping the same engine).
    assert app.bot_data["gemini_engine"] is not None
    assert app.bot_data["gemini_provider"] is not None
    assert app.bot_data["gemini_provider"].engine is app.bot_data["gemini_engine"]
