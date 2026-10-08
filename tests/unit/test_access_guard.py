"""Tests for the global deny-by-default access guard (P0-2).

Levels of proof:

1. ``check_update`` contract — sync method (PTB v21/v22 call it without
   awaiting), denies strangers, passes allow-listed users and updates
   without an effective user.
2. ``_denied`` contract — every denial path (rate-limited silent drop,
   callback alert, message reply) raises ``ApplicationHandlerStop``.
3. **Real dispatcher behaviour** — the guard and a canary handler are
   registered on a genuine ``telegram.ext.Application`` (built via
   ``ApplicationBuilder``) and driven through the real
   ``Application.process_update`` group loop, with only the network
   boundary faked (a ``telegram.request.BaseRequest`` implementation
   that answers Bot-API calls in memory). This is the code path
   production uses for both polling and webhook run-modes; a replica of
   the loop would prove nothing about PTB's actual semantics.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ApplicationHandlerStop, BaseHandler
from telegram.request import BaseRequest

from nexus_ai_agent.bot.access_guard import AccessGuardHandler, build_access_guard
from nexus_ai_agent.config.settings import Settings

DUMMY_TOKEN = "123456:TESTTOKENYYYYYYYYYYYYYYYYYYYYYY"


def update_for(user_id: int | None = 1, command: str | None = "/ai") -> SimpleNamespace:
    message = (
        SimpleNamespace(text=f"{command} hello", reply_text=AsyncMock())
        if command is not None
        else None
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        message=message,
        edited_message=None,
        callback_query=None,
    )


def callback_update_for(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=None,
        edited_message=None,
        callback_query=SimpleNamespace(answer=AsyncMock()),
    )


# --------------------------------------------------------------------- #
# 1. check_update contract (sync per telegram.ext.BaseHandler)
# --------------------------------------------------------------------- #


def test_check_update_is_synchronous() -> None:
    """PTB (v21.0 and v22.8) calls ``handler.check_update`` without awaiting.

    An ``async def check_update`` returns a coroutine — always truthy —
    so the dispatcher would run the guard for every update and never
    consult the allow-list.
    """
    import inspect

    guard = AccessGuardHandler(is_allowed=lambda uid: uid == 99)
    assert not inspect.iscoroutinefunction(guard.check_update)
    # …and calling it returns a bool, not a coroutine.
    result = guard.check_update(update_for(user_id=1))
    assert isinstance(result, bool)


def test_guard_blocks_stranger() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: uid == 99)
    assert guard.check_update(update_for(user_id=1)) is True


def test_guard_allows_listed_user_through() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: uid in {99, 7})
    # check_update False → PTB skips the handler entirely; the update
    # flows to the real handlers unimpeded.
    assert guard.check_update(update_for(user_id=7)) is False
    assert guard.check_update(update_for(user_id=99)) is False


def test_guard_no_user_passes_through() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    assert guard.check_update(update_for(user_id=None)) is False


def test_build_access_guard_from_settings() -> None:
    # No owner configured, empty allow-list → deny-by-default: everyone
    # except… nobody.
    guard = build_access_guard(Settings())
    assert guard.check_update(update_for(user_id=123)) is True

    # Owner configured → the owner passes through.
    owner_settings = Settings().model_copy(update={"owner_telegram_id": 42})
    guard_owner = build_access_guard(owner_settings)
    assert guard_owner.check_update(update_for(user_id=42)) is False
    assert guard_owner.check_update(update_for(user_id=123)) is True


# --------------------------------------------------------------------- #
# 2. _denied contract — STOP on every denial path
# --------------------------------------------------------------------- #


async def test_denied_message_update_replies_and_stops() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: uid == 99)
    update = update_for(user_id=1)
    with pytest.raises(ApplicationHandlerStop):
        await guard._denied(update, None)  # type: ignore[arg-type]
    update.message.reply_text.assert_awaited_once()


async def test_denial_rate_limit_value() -> None:
    from nexus_ai_agent.bot.access_guard import _DENIAL_REPLY_LIMIT

    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    replies = 0
    stops = 0
    for _ in range(_DENIAL_REPLY_LIMIT + 5):
        update = update_for(user_id=1)
        try:
            await guard._denied(update, None)  # type: ignore[arg-type]
        except ApplicationHandlerStop:
            stops += 1
        replies += update.message.reply_text.await_count
    assert replies == _DENIAL_REPLY_LIMIT
    # Every denied call must still raise Stop — a silently dropped
    # denial must not let the update continue to group 0.
    assert stops == _DENIAL_REPLY_LIMIT + 5


async def test_denied_callback_query_gets_alert_and_stops() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    update = callback_update_for(user_id=1)
    with pytest.raises(ApplicationHandlerStop):
        await guard._denied(update, None)  # type: ignore[arg-type]
    update.callback_query.answer.assert_awaited_once()


# --------------------------------------------------------------------- #
# 3. Real dispatcher behaviour (Application.process_update, no network)
# --------------------------------------------------------------------- #


class InMemoryTelegramAPI(BaseRequest):
    """Network boundary fake: answers Bot-API calls from memory.

    Only the HTTP layer is faked; the Application, its handler-group
    loop, ``ApplicationHandlerStop`` handling and the handlers
    themselves are the real PTB code paths used in production.
    """

    def __init__(self) -> None:
        self.posts: list[str] = []

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: Any = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        self.posts.append(url)
        if "getMe" in url:
            result: Any = {"id": 42, "is_bot": True, "first_name": "N", "username": "nexus_bot"}
        else:
            # sendMessage / answerCallbackQuery → an acknowledged call.
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


class CanaryHandler(BaseHandler[Update, Any, None]):
    """Group-0 canary: records every update PTB lets through to it."""

    def __init__(self) -> None:
        super().__init__(callback=self._record, block=True)
        self.calls: list[int] = []

    def check_update(self, update: Any) -> bool:
        return True

    async def _record(self, update: Any, context: Any) -> None:
        self.calls.append(int(update.effective_user.id))


def _message_payload(user_id: int, text: str = "/calc 2+2") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "X"},
            "chat": {"id": user_id, "type": "private"},
            "date": 1730000000,
            "text": text,
        },
    }


def _callback_payload(user_id: int) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "1",
            "from": {"id": user_id, "is_bot": False, "first_name": "X"},
            "chat_instance": "1",
            "data": "menu:main",
        },
    }


async def _drive(
    payloads: list[dict[str, Any]],
    *,
    allowed: set[int],
    concurrent: bool,
) -> tuple[list[int], list[str]]:
    """Run payloads through the *real* dispatcher; return canary calls + API posts."""
    api = InMemoryTelegramAPI()
    builder = ApplicationBuilder().token(DUMMY_TOKEN).request(api)
    if concurrent:
        builder = builder.concurrent_updates(True)
    app: Application = builder.build()  # type: ignore[assignment]
    guard = AccessGuardHandler(is_allowed=lambda uid: uid in allowed)
    canary = CanaryHandler()
    app.add_handler(guard, group=-1)
    app.add_handler(canary, group=0)
    await app.initialize()
    try:
        for payload in payloads:
            await app.process_update(Update.de_json(payload, app.bot))
    finally:
        await app.shutdown()
    return canary.calls, api.posts


async def test_real_dispatcher_blocks_unauthorized_command() -> None:
    calls, posts = await _drive([_message_payload(user_id=1)], allowed={99}, concurrent=False)
    assert calls == [], "denied update reached group 0 — fail-open"
    assert any("sendMessage" in p for p in posts), "denial UX should have been sent"


async def test_real_dispatcher_blocks_unauthorized_callback() -> None:
    calls, posts = await _drive([_callback_payload(user_id=1)], allowed={99}, concurrent=False)
    assert calls == [], "denied callback reached group 0 — fail-open"
    assert any("answerCallbackQuery" in p for p in posts)


async def test_real_dispatcher_blocks_unauthorized_with_concurrent_updates() -> None:
    """concurrent_updates=True wraps update processing in tasks, but the
    guard's ApplicationHandlerStop must still stop group 0."""
    calls, _ = await _drive([_message_payload(user_id=1)], allowed={99}, concurrent=True)
    assert calls == [], "denied update reached group 0 under concurrent_updates=True"


