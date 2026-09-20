"""Wave 2.5 D4 extension: the slideshow completion notifier, with ``telegram`` stubbed.

The hook must deliver the master on success, a short mapped message on failure,
never a path or a stack trace, stay silent (but still clean up) without an
origin chat, and only ever delete inside the configured temp directory.
The stub technique mirrors ``tests/integration/test_in_process_job_queue.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import JobCompletion
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot import slideshow_notify
from nexus_ai_agent.bot.slideshow import friendly_render_error


class _RecordingInputFile:
    """``telegram.InputFile`` stand-in: records that the file was opened."""

    instances: list[str] = []

    def __init__(self, path: str) -> None:
        _RecordingInputFile.instances.append(path)

    def __enter__(self) -> _RecordingInputFile:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _RecordingBot:
    """``telegram.Bot`` stand-in: captures every outbound message/document."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, token: str) -> None:
        assert token, "the notifier must forward a real token"

    async def send_message(self, **kwargs: Any) -> None:
        _RecordingBot.calls.append(("message", kwargs))

    async def send_document(self, **kwargs: Any) -> None:
        _RecordingBot.calls.append(("document", kwargs))


@pytest.fixture()
def telegram_stub(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    stub = ModuleType("telegram")
    stub.Bot = _RecordingBot  # type: ignore[attr-defined]
    stub.InputFile = _RecordingInputFile  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", stub)
    _RecordingBot.calls = []
    _RecordingInputFile.instances = []
    return stub


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "nexus_creative"
    base.mkdir()
    monkeypatch.setattr(
        slideshow_notify, "get_settings", lambda: SimpleNamespace(creative_temp_dir=str(base))
    )
    return base


def _completion(
    *,
    status: JobStatus = JobStatus.COMPLETED,
    result: dict[str, Any] | None,
    payload: dict[str, Any],
) -> JobCompletion:
    return JobCompletion(
        job_id="job-1",
        job_type="slideshow_render",
        status=status,
        result=result,
        error=None,
        payload=payload,
    )


def _workspace(temp_root: Path, *, with_master: bool = True) -> Path:
    workspace = temp_root / "slideshow_42_run"
    workspace.mkdir(parents=True)
    if with_master:
        (workspace / "master.mp4").write_bytes(b"fake-master")
    return workspace


async def test_success_sends_document_then_cleans_the_workspace(
    telegram_stub: ModuleType, temp_root: Path
) -> None:
    workspace = _workspace(temp_root)
    completion = _completion(
        result={
            "success": True,
            "output_path": str(workspace / "master.mp4"),
            "duration_us": 30_000_000,
            "shot_count": 3,
            "size_bytes": 1048576,
            "width": 1280,
            "height": 720,
        },
        payload={"chat_id": 7, "workspace_dir": str(workspace)},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    assert [name for name, _ in _RecordingBot.calls] == ["document"]
    _kind, kwargs = _RecordingBot.calls[0]
    assert kwargs["chat_id"] == 7
    assert "✅" in kwargs["caption"]
    assert "30 ثانیه" in kwargs["caption"]
    assert _RecordingInputFile.instances == [str(workspace / "master.mp4")]
    assert not workspace.exists()  # notifier owns post-delivery cleanup (r7 item 4)


async def test_typed_failure_sends_the_mapped_message(
    telegram_stub: ModuleType, temp_root: Path
) -> None:
    workspace = _workspace(temp_root, with_master=False)
    completion = _completion(
        result={"success": False, "error_code": "unusable_image"},
        payload={"chat_id": 7, "workspace_dir": str(workspace)},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    assert [name for name, _ in _RecordingBot.calls] == ["message"]
    _kind, kwargs = _RecordingBot.calls[0]
    assert kwargs["text"] == friendly_render_error("unusable_image")
    assert "❌" in kwargs["text"]
    assert "/" not in kwargs["text"]
    assert not workspace.exists()


async def test_queue_level_failure_without_a_result_is_still_human_readable(
    telegram_stub: ModuleType, temp_root: Path
) -> None:
    completion = _completion(
        status=JobStatus.FAILED,
        result=None,
        payload={"chat_id": 7, "workspace_dir": str(_workspace(temp_root, with_master=False))},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    _kind, kwargs = _RecordingBot.calls[0]
    assert kwargs["text"] == friendly_render_error(None)
    assert "Traceback" not in kwargs["text"]


async def test_missing_artifact_falls_back_to_internal_message(
    telegram_stub: ModuleType, temp_root: Path
) -> None:
    workspace = _workspace(temp_root, with_master=False)
    completion = _completion(
        result={
            "success": True,
            "output_path": str(workspace / "master.mp4"),  # vanished on disk
        },
        payload={"chat_id": 7, "workspace_dir": str(workspace)},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    assert [name for name, _ in _RecordingBot.calls] == ["message"]
    assert _RecordingBot.calls[0][1]["text"] == friendly_render_error("internal")


async def test_no_origin_chat_means_silent_but_still_cleaned(
    telegram_stub: ModuleType, temp_root: Path
) -> None:
    workspace = _workspace(temp_root)
    completion = _completion(
        result={"success": True, "output_path": str(workspace / "master.mp4")},
        payload={"workspace_dir": str(workspace)},  # CLI-drained job
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    assert _RecordingBot.calls == []
    assert not workspace.exists()


async def test_workspace_outside_the_temp_root_is_never_deleted(
    telegram_stub: ModuleType, temp_root: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "not_a_workspace"
    foreign.mkdir()
    survivor = foreign / "keep.me"
    survivor.write_text("precious")
    completion = _completion(
        result={"success": False, "error_code": "internal"},
        payload={"chat_id": 7, "workspace_dir": str(foreign)},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")

    assert survivor.is_file()  # containment rule protects arbitrary paths


async def test_send_failure_never_raises_to_the_queue(
    telegram_stub: ModuleType, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(_RecordingBot, "send_document", _boom)
    workspace = _workspace(temp_root)
    completion = _completion(
        result={"success": True, "output_path": str(workspace / "master.mp4")},
        payload={"chat_id": 7, "workspace_dir": str(workspace)},
    )
    await slideshow_notify.notify_slideshow_completion(completion, "token")  # no raise
    assert not workspace.exists()  # cleanup survived the failed send
