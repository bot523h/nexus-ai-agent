"""Completion delivery for ``creative_render`` (owner directive §6/§8).

Pinned here: localized copy in both terminal states, a closed failure
vocabulary (no raw key, no path, no exception text), workspace cleanup that
happens only for a contained workspace, and — with the real queue — the
guarantee that a broken notifier can never rewrite a durable job status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue, JobCompletion
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot import creative_notify
from nexus_ai_agent.bot.creative_notify import failure_code, notify_creative_completion
from nexus_ai_agent.i18n import I18n


class _Recorder:
    """Records what the notifier asked the transport to send.

    The notifier is framework-free by design (the Telegram adapter lives in
    ``bot/app.py``), so the test injects this recorder instead of PTB.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append({"kind": "message", "chat_id": chat_id, "text": text})

    async def send_document(self, chat_id: int, document: Any, caption: str) -> None:
        self.sent.append(
            {"kind": "document", "chat_id": chat_id, "document": document, "caption": caption}
        )


@pytest.fixture()
def telegram_stub() -> _Recorder:
    return _Recorder()


@pytest.fixture()
def creative_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    yield root
    settings_module.get_settings.cache_clear()


def _completion(workspace: Path, *, status: JobStatus, result: dict[str, Any] | None, error=None):
    return JobCompletion(
        job_id="job-7",
        job_type="creative_render",
        status=status,
        result=result,
        error=error,
        payload={
            "chat_id": 4242,
            "lang": "fa",
            "workspace_dir": str(workspace),
            "command": "edit",
            "operation": "trim",
        },
    )


def test_failure_code_reads_the_typed_prefix() -> None:
    completion = _completion(Path("/tmp"), status=JobStatus.FAILED, result=None)
    completion = JobCompletion(
        **{**completion.__dict__, "error": "[artifact_verification_failed] digest mismatch"}
    )
    assert failure_code(completion, {}) == "artifact_verification_failed"


def test_unknown_failure_code_degrades_to_internal() -> None:
    completion = _completion(Path("/tmp"), status=JobStatus.FAILED, result=None, error="boom")
    assert failure_code(completion, {}) == "internal"


async def test_completed_job_delivers_the_document_in_the_user_language(
    telegram_stub: _Recorder, creative_root: Path
) -> None:
    workspace = creative_root / "creative_abc"
    workspace.mkdir()
    master = workspace / "master.mp4"
    master.write_bytes(b"master-bytes")

    await notify_creative_completion(
        _completion(
            workspace,
            status=JobStatus.COMPLETED,
            result={
                "success": True,
                "output_path": str(master),
                "output_sha256": "sha256:" + "cd" * 32,
                "command": "edit",
                "operation": "trim",
            },
        ),
        telegram_stub,
    )

    assert len(telegram_stub.sent) == 1
    assert telegram_stub.sent[0]["kind"] == "document"
    caption = telegram_stub.sent[0]["caption"]
    assert caption == I18n().t(
        "creative.completed",
        lang="fa",
        command="edit",
        operation="trim",
        job_id="job-7",
        sha256="cd" * 6,
    )
    assert "creative." not in caption
    # delivery owns the file: the workspace is gone afterwards
    assert not workspace.exists()


async def test_failed_job_reports_a_localized_failure_and_cleans_up(
    telegram_stub: _Recorder, creative_root: Path
) -> None:
    workspace = creative_root / "creative_def"
    workspace.mkdir()
    completion = _completion(workspace, status=JobStatus.FAILED, result=None)
    completion = JobCompletion(
        **{**completion.__dict__, "error": "[render_failed] /home/secret/path.mp4 blew up"}
    )

    await notify_creative_completion(completion, telegram_stub)

    assert len(telegram_stub.sent) == 1
    text = telegram_stub.sent[0]["text"]
    assert text == I18n().t("creative.failed.render_failed", lang="fa", job_id="job-7")
    assert "creative." not in text
    assert "/home/secret" not in text
    assert not workspace.exists()


async def test_completed_row_without_a_deliverable_is_reported_as_failure(
    telegram_stub: _Recorder, creative_root: Path
) -> None:
    """No fake success at the delivery boundary either."""
    workspace = creative_root / "creative_ghi"
    workspace.mkdir()
    await notify_creative_completion(
        _completion(
            workspace,
            status=JobStatus.COMPLETED,
            result={"success": True, "output_path": str(workspace / "missing.mp4")},
        ),
        telegram_stub,
    )
    assert telegram_stub.sent[0]["kind"] == "message"
    expected = I18n().t("creative.failed.internal", lang="fa", job_id="job-7")
    assert telegram_stub.sent[0]["text"] == expected


async def test_workspace_outside_the_creative_root_is_never_deleted(
    telegram_stub: _Recorder, creative_root: Path, tmp_path: Path
) -> None:
    outsider = tmp_path / "precious"
    outsider.mkdir()
    (outsider / "keep.txt").write_text("keep me", encoding="utf-8")

    await notify_creative_completion(
        _completion(
            outsider,
            status=JobStatus.COMPLETED,
            result={"success": True, "output_path": str(outsider / "keep.txt")},
        ),
        telegram_stub,
    )
    assert (outsider / "keep.txt").read_text(encoding="utf-8") == "keep me"


async def test_broken_notifier_cannot_flip_a_durable_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The queue owns the status; the hook is fail-safe (directive §8)."""
    monkeypatch.setattr(creative_notify, "notify_creative_completion", _explode, raising=True)
    completions: list[JobCompletion] = []

    async def _hook(completion: JobCompletion) -> None:
        completions.append(completion)
        raise RuntimeError("notifier exploded")

    async def _ok(payload: dict[str, object]) -> dict[str, object]:
        return {"success": True}

    async def _bad(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("render died")

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=_hook)
    queue.register_handler("creative_render", _ok)
    ok_id = await queue.enqueue(
        job_type="creative_render", idempotency_key="ok", payload={"chat_id": 1}
    )
    await _wait(queue, ok_id)
    assert await queue.get_status(ok_id) is JobStatus.COMPLETED

    queue.register_handler("creative_render", _bad)
    bad_id = await queue.enqueue(
        job_type="creative_render", idempotency_key="bad", payload={"chat_id": 1}
    )
    await _wait(queue, bad_id)
    assert await queue.get_status(bad_id) is JobStatus.FAILED
    assert len(completions) == 2


async def _explode(completion: Any, token: str) -> None:  # pragma: no cover - must not run
    raise AssertionError("the patched notifier must never be reached")


async def _wait(queue: InProcessJobQueue, job_id: str) -> None:
    import asyncio

    async def _poll() -> None:
        while await queue.get_status(job_id) not in (JobStatus.COMPLETED, JobStatus.FAILED):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=5.0)
