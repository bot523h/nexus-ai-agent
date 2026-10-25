"""Creative surface — mapper tests + handler integration with a fake queue/bot.

No network, no Telegram, no FFmpeg. The fake bot stages media by writing a
marker file, exactly like ``download_to_drive`` would.

Covers the task-166 (P0-B) regression battery:

* the handler map contains EXACTLY edit/caption/grade (never a ``mapper`` key);
* idempotency is anchored to the Telegram message id: same message twice →
  one idempotency key; two messages → two keys;
* every user-visible reply is resolved through the i18n catalog — no raw
  ``creative.*`` key may ever leak;
* replied media is staged into a job workspace and the payload carries it;
* dropped ops (lut/burnin) fail at the mapper as unsupported.
"""

from __future__ import annotations

from pathlib import Path
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
from nexus_ai_agent.config import settings as settings_module

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


def test_mapper_enforces_size_limit_when_telegram_reports_it() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("edit", "trim", (), "file123", 5.0, media_file_size=10**9)
    mapped = m.map(req)
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code == CreativeErrorCode.MEDIA_TOO_LARGE


def test_mapper_rejects_lut_and_burnin_as_invalid() -> None:
    """Dropped ops (lut, burnin) must never reach the queue (task-166)."""
    m = CreativeSurfaceMapper()
    assert isinstance(m.map(CreativeRequest("grade", "lut", (), "f", 5.0)), CreativeFailure)
    assert isinstance(m.map(CreativeRequest("caption", "burnin", (), "f", 5.0)), CreativeFailure)


@pytest.mark.parametrize(
    ("command", "operation", "args"),
    [
        ("grade", "exposure", ("1.0",)),
        ("grade", "proxy", ()),
        ("grade", "otio", ()),
        ("caption", "transcribe", ()),
        ("edit", "trim", ("0", "5")),
    ],
)
def test_job_payload_carries_no_lifecycle_opt_in(
    command: str, operation: str, args: tuple[str, ...]
) -> None:
    """task-183: the surface never writes a lifecycle opt-in into the job row
    (the worker derives it from server policy), and every row it produces is a
    valid worker payload (positive control for the surface→queue contract)."""
    from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload

    m = CreativeSurfaceMapper()
    payload = m.job_payload(
        CreativeRequest(command, operation, args, "fid", 10.0),
        user_id=1,
        chat_id=2,
        lang="en",
        idempotency_key="creative:1:2:3",
        workspace_dir="/tmp/creative_x",
        input_path="/tmp/creative_x/input.mp4",
    )
    assert "allow_experimental" not in payload
    CreativeRenderPayload.model_validate(payload)


def test_job_payload_contains_ids_and_workspace() -> None:
    m = CreativeSurfaceMapper()
    req = CreativeRequest("caption", "transcribe", (), "fid", 10.0)
    payload = m.job_payload(
        req,
        user_id=123,
        chat_id=456,
        lang="fa",
        idempotency_key="creative:123:456:7",
        workspace_dir="/tmp/creative_x",
        input_path="/tmp/creative_x/input.mp4",
    )
    assert payload["user_id"] == 123
    assert payload["chat_id"] == 456
    assert payload["command"] == "caption"
    assert payload["media_duration_us"] == 10_000_000
    assert payload["workspace_dir"] == "/tmp/creative_x"


# -- handler integration (fake queue + fake bot) -----------------------------


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.should_fail = False

    @property
    def enqueued(self) -> list[tuple[str, dict[str, Any]]]:
        return [(job_type, payload) for job_type, _, payload in self.calls]

    @property
    def enqueued_keys(self) -> list[str]:
        return [key for _, key, _ in self.calls]

    async def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> str:
        if self.should_fail:
            raise RuntimeError("queue down")
        self.calls.append((job_type, idempotency_key, payload))
        return "job-x"


class _FakeTelegramFile:
    def __init__(self, file_path: str = "videos/in.mp4") -> None:
        self.file_path = file_path

    async def download_to_drive(self, target: Any) -> None:
        Path(str(target)).write_bytes(b"fake-media-bytes")


class _FakeBot:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> _FakeTelegramFile:
        self.requested.append(file_id)
        return _FakeTelegramFile()


