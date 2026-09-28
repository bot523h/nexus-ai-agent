"""Exercise real internal boundaries; fake only Telegram and remote providers.

In particular the session factory is NOT mocked, and success-path media tests
use the real engine's returned dictionary, not a second handwritten contract.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import select

from nexus_ai_agent.bot.handlers import _upsert_chat, _upsert_user, build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features import image_gen
from nexus_ai_agent.features.onboarding import is_first_time_user
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserLanguage

USER_ID = 123
CHAT_ID = 456


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    for key in ("NEXUS_DATABASE_URL", "DATABASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(USER_ID))
    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    image_gen._image_cache.clear()
    image_gen._last_request.clear()
    settings = settings_module.get_settings()
    from nexus_ai_agent.storage.migrations import ensure_startup_schema

    ensure_startup_schema()
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"response": "graph reached"}))
    handlers = build_handlers(graph, get_session, settings, PresenceStore(), object())
    commands = {
        command: handler.callback
        for handler in handlers
        for command in getattr(handler, "commands", ())
    }
    yield SimpleNamespace(commands=commands, handlers=handlers, graph=graph, root=tmp_path)
    settings_module.get_settings.cache_clear()
    image_gen._image_cache.clear()
    image_gen._last_request.clear()


def update(user: int = USER_ID, text: str = "hello") -> Any:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_voice=AsyncMock(),
        reply_document=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user, username="before"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=message,
        edited_message=None,
        callback_query=None,
    )


def context(*args: str) -> Any:
    return SimpleNamespace(args=list(args), bot=SimpleNamespace(), bot_data={})


async def test_upserts_return_models_and_update_existing_rows(runtime: Any) -> None:
    user = await _upsert_user(get_session, update().effective_user)
    chat = await _upsert_chat(get_session, CHAT_ID, "tg:old")
    assert isinstance(user, User) and isinstance(chat, Chat)
    other = SimpleNamespace(id=USER_ID, username="after")
    assert (await _upsert_user(get_session, other)).id == user.id
    assert (await _upsert_chat(get_session, CHAT_ID, "tg:new")).id == chat.id
    async with get_session() as session:
        users = (await session.execute(select(User))).scalars().all()
        chats = (await session.execute(select(Chat))).scalars().all()
    assert [(u.telegram_id, u.username) for u in users] == [(USER_ID, "after")]
    assert [(c.chat_id, c.thread_id) for c in chats] == [(CHAT_ID, "tg:new")]


async def test_message_reaches_graph_after_real_database_writes(runtime: Any) -> None:
    incoming = update()
    callback = next(h.callback for h in runtime.handlers if h.callback.__name__ == "on_message")
    await callback(incoming, context())
    runtime.graph.ainvoke.assert_awaited_once()
    incoming.message.reply_text.assert_any_await("graph reached")
    async with get_session() as session:
        assert (await session.execute(select(User))).scalar_one().telegram_id == USER_ID
        assert (await session.execute(select(Chat))).scalar_one().chat_id == CHAT_ID


async def test_language_insert_read_update_and_onboarding(runtime: Any) -> None:
    assert await is_first_time_user(USER_ID, get_session)
    incoming = update()
    await runtime.commands["language"](incoming, context("fa"))
    assert not await is_first_time_user(USER_ID, get_session)
    await runtime.commands["language"](incoming, context())
    assert "فارسی" in incoming.message.reply_text.call_args.args[0]
    await runtime.commands["language"](incoming, context("en"))
    async with get_session() as session:
        rows = (await session.execute(select(UserLanguage))).scalars().all()
    assert [(r.user_id, r.language) for r in rows] == [(USER_ID, "en")]


async def test_onboarding_reads_an_existing_language_with_real_session(runtime: Any) -> None:
    async with get_session() as session:
        session.add(UserLanguage(user_id=USER_ID, language="fa"))
        await session.commit()
    assert not await is_first_time_user(USER_ID, get_session)


async def test_cloud_list_returns_owned_models_not_rows(runtime: Any) -> None:
    async with get_session() as session:
        session.add_all(
            [
                CloudFile(user_id=USER_ID, file_name="mine.pdf", file_size=1024),
                CloudFile(user_id=999, file_name="private.pdf", file_size=2048),
            ]
        )
        await session.commit()
    incoming = update()
    await runtime.commands["myfiles"](incoming, context())
    text = incoming.message.reply_text.call_args.args[0]
    assert "mine.pdf" in text and "private.pdf" not in text


async def test_cloud_download_uses_owned_model(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage.unified_cloud import UnifiedCloudStorage

    async with get_session() as session:
        session.add(CloudFile(user_id=USER_ID, file_name="mine.pdf", remote_path="owned/key"))
        session.add(CloudFile(user_id=999, file_name="private.pdf", remote_path="private/key"))
        await session.commit()
    remote = AsyncMock(return_value={"data": b"pdf", "error": None})
    monkeypatch.setattr(UnifiedCloudStorage, "download_file", remote)
    incoming = update()
    await runtime.commands["download"](incoming, context("private.pdf"))
    remote.assert_not_awaited()
    await runtime.commands["download"](incoming, context("mine.pdf"))
    assert remote.call_args.args[0] == "owned/key"
    assert incoming.message.reply_document.call_args.kwargs["document"].getvalue() == b"pdf"


def remote_media(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    if command == "image":
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"image bytes"))
        monkeypatch.setattr(
            image_gen.httpx, "AsyncClient", partial(real_client, transport=transport)
        )
    else:
        import gtts

        class RemoteTTS:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def save(self, filename: str) -> None:
                Path(filename).write_bytes(b"audio bytes")

        monkeypatch.setattr(gtts, "gTTS", RemoteTTS)


@pytest.mark.parametrize(
    "command,method,payload",
    [
        ("image", "reply_photo", b"image bytes"),
        ("tts", "reply_voice", b"audio bytes"),
    ],
)
@pytest.mark.parametrize("delivery", ["success", "failure", "io_failure", "cancelled"])
async def test_real_media_result_reaches_telegram_and_closes_file(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    method: str,
    payload: bytes,
    delivery: str,
) -> None:
    remote_media(monkeypatch, command)
    incoming = update()
    opened = []

    async def send(**kwargs: Any) -> None:
        handle = kwargs["photo" if command == "image" else "voice"]
        opened.append(handle)
        assert not handle.closed
        assert handle.read() == payload
        if delivery == "failure":
            raise RuntimeError("telegram unavailable")
        if delivery == "io_failure":
            raise OSError("telegram transport failed")
        if delivery == "cancelled":
            raise asyncio.CancelledError

    setattr(incoming.message, method, send)
    invocation = runtime.commands[command](incoming, context("hello", "world"))
    if delivery == "success":
        await invocation
    else:
        error = {
            "failure": RuntimeError,
            "io_failure": OSError,
            "cancelled": asyncio.CancelledError,
        }[delivery]
        with pytest.raises(error):
            await invocation
    assert len(opened) == 1 and opened[0].closed
    incoming.message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("command", ["image", "tts"])
@pytest.mark.parametrize(
    "bad_result",
    [
        {"success": False, "path": "should-not-send", "error": None},
        {"success": True, "file_path": "legacy-key", "error": None},
        {"success": True, "path": "missing-file", "error": None},
        {"success": True, "path": "", "error": None},
        {"success": False, "path": None, "error": "provider refused"},
    ],
)
async def test_failed_or_invalid_media_never_claims_delivery(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    bad_result: dict,
) -> None:
    from nexus_ai_agent.features.speech import SpeechEngine

    engine, method = (
        (image_gen.ImageGenEngine, "generate")
        if command == "image"
        else (SpeechEngine, "text_to_speech")
    )
    monkeypatch.setattr(engine, method, AsyncMock(return_value=bad_result))
    incoming = update()
    await runtime.commands[command](incoming, context("hello", "world"))
    incoming.message.reply_photo.assert_not_awaited()
    incoming.message.reply_voice.assert_not_awaited()
    assert incoming.message.reply_text.call_count == 1


async def test_default_session_uses_configured_database(runtime: Any) -> None:
    async with get_session() as session:
        assert Path(session.bind.url.database) == runtime.root / "app.sqlite"
        session.add(User(telegram_id=789))
        await session.commit()
    async with get_session(str(runtime.root / "app.sqlite")) as session:
        assert (await session.execute(select(User))).scalar_one().telegram_id == 789
    assert not (runtime.root / "data" / "app.sqlite").exists()


async def test_same_relative_name_in_two_directories_is_not_the_same_database(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("first", "second"):
        directory = runtime.root / name
        directory.mkdir()
        monkeypatch.chdir(directory)
        async with get_session("relative.sqlite") as session:
            assert (await session.execute(select(User))).scalars().all() == []
            session.add(User(telegram_id=789))
            await session.commit()
        assert (directory / "relative.sqlite").is_file()


async def test_explicit_sqlite_path_overrides_postgres_environment(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://invalid.invalid/not-contacted")
    explicit = runtime.root / "explicit.sqlite"
    async with get_session(str(explicit)) as session:
        assert Path(session.bind.url.database) == explicit
        assert (await session.execute(select(User))).scalars().all() == []


async def test_telegram_dispatcher_enforces_access_and_reaches_real_db(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram import Update
    from telegram import User as TelegramUser
    from telegram.ext import Application, ExtBot

    from nexus_ai_agent.bot.access_guard import build_access_guard

    # Only Telegram's network boundary is replaced. Dispatcher, filters,
    # guard, callbacks, sessions and models remain the production code.
    monkeypatch.setattr(ExtBot, "initialize", AsyncMock())
    monkeypatch.setattr(ExtBot, "shutdown", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(ExtBot, "send_message", send)
    bot = ExtBot("123:TEST")
    bot._bot_user = TelegramUser(42, "Nexus", is_bot=True, username="nexus_test_bot")
    application = Application.builder().bot(bot).build()
    application.add_handler(build_access_guard(settings_module.get_settings()), group=-1)
    application.add_handlers(runtime.handlers)
    errors = AsyncMock()
    application.add_error_handler(errors)
    await application.initialize()
    try:
        for user_id in (999, USER_ID):
            incoming = Update.de_json(
                {
                    "update_id": user_id,
                    "message": {
                        "message_id": user_id,
                        "date": 1,
                        "chat": {"id": CHAT_ID, "type": "private"},
                        "from": {"id": user_id, "first_name": "Test", "is_bot": False},
                        "text": "hello",
                    },
                },
                bot,
            )
            await application.process_update(incoming)
        errors.assert_not_awaited()
        runtime.graph.ainvoke.assert_awaited_once()
        assert any(call.kwargs.get("text") == "graph reached" for call in send.call_args_list)
        async with get_session() as session:
            users = (await session.execute(select(User))).scalars().all()
        assert [user.telegram_id for user in users] == [USER_ID]
    finally:
        await application.shutdown()
