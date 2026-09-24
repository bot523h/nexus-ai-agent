"""§3 integration harness — the creative chain, full depth, no mock at the seam.

Reproduces the chain the owner directive demands, end-to-end through the
REAL queue and REAL render binary (imageio-ffmpeg wheel):

    enqueue creative_render
      → InProcessJobQueue persists + schedules
      → default_job_handlers routes to creative/render_jobs.py
      → packs registry → CommandBus → lane → FFmpeg → measured artifact
      → durable JobStatus.COMPLETED / FAILED
      → JobCompletion notifier (translated, artifact attached, cleanup)
      → Telegram (PTB faked only at the telegram.Bot boundary)

Failure sides proven too: typed failure (unsupported op → COMPLETED with
translated typed failure), durable FAILED (unexpected exception →
creative.failed.internal, never raw traceback/path), idempotent re-enqueue
(same key = same job, no double effect).
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.app import _notify_creative_completion
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.i18n import i18n
from nexus_ai_agent.worker import default_job_handlers


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.videos: list[tuple[int, bytes, str]] = []
        self.documents: list[tuple[int, bytes, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append(text)

    async def send_video(self, *, chat_id: int, video: Any, caption: str, **kw: Any) -> None:
        self.videos.append((chat_id, video.read(), caption))

    async def send_document(self, *, chat_id: int, document: Any, caption: str) -> None:
        self.documents.append((chat_id, document.read(), caption))


def _clip(path: Path, seconds: int = 2) -> None:
    binary = resolve_ffmpeg_bin()
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _payload(workspace: Path, key: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "fa",
        "idempotency_key": key,
    }
    base.update(over)
    return base


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir(parents=True)
    settings_module.get_settings.cache_clear()
    bot = _FakeBot()
    monkeypatch.setattr("telegram.Bot", lambda token: bot)
    completions: list[Any] = []

    async def hook(completion: Any) -> None:
        # capture the completion + pre-cleanup artifact facts, then deliver
        result = completion.result or {}
        artifact = result.get("artifact_path")
        artifact_exists = Path(str(artifact)).is_file() if artifact else False
        completions.append((completion, artifact_exists))
        await _notify_creative_completion(completion, token="dummy")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=hook)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, bot, completions  # type: ignore[misc]
    settings_module.get_settings.cache_clear()


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            return status
        await asyncio.sleep(0.1)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


@pytest.mark.asyncio
async def test_enqueue_to_artifact_to_translated_delivery(harness) -> None:  # noqa: ANN001
    queue, bot, completions = harness
    key = "creative:42:4242:777"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e777"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # ── enqueue: same key twice → same job id, exactly one effect (§6) ──
    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    job_id_again = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    assert job_id_again == job_id, "re-enqueue must collapse onto the durable key"

    # ── durable completion through the real handler ──
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    assert result["operation"] == "timeline.trim"
    # artifact facts are measured, not asserted by flags
    digest = result["sha256"]
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert 700_000 <= result["duration_us"] <= 1_300_000

    # ── notifier delivered the artifact (bytes == sha256 evidence) ──
    assert completions and all(existed for _, existed in completions), (
        "artifact must exist on disk at completion-hook time"
    )
    assert len(bot.videos) == 1, f"expected exactly one video delivery, got {bot.videos}"
    chat_id, video_bytes, caption = bot.videos[0]
    assert chat_id == 4242
    assert "sha256:" + hashlib.sha256(video_bytes).hexdigest() == digest
    assert caption == i18n.t("creative.completed", lang="fa", command="edit", operation="trim")
    assert "creative." not in caption

    # ── workspace cleaned by notifier; idempotent re-enqueue does not replay ──
    assert not workspace.exists()
    job_id_third = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    assert job_id_third == job_id
    await asyncio.sleep(0.3)
    assert len(bot.videos) == 1, "idempotent key must not replay a finished job"


@pytest.mark.asyncio
async def test_typed_failure_reaches_user_translated(harness) -> None:  # noqa: ANN001
    queue, bot, _ = harness
    key = "creative:42:4242:778"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e778"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key, operation="lut", command="grade"),
    )
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert result["error_code"] == "unsupported_operation"
    assert bot.messages, "typed failure must reach the user"
    text = bot.messages[-1]
    assert text == i18n.t("creative.failed.unsupported_operation", lang="fa", detail="x")
    assert "creative." not in text and "lut" not in text.replace("creative.failed", "")


@pytest.mark.asyncio
async def test_unexpected_crash_is_durable_failed_and_safe(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, bot, _ = harness
    key = "creative:42:4242:779"
    workspace = Path(str(queue.db_path)).parent / "creative_tmp" / "creative_e2e779"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # simulate a catastrophic environment failure past every typed guard
    def _boom(raw: str) -> Path:
        raise OSError("disk exploded")

    monkeypatch.setattr("nexus_ai_agent.creative.render_jobs._guarded_workspace", _boom)

    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=_payload(workspace, key)
    )
    status = await _drain(queue, job_id)
    assert status is JobStatus.FAILED, "unexpected exceptions must persist FAILED"
    row_error = await queue.get_result(job_id)
    assert row_error is None  # FAILED jobs have no result payload
    assert bot.messages, "failed job must notify the user"
    text = bot.messages[-1]
    assert text == i18n.t("creative.failed.internal", lang="fa", detail="x")
    assert "disk exploded" not in text, "internal errors must not leak to users"
    assert "creative_" not in text and "/tmp" not in text, "internal paths must not leak"
