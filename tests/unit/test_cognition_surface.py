"""E2E-ish tests for /ai cognition surface (no real Gemini)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus_ai_agent.bot.cognition_surface import build_cognition_ai_handler
from nexus_ai_agent.cognition.port import CognitionError, CognitionRequest, CognitionResult


class _FakeCog:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.last_request: CognitionRequest | None = None

    async def propose(self, request: CognitionRequest) -> CognitionResult:
        self.last_request = request
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _update(text_args: list[str], *, chat_id=1, user_id=2, message_id=99):
    msg = MagicMock()
    msg.message_id = message_id
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.effective_user = SimpleNamespace(id=user_id)
    context = MagicMock()
    context.args = text_args
    context.application = None
    return update, context, msg


@pytest.mark.asyncio
async def test_ai_path_happy_path_correlation() -> None:
    cog = _FakeCog(
        result=CognitionResult(
            text="hello-out",
            correlation_id="x",
            provider_id="fake",
            route_reason="test",
        )
    )
    handler = build_cognition_ai_handler(cog)
    update, context, msg = _update(["hi", "there"], message_id=42)
    await handler(update, context)
    assert cog.last_request is not None
    assert cog.last_request.correlation_id == "tg:1:2:42"
    assert cog.last_request.prompt == "hi there"
    msg.reply_text.assert_awaited()
    assert "hello-out" in msg.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_provider_error_does_not_leak_secret() -> None:
    secret = "API_KEY=sk-super-secret-do-not-leak"
    cog = _FakeCog(error=CognitionError("provider_error", secret, correlation_id="c1"))
    handler = build_cognition_ai_handler(cog)
    update, context, msg = _update(["probe"])
    await handler(update, context)
    sent = msg.reply_text.await_args.args[0]
    assert secret not in sent
    assert "sk-super-secret" not in sent


@pytest.mark.asyncio
async def test_provider_unavailable_safe_message() -> None:
    cog = _FakeCog(
        error=CognitionError("provider_unavailable", "internal detail", correlation_id="c2")
    )
    handler = build_cognition_ai_handler(cog)
    update, context, msg = _update(["x"])
    await handler(update, context)
    sent = msg.reply_text.await_args.args[0]
    assert "internal detail" not in sent
    assert "unavailable" in sent.lower()