async def test_real_dispatcher_rate_limited_denial_still_blocks() -> None:
    """Past the denial-reply limit the guard goes silent — but the update
    must still never reach group 0."""
    from nexus_ai_agent.bot.access_guard import _DENIAL_REPLY_LIMIT

    payloads = [_message_payload(user_id=1)] * (_DENIAL_REPLY_LIMIT + 5)
    calls, posts = await _drive(payloads, allowed={99}, concurrent=False)
    assert calls == []
    # Exactly _DENIAL_REPLY_LIMIT denial replies, the rest dropped silently.
    assert sum(1 for p in posts if "sendMessage" in p) == _DENIAL_REPLY_LIMIT


async def test_real_dispatcher_allows_authorized_user() -> None:
    calls, posts = await _drive([_message_payload(user_id=99)], allowed={99}, concurrent=False)
    assert calls == [99], "authorized update must reach group 0"
    assert not any("sendMessage" in p for p in posts), "authorized user got denial UX"


async def test_real_dispatcher_allows_authorized_user_concurrent() -> None:
    calls, posts = await _drive([_message_payload(user_id=99)], allowed={99}, concurrent=True)
    assert calls == [99]
    assert not any("sendMessage" in p for p in posts)


async def test_guard_is_registered_with_block_true() -> None:
    """block=True → the dispatcher awaits the guard before continuing,
    keeping denial ordering deterministic."""
    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    assert guard.block is True
