"""The canonical creative chain, end to end — Final Acceptance (directive §13).

Nothing is faked except the two things that must never run in a test suite (a
network Telegram call and Cloudflare R2): a **real** ``InProcessJobQueue``, the
**real** handler map from ``worker.default_job_handlers()``, the **real** lane
executor and a **real** FFmpeg encode.

Proven here:

    /edit  → queued → creative_render row → worker finds the handler
           → Agent B's executor (``render_lane``) runs
           → artifact verified (sha256 recomputed from the delivered bytes)
           → COMPLETED → localized Telegram result

and the failure half of the same path:

    same path → runtime failure → durable FAILED → safe localized message
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue, JobCompletion
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.app import _build_job_completion_notifier
from nexus_ai_agent.bot.creative_surface import build_creative_handlers
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin
from nexus_ai_agent.i18n import I18n
from nexus_ai_agent.worker import default_job_handlers


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:  # pragma: no cover - environment dependent
        pytest.skip("no FFmpeg binary available")


def _make_clip(path: Path, duration_s: float, binary: str) -> Path:
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=30:duration={duration_s}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return path


class _FakeBot:
    """Records what the notifier would have sent."""

    def __init__(self, token: str, sent: list[dict[str, Any]]) -> None:
        self.token = token
        self._sent = sent

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self._sent.append({"kind": "message", "chat_id": chat_id, "text": text})

    async def send_document(self, *, chat_id: int, document: Any, caption: str) -> None:
        # Hash the bytes *at delivery time*: the workspace is cleaned right
        # after, so this is the only moment the delivered artifact exists.
        data = Path(str(document)).read_bytes()
        self._sent.append(
            {
                "kind": "document",
                "chat_id": chat_id,
                "caption": caption,
                "path": str(document),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )


@pytest.fixture()
def telegram_stub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Keep the real ``telegram`` package; replace only the two send symbols."""
    import telegram

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(telegram, "Bot", lambda token: _FakeBot(token, sent))
    monkeypatch.setattr(telegram, "InputFile", lambda path: path)
    return sent


@pytest.fixture()
def creative_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    monkeypatch.setenv("NEXUS_FFMPEG_BIN", _ffmpeg())
    settings_module.get_settings.cache_clear()
    yield root
    settings_module.get_settings.cache_clear()


# -- surface harness ---------------------------------------------------------


class _FakeMessage:
    def __init__(self, *, file_id: str = "fid123", duration: float = 4.0) -> None:
        self.message_id = 555
        self.replies: list[str] = []
        self.reply_to_message = types.SimpleNamespace(
            video=types.SimpleNamespace(file_id=file_id, duration=duration, file_name="clip.mp4")
        )

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def _update(message: Any, *, user_id: int = 7, chat_id: int = 70, language: str = "en") -> Any:
    return types.SimpleNamespace(
        message=message,
        edited_message=None,
        effective_user=types.SimpleNamespace(id=user_id, language_code=language),
        effective_chat=types.SimpleNamespace(id=chat_id),
    )


def _context(args: list[str]) -> Any:
    return types.SimpleNamespace(args=args, bot=None)


def _stage_from(source: Path):
    async def _stage(bot: Any, file_id: str, destination: Path) -> None:
        Path(destination).write_bytes(source.read_bytes())

    return _stage


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> None:
    async def _poll() -> None:
        while await queue.get_status(job_id) not in (JobStatus.COMPLETED, JobStatus.FAILED):
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _job_id_from_reply(reply: str) -> str:
    return reply.rsplit(" ", 1)[-1]


# ---------------------------------------------------------------------------
# success half
# ---------------------------------------------------------------------------


