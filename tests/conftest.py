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
    db_module._session_factory = None  # type: ignore[attr-defined]

    return settings_module.get_settings()


@pytest.fixture()
def sample_telegram_update() -> dict[str, Any]:
    p = Path(__file__).parent / "fixtures" / "telegram_updates" / "sample_message.json"
    return json.loads(p.read_text(encoding="utf-8"))
