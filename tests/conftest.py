from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider


@pytest.fixture()
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture(autouse=True)
async def _dispose_db_engine():
    """Dispose the shared async engine between tests.

    aiosqlite connections are bound to the event loop they were opened on;
    leaving the global engine referenced across tests lets worker threads
    outlive their loop and emit "Event loop is closed" noise.  Disposing
    while the current loop is still open keeps the suite warning-free and
    gives each test a fresh engine.

    The schema cache (``_initialized_paths``) intentionally survives: it is
    keyed by absolute file identity (W1 recovery), so a disposed engine never
    causes a fresh file to skip schema creation, while a reused file keeps
    its idempotent fast path.  Engine lifetime and schema lifetime stay
    separate (LAW 11).
    """
    yield
    from nexus_ai_agent.storage import db as db_module

    if db_module._engine is not None:
        await db_module._engine.dispose()
        db_module._engine = None
        db_module._engine_path = None
        db_module._session_factory = None


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """Keep ``get_settings()`` hermetic across tests.

    W1 recovery (task-196): ``get_session(None)`` follows
    ``settings.db_path`` (NEXUS_DB_PATH).  Several fixtures set that variable
    via ``monkeypatch`` (auto-undone) but leave the ``lru_cache`` holding the
    previous test's absolute tmp path — the next test then opens the wrong
    file.  Clearing before *and* after every test makes each test read its
    own environment, regardless of fixture teardown discipline elsewhere.
    """
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def settings_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    Override settings/env to use temp paths for tests.
    """

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    monkeypatch.setenv("NEXUS_LOG_LEVEL", "INFO")
    monkeypatch.setenv("NEXUS_ENABLE_SHELL", "false")
    monkeypatch.setenv("NEXUS_ALLOWED_USER_IDS", "")

    # Clear cached settings between tests.
    settings_module.get_settings.cache_clear()

    # Reset DB engine cache (important when settings override DB path).
    from nexus_ai_agent.storage import db as db_module

    db_module._engine = None  # type: ignore[attr-defined]
    db_module._engine_path = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]

    yield settings_module.get_settings()

    # Teardown mirrors setup so the override never leaks into the next test
    # even if the global autouse isolation order ever changes.
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._engine_path = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]


@pytest.fixture()
def sample_telegram_update() -> dict[str, Any]:
    p = Path(__file__).parent / "fixtures" / "telegram_updates" / "sample_message.json"
    return json.loads(p.read_text(encoding="utf-8"))
