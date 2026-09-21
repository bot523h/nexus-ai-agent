"""Behavioural tests for the single bot access gate (P0-2).

The audit found authorization on 2 of ~80 commands.  These tests pin the
replacement: one decision function in front of every update, deny-by-default
when an owner or allow-list is configured, and explicit reasons so a refusal
can be explained instead of guessed at.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop, TypeHandler

from nexus_ai_agent.bot.middleware import (
    PUBLIC_COMMANDS,
    AuthMiddleware,
    BotAccessGate,
    RateLimiter,
    command_name,
)
from nexus_ai_agent.config.settings import Settings

OWNER = 99
ALLOWED = 7
STRANGER = 4242


def _entity(offset: int, length: int, type_: str = "bot_command") -> SimpleNamespace:
    return SimpleNamespace(offset=offset, length=length, type=type_)


def _text_update(text: str, *, user_id: int | None = STRANGER) -> SimpleNamespace:
    """A duck-typed update carrying one bot-command entity, like PTB's."""
    message = SimpleNamespace(
        text=text,
        caption=None,
        entities=[_entity(0, len(text.split()[0]))] if text.startswith("/") else [],
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        message=message,
        edited_message=None,
        channel_post=None,
        callback_query=None,
    )


def _gate(allowed: list[int] | None = None, owner: int = 0) -> BotAccessGate:
    return BotAccessGate(AuthMiddleware(allowed or [], owner))


# ── command extraction ────────────────────────────────────────────────────


def test_command_name_handles_plain_mentioned_and_argument_forms() -> None:
    assert command_name(_text_update("/start")) == "start"
    assert command_name(_text_update("/Start")) == "start"
    assert command_name(_text_update("/start@nexus_bot")) == "start"
    assert command_name(_text_update("/calc 2+2")) == "calc"
    assert command_name(_text_update("hello there")) is None


def test_command_name_survives_an_edit_or_a_button_press() -> None:
    edited = _text_update("/start")
    edited.message = None
    edited.edited_message = SimpleNamespace(text="/start", caption=None, entities=[_entity(0, 6)])
    assert command_name(edited) == "start"

    via_button = _text_update("")
    via_button.message = None
    via_button.callback_query = SimpleNamespace(
        message=SimpleNamespace(text="/start", caption=None, entities=[_entity(0, 6)])
    )
    assert command_name(via_button) == "start"


# ── gate policy ───────────────────────────────────────────────────────────


def test_public_deployment_does_not_lock_everyone_out() -> None:
    """No owner + empty allow-list = public bot: the gate must stay open."""
    gate = _gate()
    assert gate.enforcing is False
    decision = gate.decide(_text_update("/ai burn my quota"))
    assert decision.allowed is True
    assert decision.reason == "public_mode"


def test_allowlist_deployment_denies_a_stranger_by_default() -> None:
    gate = _gate(owner=OWNER)
    assert gate.enforcing is True
    decision = gate.decide(_text_update("/ai burn my quota"))
    assert decision.allowed is False
    assert decision.reason == "not_authorized"
    assert decision.user_id == STRANGER


def test_allowlist_deployment_admits_owner_and_allowlisted_users() -> None:
    gate = _gate(allowed=[ALLOWED], owner=OWNER)
    assert gate.decide(_text_update("/ai hi", user_id=OWNER)).reason == "authorized"
    assert gate.decide(_text_update("/ai hi", user_id=ALLOWED)).reason == "authorized"


def test_public_commands_stay_reachable_for_a_stranger() -> None:
    gate = _gate(owner=OWNER)
    for command in sorted(PUBLIC_COMMANDS):
        decision = gate.decide(_text_update(f"/{command}"))
        assert decision.allowed is True, command
        assert decision.reason == "public_command"


def test_an_update_without_an_identity_is_not_refused() -> None:
    gate = _gate(owner=OWNER)
    decision = gate.decide(_text_update("/ai hi", user_id=None))
    assert decision.allowed is True
    assert decision.reason == "no_user_identity"


def test_expensive_command_is_refused_while_start_is_not() -> None:
    """The exact hole from the audit: /ai was callable by anyone."""
    gate = _gate(owner=OWNER)
    assert gate.decide(_text_update("/ai hello")).allowed is False
    assert gate.decide(_text_update("/imagine a cat")).allowed is False
    assert gate.decide(_text_update("/start")).allowed is True


# ── PTB wiring ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_handler_stops_processing_for_a_refused_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.bot.app import ACCESS_GATE_GROUP, build_access_gate_handler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    monkeypatch.setenv("NEXUS_ALLOWED_USER_IDS", "")

    handler = build_access_gate_handler(Settings())
    assert isinstance(handler, TypeHandler)
    assert ACCESS_GATE_GROUP == -1

    replies: list[str] = []
    # Duck-typed update: the gate reads attributes, never PTB internals.
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=STRANGER),
        effective_message=SimpleNamespace(reply_text=_recorder(replies)),
        callback_query=None,
        message=SimpleNamespace(text="/ai hello", caption=None, entities=[_entity(0, 3)]),
        edited_message=None,
        channel_post=None,
    )
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(update, SimpleNamespace())
    assert replies and "مجاز نیستید" in replies[0]


@pytest.mark.asyncio
async def test_gate_handler_lets_an_authorized_update_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.bot.app import build_access_gate_handler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))

    handler = build_access_gate_handler(Settings())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER),
        effective_message=None,
        callback_query=None,
        message=SimpleNamespace(text="/ai hello", caption=None, entities=[_entity(0, 3)]),
        edited_message=None,
        channel_post=None,
    )
    # No ApplicationHandlerStop → the update continues to the command handlers.
    await handler.callback(update, SimpleNamespace())


def _recorder(sink: list[str]):
    async def _reply_text(text: str, **_kwargs: object) -> None:
        sink.append(text)

    return _reply_text


# ── rate limiter memory bound ─────────────────────────────────────────────


def test_rate_limiter_still_blocks_within_a_window() -> None:
    limiter = RateLimiter(max_messages=2, window_seconds=60)
    assert limiter.is_allowed(1) is True
    assert limiter.is_allowed(1) is True
    assert limiter.is_allowed(1) is False


def test_rate_limiter_state_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = RateLimiter(max_messages=5, window_seconds=60)
    monkeypatch.setattr(RateLimiter, "MAX_TRACKED_USERS", 10)
    for user_id in range(50):
        assert limiter.is_allowed(user_id) is True
    assert len(limiter._events) <= 10
    # A tracked user is still limited after eviction pressure.
    assert limiter.is_allowed(49) is True
    limiter2 = RateLimiter(max_messages=1, window_seconds=60)
    assert limiter2.is_allowed(49) is True
    assert limiter2.is_allowed(49) is False


def test_in_memory_rate_limiter_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.bot.rate_limiter import InMemoryRateLimiter

    limiter = InMemoryRateLimiter(limit=2, period=60.0)
    monkeypatch.setattr(InMemoryRateLimiter, "MAX_TRACKED_USERS", 10)
    for user_id in range(40):
        assert limiter.is_allowed(user_id) is True
    assert len(limiter._events) <= 10
    assert limiter.is_allowed(39) is True
    assert limiter.is_allowed(39) is False
