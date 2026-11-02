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
    _ensure_private_directory,
    _StagingError,
    _trusted_root,
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


def _trace_id_for(user_id: int, chat_id: int, message_id: int | None) -> str:
    """Mint the trace exactly the way the surface does (task-215)."""
    from nexus_ai_agent.bot.creative_surface import execution_trace

    return execution_trace(user_id=user_id, chat_id=chat_id, message_id=message_id).trace_id


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
        trace_id=_trace_id_for(1, 2, 3),
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
        trace_id=_trace_id_for(123, 456, 7),
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


# -- P0 redelivery: content identity in the typed payload --------------------
#
# task-nagar-workspace-redelivery-01. These are the fast, pure assertions that
# back the integration suite: the payload MUST name the bytes it was admitted
# with, the digest MUST participate in the canonical request fingerprint, and the
# workspace name MUST stay compatible with the worker's guard. The real-queue
# proofs live in tests/integration/test_creative_surface_redelivery.py.


def _digest_of(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(Path(str(payload["input_path"])).read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_payload_carries_the_digest_of_the_staged_bytes(tmp_path: Path) -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    msg = _FakeMessage(31)
    await handlers["edit"](_video_update(msg), _make_context(["trim", "0", "5"]))

    payload = q.calls[0][2]
    digest = payload["input_sha256"]
    assert isinstance(digest, str)
    assert len(digest) == 64 and digest == digest.lower()
    assert digest == _digest_of(payload), "the digest must describe the actual staged file"


@pytest.mark.asyncio
async def test_the_input_file_is_content_addressed_not_fixed_name(tmp_path: Path) -> None:
    """A predictable ``input.mp4`` is exactly what a redelivery used to clobber."""
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    await handlers["edit"](_video_update(_FakeMessage(32)), _make_context(["trim", "0", "5"]))

    payload = q.calls[0][2]
    staged_name = Path(str(payload["input_path"])).name
    assert staged_name.startswith("input-")
    assert payload["input_sha256"] in staged_name, (
        "the immutable input's name must contain its content digest"
    )


@pytest.mark.asyncio
async def test_a_different_digest_changes_the_payload_fingerprint(tmp_path: Path) -> None:
    """The digest must be *inside* the canonical fingerprint the queue compares.

    This is the mechanical reason the fix works: ``request_identity()`` hashes the
    whole canonical payload, so two deliveries that differ only by media content
    are two different requests and the creative conflict guard fires.
    """
    from nexus_ai_agent.jobs.creative_passport import request_identity

    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    await handlers["edit"](_video_update(_FakeMessage(33)), _make_context(["trim", "0", "5"]))
    payload = q.calls[0][2]

    same = request_identity("creative_render", "k", dict(payload))
    assert (
        same.request_fingerprint
        == request_identity("creative_render", "k", dict(payload)).request_fingerprint
    )

    tampered = dict(payload)
    tampered["input_sha256"] = "b" * 64
    assert (
        request_identity("creative_render", "k", tampered).request_fingerprint
        != same.request_fingerprint
    ), "changing only the media digest MUST change the request fingerprint"


@pytest.mark.asyncio
async def test_media_less_payload_has_no_digest(tmp_path: Path) -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    await handlers["grade"](_update_for_message(34), _make_context(["otio"]))

    payload = q.calls[0][2]
    assert payload["input_path"] is None
    assert payload["input_sha256"] is None


@pytest.mark.asyncio
async def test_identical_redelivery_reuses_the_same_input_file(tmp_path: Path) -> None:
    q = _FakeQueue()
    handlers = build_creative_handlers(q)  # type: ignore[arg-type]
    context = _make_context(["trim", "0", "5"])
    for _ in range(2):
        await handlers["edit"](_video_update(_FakeMessage(35)), context)

    first, second = q.calls[0][2], q.calls[1][2]
    assert first["input_path"] == second["input_path"]
    assert first["input_sha256"] == second["input_sha256"]
    assert Path(str(first["input_path"])).exists()


def _update_for_message(message_id: int) -> Any:
    message = _FakeMessage(message_id)
    message.reply_to_message = None
    return _make_update(message)


# -- private directory creation ---------------------------------------------


def test_private_directory_creates_a_missing_root(tmp_path: Path) -> None:
    """The trusted root is created on demand; a missing root is not a refusal.

    ``get_settings()`` is ``lru_cache``d and creates ``creative_temp_dir`` only once
    per process, so after a temp reaper or an emptied volume nothing else would ever
    re-create it. The pre-change surface relied on ``parents=True`` for exactly this.
    """
    root = tmp_path / "creative"
    assert not root.exists()
    workspace = _ensure_private_directory(root / "creative_abc", root, "workspace")
    assert root.is_dir(), "the trusted root must be created on demand"
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700


def test_private_directory_still_refuses_a_symlinked_root(tmp_path: Path) -> None:
    """Creating a missing root must not become a way to accept a redirect."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    root = tmp_path / "creative"
    root.symlink_to(real, target_is_directory=True)
    with pytest.raises(_StagingError):
        _ensure_private_directory(root / "creative_abc", root, "workspace")
    assert list(real.iterdir()) == [], "nothing may be written through the symlink"


def test_trusted_root_refuses_the_configured_node_but_allows_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    """The trust boundary is the configured node, and it must not creep upwards.

    Two failure modes are tested together because they pull in opposite directions:

    * the *attack* — ``creative_temp_dir`` itself is a symlink, which is the only
      thing an operator can be talked into configuring. It must be refused, and it
      must be refused *before* ``resolve()`` can canonicalize the evidence away.
    * the *regression* — the configured node sits under a symlinked ancestor. That
      is ordinary deployment plumbing (macOS ``/tmp`` -> ``/private/tmp``, container
      volume mounts). Refusing it would break real installs while buying no safety:
      whoever can swap an ancestor of the trusted root owns the host outright.
    """
    real = tmp_path / "real"
    (real / "creative").mkdir(parents=True)

    # Attack: the configured node is a link to somewhere unrelated.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    evil = tmp_path / "evil"
    evil.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(_StagingError):
        _trusted_root(evil)

    # Regression: only an ancestor is a link; the node itself is a real directory.
    ancestor = tmp_path / "link" / "creative"
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    assert _trusted_root(ancestor) == (real / "creative").resolve()

    # Same, but the node does not exist yet — creation is the caller's job.
    assert _trusted_root(tmp_path / "link" / "not-yet") == (real / "not-yet").resolve()

    # A plain file where a directory is required is a refusal, not a crash later.
    a_file = tmp_path / "afile"
    a_file.write_text("x")
    with pytest.raises(_StagingError):
        _trusted_root(a_file)
