"""Minimal regression tests for the commands repaired in the Phase 0 gate commit.

Each test invokes the real handler callback (from ``build_handlers``) with
lightweight fakes and asserts the command no longer crashes and produces a
reply.

Repaired commands covered here:
- ``/quiz``       — ``QuizGame.get_question`` now receives ``user_id`` and the
                    ``None`` result is handled (question key is ``"q"``).
- ``/code``       — wired to ``GeminiEngine.code`` (previously a missing
                    ``GeminiEngine.generate`` method).
- ``/translate``  — wired to ``GeminiEngine.translate`` (previously a missing
                    ``GeminiEngine.generate`` method).
- ``/viral_now``  — wired to the existing ``ViralEngine`` static methods
                    (previously a missing ``ViralEngine.generate_and_send``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.presence import PresenceStore

OWNER_ID = 111
CHAT_ID = 222


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, user_id: int = OWNER_ID, chat_id: int = CHAT_ID, text: str = "") -> None:
        self.effective_user = SimpleNamespace(id=user_id, username="tester")
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage(text)
        self.edited_message = None
        self.callback_query = None


def _get_handler(handlers: list[Any], name: str) -> Any:
    for handler in handlers:
        commands = getattr(handler, "commands", None)
        if commands and set(commands) == {name}:
            return handler
    raise AssertionError(f"handler for /{name} not found")


async def _invoke(
    handlers: list[Any], name: str, update: _FakeUpdate, *, args: list[str] | None = None
) -> list[str]:
    handler = _get_handler(handlers, name)
    context = SimpleNamespace(args=args or [], bot=SimpleNamespace())
    await handler.callback(update, context)
    assert update.message.replies, f"/{name} did not reply"
    return update.message.replies


@pytest.fixture()
def bot_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    settings_module.get_settings.cache_clear()

    # owner_control caches the owner id in a module global after first read.
    from nexus_ai_agent.features import owner_control

    owner_control._owner_id = None  # type: ignore[attr-defined]

    from nexus_ai_agent.storage import db as db_module

    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    return settings_module.get_settings()


def _build(settings: Any) -> list[Any]:
    return build_handlers(object(), lambda: None, settings, PresenceStore(), object())


@pytest.mark.asyncio
async def test_quiz_command_no_crash(bot_settings: Any) -> None:
    handlers = _build(bot_settings)
    update = _FakeUpdate(user_id=OWNER_ID)
    replies = await _invoke(handlers, "quiz", update)
    assert "Quiz Time" in replies[0]


@pytest.mark.asyncio
async def test_code_command_wired_to_gemini_code(bot_settings: Any, monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_code(self: Any, prompt: str, *, user_id: int) -> str:
        captured["prompt"] = prompt
        captured["user_id"] = str(user_id)
        return "print('hello')\n"

    monkeypatch.setattr(GeminiEngine, "code", fake_code)

    handlers = _build(bot_settings)
    update = _FakeUpdate(user_id=OWNER_ID)
    replies = await _invoke(handlers, "code", update, args=["a counter"])

    assert captured["prompt"] == "a counter"
    assert captured["user_id"] == str(OWNER_ID)
    assert "print('hello')" in replies[0]


@pytest.mark.asyncio
async def test_translate_command_wired_to_gemini_translate(bot_settings: Any, monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_translate(self: Any, text: str, *, target_lang: str, user_id: int) -> str:
        captured["text"] = text
        captured["target_lang"] = target_lang
        return f"translated({target_lang}): {text}"

    monkeypatch.setattr(GeminiEngine, "translate", fake_translate)

    handlers = _build(bot_settings)
    update = _FakeUpdate(user_id=OWNER_ID)
    replies = await _invoke(handlers, "translate", update, args=["salam"])

    assert captured["text"] == "salam"
    assert captured["target_lang"] == "Persian"
    assert "translated(Persian): salam" in replies[0]


@pytest.mark.asyncio
async def test_viral_now_command_uses_existing_engine_api(bot_settings: Any, monkeypatch) -> None:
    monkeypatch.setattr(ViralEngine, "calculate_viral_score", staticmethod(lambda text: 8.5))
    monkeypatch.setattr(
        ViralEngine, "save_post", staticmethod(lambda chat_id, text, viral_score=0.0: 42)
    )

    handlers = _build(bot_settings)
    update = _FakeUpdate(user_id=OWNER_ID, chat_id=CHAT_ID)
    replies = await _invoke(handlers, "viral_now", update)

    assert "id=42" in replies[0]
    assert "8.5" in replies[0]
