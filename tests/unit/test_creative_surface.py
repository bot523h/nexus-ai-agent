"""Creative surface — the Telegram entrypoint of the canonical chain.

Two layers are pinned here:

* the pure mapper (no Telegram, no disk): validation, normalization, the honest
  operation set, limits;
* the thin handler: staging, enqueue, idempotency — and, above all, that **no
  raw i18n key ever reaches the user** (owner directive §6).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot.creative_surface import (
    COMMANDS,
    CreativeErrorCode,
    CreativeFailure,
    CreativeRequest,
    CreativeSurfaceMapper,
    build_creative_handlers,
)

# ---------------------------------------------------------------------------
# mapper (pure)
# ---------------------------------------------------------------------------


def _req(**overrides: Any) -> CreativeRequest:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ("0", "5"),
        "media_file_id": "file123",
        "media_duration_s": 5.0,
    }
    base.update(overrides)
    return CreativeRequest(**base)


def test_mapper_accepts_the_executable_set() -> None:
    mapper = CreativeSurfaceMapper()
    for command, operation in (
        ("edit", "trim"),
        ("edit", "speed"),
        ("edit", "reverse"),
        ("grade", "exposure"),
    ):
        mapped = mapper.map(_req(command=command, operation=operation, args=()))
        assert not isinstance(mapped, CreativeFailure), (command, operation)


def test_mapper_rejects_unknown_command_and_operation() -> None:
    mapper = CreativeSurfaceMapper()
    unknown_command = mapper.map(_req(command="teleport"))
    assert isinstance(unknown_command, CreativeFailure)
    assert unknown_command.code is CreativeErrorCode.INVALID_REQUEST

    unknown_operation = mapper.map(_req(operation="explode"))
    assert isinstance(unknown_operation, CreativeFailure)
    assert unknown_operation.code is CreativeErrorCode.INVALID_OPERATION


@pytest.mark.parametrize(
    ("command", "operation"),
    [("caption", "transcribe"), ("caption", "burnin"), ("grade", "lut"), ("grade", "proxy")],
)
def test_operations_without_a_lane_primitive_are_refused_not_queued(
    command: str, operation: str
) -> None:
    """No fake success: an op that cannot render is refused, never queued."""
    mapper = CreativeSurfaceMapper()
    mapped = mapper.map(_req(command=command, operation=operation, args=()))
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code is CreativeErrorCode.NOT_AVAILABLE
    assert mapped.message_key.startswith("creative.")


def test_mapper_requires_replied_media() -> None:
    mapper = CreativeSurfaceMapper()
    mapped = mapper.map(_req(media_file_id=None))
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code is CreativeErrorCode.NOT_REPLIED


def test_mapper_enforces_the_duration_limit() -> None:
    mapper = CreativeSurfaceMapper()
    mapped = mapper.map(_req(media_duration_s=99.0))
    assert isinstance(mapped, CreativeFailure)
    assert mapped.code is CreativeErrorCode.LIMIT_EXCEEDED


def test_mapper_normalizes_numeric_args_and_rejects_junk() -> None:
    mapper = CreativeSurfaceMapper()
    assert mapper.map(_req(args=("1.5", "2.5"))) is not None
    bad = mapper.map(_req(args=("abc",)))
    assert isinstance(bad, CreativeFailure)
    assert bad.code is CreativeErrorCode.INVALID_REQUEST


def test_idempotency_key_is_anchored_to_the_message() -> None:
    mapper = CreativeSurfaceMapper()
    req = _req(message_id=77)
    first = mapper.idempotency_key(req, chat_id=1, user_id=2)
    assert first == mapper.idempotency_key(req, chat_id=1, user_id=2)
    assert first != mapper.idempotency_key(_req(message_id=78), chat_id=1, user_id=2)


# ---------------------------------------------------------------------------
# handler (fake queue, fake bot)
# ---------------------------------------------------------------------------


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict[str, Any]]] = []
        self.should_fail = False

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict[str, Any]) -> str:
        if self.should_fail:
            raise RuntimeError("queue down")
        self.enqueued.append((job_type, idempotency_key, payload))
        return "job-42"

    async def get_status(self, job_id: str) -> Any:
        raise NotImplementedError

    async def get_result(self, job_id: str) -> Any:
        raise NotImplementedError


class _FakeMessage:
    def __init__(self, *, file_id: str = "fid123", duration: Any = 5, name: str = "clip.mp4"):
        self.message_id = 77
        self.replies: list[str] = []
        self.reply_to_message = SimpleNamespace(
            video=SimpleNamespace(file_id=file_id, duration=duration, file_name=name)
        )

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def _update(message: Any, user_id: int = 1, chat_id: int = 10, language: str = "en") -> Any:
    return SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=SimpleNamespace(id=user_id, language_code=language),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _context(args: list[str]) -> Any:
    return SimpleNamespace(args=args, bot=None)


@pytest.fixture()
def _temp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from nexus_ai_agent.config import settings as settings_module

    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    settings_module.get_settings.cache_clear()
    yield tmp_path / "creative"
    settings_module.get_settings.cache_clear()


async def _stage_ok(bot: Any, file_id: str, destination: Path) -> None:
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_bytes(b"\x00\x00\x00\x18ftypmp42staged")


async def _stage_boom(bot: Any, file_id: str, destination: Path) -> None:
    raise RuntimeError("telegram said no")


async def test_handler_enqueues_staged_media_with_the_worker_envelope(_temp_dir: Path) -> None:
    queue = _FakeQueue()
    handlers = build_creative_handlers(queue, stage_media=_stage_ok)  # type: ignore[arg-type]
    message = _FakeMessage()
    await handlers["edit"](_update(message), _context(["trim", "0", "4"]))

    assert len(queue.enqueued) == 1
    job_type, idempotency_key, payload = queue.enqueued[0]
    assert job_type == "creative_render"
    assert idempotency_key
    assert payload["command"] == "edit"
    assert payload["operation"] == "trim"
    assert payload["args"] == ["0", "4"]
    assert payload["user_id"] == 1
    assert payload["chat_id"] == 10
    assert payload["lang"] == "en"
    staged = Path(str(payload["input_path"]))
    assert staged.is_file()
    assert staged.is_relative_to(Path(str(payload["workspace_dir"])))
    assert Path(str(payload["workspace_dir"])).name.startswith("creative_")
    assert message.replies and "job-42" in message.replies[-1]


async def test_handler_reply_is_localized_and_never_a_raw_key(_temp_dir: Path) -> None:
    """The reply must be the *translated* string, in every locale we ship."""
    from nexus_ai_agent.i18n import I18n

    i18n = I18n()
    for lang in i18n.get_available_languages():
        queue = _FakeQueue()
        handlers = build_creative_handlers(queue, stage_media=_stage_ok)  # type: ignore[arg-type]
        message = _FakeMessage()
        await handlers["edit"](_update(message, language=lang), _context([]))

        assert message.replies, lang
        reply = message.replies[-1]
        assert "creative." not in reply, f"{lang}: raw key leaked: {reply}"
        # The refusal is rendered from the catalog, never echoed verbatim.
        assert reply != i18n.t("creative.invalid_operation", lang=lang)


async def test_handler_reports_queue_failure_in_the_user_language(_temp_dir: Path) -> None:
    queue = _FakeQueue()
    queue.should_fail = True
    handlers = build_creative_handlers(queue, stage_media=_stage_ok)  # type: ignore[arg-type]
    message = _FakeMessage()
    await handlers["edit"](_update(message, language="fa"), _context(["reverse"]))

    reply = message.replies[-1]
    assert "creative." not in reply
    assert reply != "⚠️ Could not queue the job. Please try again."
    # The staged workspace must not survive a failed enqueue.
    assert not list((_temp_dir).glob("creative_*"))


async def test_handler_reports_media_failure_without_enqueuing(_temp_dir: Path) -> None:
    queue = _FakeQueue()
    handlers = build_creative_handlers(queue, stage_media=_stage_boom)  # type: ignore[arg-type]
    message = _FakeMessage()
    await handlers["edit"](_update(message), _context(["reverse"]))

    assert queue.enqueued == []
    assert "creative." not in message.replies[-1]
    assert not list((_temp_dir).glob("creative_*"))


async def test_handler_refuses_unavailable_operation_before_staging(_temp_dir: Path) -> None:
    queue = _FakeQueue()

    async def _never(bot: Any, file_id: str, destination: Path) -> None:  # pragma: no cover
        raise AssertionError("staging must not run for an unavailable operation")

    handlers = build_creative_handlers(queue, stage_media=_never)  # type: ignore[arg-type]
    message = _FakeMessage()
    await handlers["caption"](_update(message), _context(["burnin"]))

    assert queue.enqueued == []
    assert "creative." not in message.replies[-1]


def test_surface_owns_exactly_three_commands() -> None:
    queue = _FakeQueue()
    handlers = build_creative_handlers(queue)  # type: ignore[arg-type]
    assert set(handlers) == set(COMMANDS) == {"edit", "caption", "grade"}
