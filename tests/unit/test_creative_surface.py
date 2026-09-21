"""Creative surface — wave-4 step5.

Pure mapper tests + handler integration with a fake JobQueuePort.
No network, no Telegram, no FFmpeg.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot.creative_surface import (
    CreativeErrorCode,
    CreativeFailure,
    CreativeRequest,
    CreativeSurfaceMapper,
    build_creative_handlers,
)

# -- mapper (pure) ----------------------------------------------------------


def test_mapper_accepts_valid_edit_trim() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("edit", "trim", ("0", "5"), "file123", 5.0)
    assert not isinstance(m.map(req), CreativeFailure)


def test_mapper_rejects_unknown_command() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("unknown", "trim", (), "file123", 5.0)
    mapped = m.map(req)
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code == CreativeErrorCode.INVALID_REQUEST


def test_mapper_rejects_unknown_operation() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("edit", "unknown_op", (), "file123", 5.0)
    mapped = m.map(req)
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code == CreativeErrorCode.INVALID_REQUEST


def test_mapper_requires_media_when_needed() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("edit", "trim", (), None, None)
    mapped = m.map(req)
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code == CreativeErrorCode.NOT_REPLIED


def test_mapper_otio_does_not_require_media() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("grade", "otio", (), None, None)
    assert not isinstance(m.map(req), CreativeFailure)


def test_mapper_enforces_duration_limit() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("edit", "trim", (), "file123", 99.0)
    mapped = m.map(req)
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code == CreativeErrorCode.LIMIT_EXCEEDED


def test_job_payload_contains_ids() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("caption", "transcribe", (), "fid", 10.0)
    payload = m.job_payload(req, 123, 456)
    assert payload["user_id"] == 123
    assert payload["chat_id"] == 456
    assert payload["command"] == "caption"


# -- handler integration (fake queue) ---------------------------------------


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []
        self.should_fail = False

    async def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> str:
        _ = idempotency_key
        if self.should_fail:
            raise RuntimeError("queue down")
        self.enqueued.append((job_type, payload))
        return "job-42"


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_to_message: Any = None

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def _make_update(message: _FakeMessage, user_id: int = 1, chat_id: int = 10) -> Any:
    return SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _make_context(args: list[str]) -> Any:
    return SimpleNamespace(args=args, bot=None)


@pytest.mark.asyncio
async def test_handler_queues_valid_request() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage()
    msg.reply_to_message = SimpleNamespace(video=SimpleNamespace(file_id="fid123", duration=5))
    update = _make_update(msg)
    context = _make_context(["trim", "0", "5"])
    await handlers["edit"](update, context)
    assert len(q.enqueued) == 1
    assert q.enqueued[0][0] == "creative_render"
    assert any("Queued" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_handler_rejects_when_not_replied() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage()
    msg.reply_to_message = None
    update = _make_update(msg)
    context = _make_context(["trim"])
    await handlers["edit"](update, context)
    assert len(q.enqueued) == 0
    assert any("not_replied" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_handler_reports_queue_failure() -> None:
    q = _FakeQueue()
    q.should_fail = True
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage()
    msg.reply_to_message = SimpleNamespace(video=SimpleNamespace(file_id="fid", duration=5))
    update = _make_update(msg)
    context = _make_context(["speed", "1.5"])
    await handlers["edit"](update, context)
    assert any("queue failed" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_handler_rejects_limit_exceeded() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage()
    msg.reply_to_message = SimpleNamespace(video=SimpleNamespace(file_id="fid", duration=100))
    update = _make_update(msg)
    context = _make_context(["reverse"])
    await handlers["edit"](update, context)
    assert len(q.enqueued) == 0
    assert any("limit_exceeded" in r for r in msg.replies)


def test_build_returns_mapper() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    assert isinstance(handlers["mapper"], CreativeSurfaceMapper)
