"""Tests for the global deny-by-default access guard (P0-2)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from nexus_ai_agent.bot.access_guard import AccessGuardHandler, build_access_guard
from nexus_ai_agent.config.settings import Settings


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


async def test_guard_blocks_stranger() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: uid == 99)
    assert await guard.check_update(update_for(user_id=1)) is True
    # Handling a denied command update replies once.
    update = update_for(user_id=1)
    await guard._denied(update, None)
    update.message.reply_text.assert_awaited_once()


async def test_guard_allows_listed_user_through() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: uid in {99, 7})
    # check_update False → PTB skips the handler entirely; the update
    # flows to the real handlers unimpeded.
    assert await guard.check_update(update_for(user_id=7)) is False
    assert await guard.check_update(update_for(user_id=99)) is False


async def test_guard_no_user_passes_through() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    assert await guard.check_update(update_for(user_id=None)) is False


async def test_denial_rate_limit_value() -> None:
    from nexus_ai_agent.bot.access_guard import _DENIAL_REPLY_LIMIT

    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    replies = 0
    for _ in range(_DENIAL_REPLY_LIMIT + 5):
        update = update_for(user_id=1)
        await guard._denied(update, None)
        replies += update.message.reply_text.await_count
    assert replies == _DENIAL_REPLY_LIMIT


async def test_denied_callback_query_gets_alert() -> None:
    guard = AccessGuardHandler(is_allowed=lambda uid: False)
    update = callback_update_for(user_id=1)
    await guard._denied(update, None)
    update.callback_query.answer.assert_awaited_once()


async def test_build_access_guard_from_settings() -> None:
    # No owner configured, empty allow-list → deny-by-default: everyone
    # except… nobody.
    guard = build_access_guard(Settings())
    assert await guard.check_update(update_for(user_id=123)) is True

    # Owner configured → the owner passes through.
    owner_settings = Settings().model_copy(update={"owner_telegram_id": 42})
    guard_owner = build_access_guard(owner_settings)
    assert await guard_owner.check_update(update_for(user_id=42)) is False
    assert await guard_owner.check_update(update_for(user_id=123)) is True