class _FakeMessage:
    def __init__(self, message_id: int = 1) -> None:
        self.replies: list[str] = []
        self.reply_to_message: Any = None
        self.message_id = message_id

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def _make_update(
    message: _FakeMessage, user_id: int = 1, chat_id: int = 10, lang: str = "en"
) -> Any:
    return SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=user_id, language_code=lang),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _make_context(args: list[str], bot: Any | None = None) -> Any:
    return SimpleNamespace(args=args, bot=bot if bot is not None else _FakeBot())


def _video_update(message: _FakeMessage, *, duration: float = 5.0, lang: str = "en") -> Any:
    message.reply_to_message = SimpleNamespace(
        video=SimpleNamespace(file_id="fid123", duration=duration, file_size=1024)
    )
    return _make_update(message, lang=lang)


@pytest.fixture(autouse=True)
def _private_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path))
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_handler_queues_valid_request() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(42)
    update = _video_update(msg)
    context = _make_context(["trim", "0", "5"])
    await handlers["edit"](update, context)
    assert len(q.calls) == 1
    job_type, key, payload = q.calls[0]
    assert job_type == "creative_render"
    assert key == "creative:1:10:42"
    assert payload["operation"] == "trim"
    assert any("job-x" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_media_is_staged_and_payload_carries_paths() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(91)
    await handlers["edit"](_video_update(msg), _make_context(["trim", "0", "5"]))
    payload = q.calls[0][2]
    workspace = Path(payload["workspace_dir"])
    input_path = Path(payload["input_path"])
    assert workspace.name.startswith("creative_")
    assert input_path.parent == workspace
    assert input_path.exists()
    assert payload["media_duration_us"] == 5_000_000


@pytest.mark.asyncio
async def test_same_message_twice_maps_to_one_idempotency_key() -> None:
    """Redelivery of one Telegram message must not duplicate the logical job."""
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    context = _make_context(["trim", "0", "5"])
    for _ in range(2):
        msg = _FakeMessage(777)
        await handlers["edit"](_video_update(msg), context)
    assert q.enqueued_keys == ["creative:1:10:777", "creative:1:10:777"]


@pytest.mark.asyncio
async def test_distinct_messages_map_to_distinct_keys() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    context = _make_context(["reverse"])
    for mid in (101, 102):
        msg = _FakeMessage(mid)
        await handlers["edit"](_video_update(msg), context)
    assert len(set(q.enqueued_keys)) == 2


@pytest.mark.asyncio
async def test_replies_are_translated_not_raw_keys() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(5)  # no reply-to → NOT_REPLIED failure
    await handlers["edit"](_make_update(msg, lang="fa"), _make_context(["trim"]))
    assert msg.replies, "expected a user-visible reply"
    for reply in msg.replies:
        assert "creative." not in reply, f"raw i18n key leaked to the user: {reply}"
        assert "not_replied" not in reply
    assert any("ویدیو" in r or "پاسخ" in r for r in msg.replies), msg.replies


@pytest.mark.asyncio
async def test_queued_reply_is_translated_persian() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(66)
    await handlers["edit"](_video_update(msg, lang="fa"), _make_context(["trim", "0", "5"]))
    assert any("صف" in r and "job-x" in r for r in msg.replies), msg.replies


@pytest.mark.asyncio
async def test_failure_reply_for_bad_operation_lists_options_in_english() -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(7)
    await handlers["edit"](_video_update(msg), _make_context(["warp"]))
    text = msg.replies[-1]
    assert "warp" not in text.replace("/edit", "")
    assert "creative." not in text
    assert "reverse" in text and "trim" in text and "speed" in text


@pytest.mark.asyncio
async def test_handler_reports_queue_failure_translated() -> None:
    q = _FakeQueue()
    q.should_fail = True
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(8)
    await handlers["edit"](_video_update(msg, lang="fa"), _make_context(["speed", "1.5"]))
    assert msg.replies
    assert "creative." not in msg.replies[-1]
    assert any("صف" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_download_failure_is_a_typed_translated_failure() -> None:
    class _BoomBot:
        async def get_file(self, file_id: str) -> Any:
            raise RuntimeError("telegram down")

    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(9)
    await handlers["edit"](_video_update(msg), _make_context(["trim"], bot=_BoomBot()))
    assert q.calls == []
    assert "creative." not in msg.replies[-1]


def test_build_returns_only_command_handlers() -> None:
    """The handler map must never leak a bogus 'mapper' command (task-166)."""
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    assert sorted(handlers) == ["caption", "edit", "grade"]
