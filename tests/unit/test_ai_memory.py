"""Behavioural tests for the AIMemory extract + forget cycle.

These exercise the two user-visible operations (extraction into long-term
memory, and ``/forget_me``). Since P0-7, extraction is gated behind an
explicit per-user consent vote, so the tests grant consent first — the
gate itself is covered in depth by ``test_ai_memory_consent.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features.ai_memory import AIMemoryEngine
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage import models as models_module  # noqa: F401  (registers tables)


def _reset_db_engine_cache() -> None:
    db_module._engine = None  # type: ignore[misc]
    db_module._engine_path = None  # type: ignore[misc]
    db_module._session_factory = None  # type: ignore[misc]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ``get_session()`` (CWD-relative ``data/app.sqlite``) per test."""
    (tmp_path / "data").mkdir()
    db_file = tmp_path / "data" / "app.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    _reset_db_engine_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class _FakeGemini:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def generate(self, prompt: str, system: str = "") -> str:
        return self.reply


async def test_extract_context(db: None) -> None:
    engine = AIMemoryEngine(
        gemini_provider=_FakeGemini('{"name": "Majid", "occupation": "Developer"}'),  # type: ignore[arg-type]
        min_egress_seconds=0,
    )
    # P0-7: grant explicit consent before any egress.
    await engine.set_consent(123, granted=True)
    await engine.update_from_message(123, "My name is Majid and I am a developer")

    context = await engine.get_context(123)
    assert "Majid" in context
    assert "Developer" in context


async def test_forget_me(db: None) -> None:
    engine = AIMemoryEngine(
        gemini_provider=_FakeGemini("{}"),  # type: ignore[arg-type]
        min_egress_seconds=0,
    )
    # Manually save something to test deletion.
    await engine._save_memory(123, {"name": "Majid"})
    await engine.forget_user(123)
    context = await engine.get_context(123)
    assert context == ""