async def test_edit_reaches_a_verified_artifact_and_the_users_chat(
    creative_root: Path,
    tmp_path: Path,
    telegram_stub: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_clip(tmp_path / "clip.mp4", 4.0, _ffmpeg())

    # Prove the canonical executor is the one that runs.
    import nexus_ai_agent.creative.rendering as rendering_pkg

    real_render = rendering_pkg.render_lane
    calls: list[dict[str, Any]] = []

    def _spy(lane: Any, output_path: Path, **kwargs: Any) -> Any:
        calls.append({"lane": lane, "output_path": Path(output_path)})
        return real_render(lane, output_path, **kwargs)

    monkeypatch.setattr(rendering_pkg, "render_lane", _spy)

    completions: list[JobCompletion] = []
    notifier = _build_job_completion_notifier("test-token")

    async def _hook(completion: JobCompletion) -> None:
        completions.append(completion)
        await notifier(completion)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=_hook)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    handlers = build_creative_handlers(queue, stage_media=_stage_from(source))  # type: ignore[arg-type]
    message = _FakeMessage()
    await handlers["edit"](_update(message), _context(["trim", "0", "2"]))

    assert message.replies, "the surface must answer immediately"
    job_id = _job_id_from_reply(message.replies[-1])
    assert I18n().t("creative.queued", lang="en", command="edit", operation="trim", job_id=job_id)
    assert message.replies[-1].startswith("⏳")

    await _drain(queue, job_id)

    # 1. the durable row is COMPLETED and carries a verified artifact
    assert await queue.get_status(job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    assert result["verified"] is True
    artifact = Path(str(result["output_path"]))
    assert artifact.name == "master.mp4"
    assert artifact.is_relative_to(creative_root)

    # 2. the executor that produced it is Agent B's canonical lane
    assert calls, "render_lane must be the executor (no second renderer)"
    assert calls[0]["output_path"] == artifact
    assert [op.op for op in calls[0]["lane"].ops] == ["trim"]

    # 3. the user received the master, and the *delivered* bytes hash to
    #    exactly the digest the queue recorded after verification
    delivery = [entry for entry in telegram_stub if entry["kind"] == "document"]
    assert len(delivery) == 1
    digest = str(result["output_sha256"]).removeprefix("sha256:")
    assert delivery[0]["sha256"] == digest
    assert delivery[0]["bytes"] > 10_000, "a real encode, not a placeholder"
    assert delivery[0]["caption"] == I18n().t(
        "creative.completed",
        lang="en",
        command="edit",
        operation="trim",
        job_id=job_id,
        sha256=digest[:12],
    )

    # 5. the job workspace was cleaned after delivery
    assert list(creative_root.glob("creative_*")) == []
    assert len(completions) == 1


async def test_exposure_grade_reaches_a_verified_artifact(
    creative_root: Path, tmp_path: Path, telegram_stub: list[dict[str, Any]]
) -> None:
    source = _make_clip(tmp_path / "clip.mp4", 3.0, _ffmpeg())
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    handlers = build_creative_handlers(queue, stage_media=_stage_from(source))  # type: ignore[arg-type]

    message = _FakeMessage(duration=3.0)
    await handlers["grade"](_update(message), _context(["exposure", "1.0"]))
    job_id = _job_id_from_reply(message.replies[-1])
    await _drain(queue, job_id)

    assert await queue.get_status(job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None
    assert result["canonical_operation"] == "color.adjust_exposure"
    assert result["verified"] is True


# ---------------------------------------------------------------------------
# failure half
# ---------------------------------------------------------------------------


async def test_runtime_failure_is_durable_failed_with_a_localized_message(
    creative_root: Path, tmp_path: Path, telegram_stub: list[dict[str, Any]]
) -> None:
    bad_source = tmp_path / "not-a-video.mp4"
    bad_source.write_bytes(b"\x00\x01 not a video at all" * 100)

    notifier = _build_job_completion_notifier("test-token")

    async def _hook(completion: JobCompletion) -> None:
        await notifier(completion)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=_hook)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    handlers = build_creative_handlers(queue, stage_media=_stage_from(bad_source))  # type: ignore[arg-type]
    message = _FakeMessage(duration=3.0)
    await handlers["edit"](_update(message, language="fa"), _context(["trim", "0", "1"]))

    job_id = _job_id_from_reply(message.replies[-1])
    await _drain(queue, job_id)

    # 1. durable FAILED — the queue never swallows the failure
    assert await queue.get_status(job_id) is JobStatus.FAILED
    # 2. the user gets a localized, path-free message; no raw key leaks
    assert len(telegram_stub) == 1
    text = telegram_stub[0]["text"]
    assert text == I18n().t("creative.failed.media_unusable", lang="fa", job_id=job_id)
    assert "creative." not in text
    assert str(tmp_path) not in text
    # 3. the workspace is not left behind
    assert list(creative_root.glob("creative_*")) == []


async def test_a_redelivered_update_renders_once(
    creative_root: Path, tmp_path: Path, telegram_stub: list[dict[str, Any]]
) -> None:
    source = _make_clip(tmp_path / "clip.mp4", 3.0, _ffmpeg())
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    stages: list[Path] = []

    def _counting_stage(source_path: Path):
        async def _stage(bot: Any, file_id: str, destination: Path) -> None:
            stages.append(Path(destination))
            Path(destination).write_bytes(source_path.read_bytes())

        return _stage

    handlers = build_creative_handlers(  # type: ignore[arg-type]
        queue, stage_media=_counting_stage(source)
    )
    message = _FakeMessage(duration=3.0)
    await handlers["edit"](_update(message), _context(["reverse"]))
    first_job = _job_id_from_reply(message.replies[-1])

    client = _FakeMessage(duration=3.0)
    await handlers["edit"](_update(client), _context(["reverse"]))
    second_job = _job_id_from_reply(client.replies[-1])

    assert first_job == second_job, "the same Telegram message must map to one job"
    await _drain(queue, first_job)

    rows = [row for row in Path(tmp_path / "jobs.sqlite3").parent.glob("jobs.sqlite3")]
    assert rows, "the durable queue file must exist"
    import sqlite3

    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM nexus_job_queue WHERE job_type = 'creative_render'"
        ).fetchone()[0]
    assert count == 1


async def test_unavailable_operation_never_creates_a_job(
    creative_root: Path, tmp_path: Path, telegram_stub: list[dict[str, Any]]
) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    handlers = build_creative_handlers(queue)  # type: ignore[arg-type]

    message = _FakeMessage(duration=3.0)
    await handlers["caption"](_update(message, language="fa"), _context(["burnin"]))

    import sqlite3

    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM nexus_job_queue").fetchone()[0]
    assert count == 0
    assert "creative." not in message.replies[-1]
    assert message.replies[-1] == I18n().t(
        "creative.not_available", lang="fa", command="caption", operation="burnin"
    )
