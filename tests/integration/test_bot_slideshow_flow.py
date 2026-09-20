"""Wave 2.5 end-to-end queue round-trip for ``slideshow_render`` (mocked encoder).

A real ``InProcessJobQueue`` and the real worker adapter run together; only
``render_from_files`` is replaced (the FFmpeg lane is covered by the Wave 2c
suite) so the test verifies what Wave 2.5 actually adds: the payload envelope,
the typed result, and who cleans which file when (r7 item 4).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue, JobCompletion
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.slideshow import worker_adapter
from nexus_ai_agent.creative.slideshow.probe import ProbeError

#: The encoder is mocked here — the suite must never depend on FFmpeg.
FAKE_MASTER = b"\x00\x00\x00\x18ftypmp42fake-master"

#: The payload envelope validates suffixes only (probing lives behind the
#: mocked engine call), so placeholder bytes with image names are enough.
def make_image(path: Path, seed: int = 0) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + f"placeholder-{seed}".encode())
    return path


def _completed_outcome(output_path: Path, *, shots: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        artifact={
            "output_path": str(output_path),
            "output_sha256": "sha256:" + "ab" * 32,
            "duration_us": 30_000_000,
            "width": 1280,
            "height": 720,
            "has_audio": False,
            "size_bytes": len(FAKE_MASTER),
        },
        template_id="energetic_cut",
        plan={"shots": [{} for _ in range(shots)], "warnings": []},
    )


def _make_workspace(tmp_path: Path, count: int) -> tuple[Path, list[Path]]:
    workspace = tmp_path / f"slideshow_42_{uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    images = [make_image(workspace / f"img_{i:02d}.jpg", seed=i) for i in range(count)]
    return workspace, images


def _payload(workspace: Path, images: list[Path], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": 7,
        "user_id": 42,
        "image_paths": [str(path) for path in images],
        "workspace_dir": str(workspace),
        "output_path": str(workspace / "master.mp4"),
        "target_duration_us": worker_adapter.BOT_TARGET_DURATION_US,
        "resolution": worker_adapter.DEFAULT_RESOLUTION,
    }
    payload.update(overrides)
    return payload


async def _drain(queue: InProcessJobQueue, job_id: str) -> None:
    async def _poll() -> None:
        while await queue.get_status(job_id) not in (JobStatus.COMPLETED, JobStatus.FAILED):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=5.0)


def _queue(tmp_path: Path, completions: list[JobCompletion]) -> InProcessJobQueue:
    async def _hook(completion: JobCompletion) -> None:
        completions.append(completion)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", on_job_finished=_hook)
    queue.register_handler(worker_adapter.SLIDESHOW_JOB_TYPE, worker_adapter.slideshow_render_job)
    return queue


@pytest.fixture()
def _fake_render(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the engine call; record every request the adapter composed."""
    seen: list[dict[str, Any]] = []

    def _render(request: Any, *, output_path: Path, **kwargs: Any) -> SimpleNamespace:
        seen.append({"request": request, "output_path": Path(output_path), "kwargs": kwargs})
        Path(output_path).write_bytes(FAKE_MASTER)
        return _completed_outcome(Path(output_path), shots=len(request.images))

    monkeypatch.setattr(worker_adapter, "render_from_files", _render)
    return seen


async def test_success_delivers_and_leaves_the_master_to_the_notifier(
    tmp_path: Path, _fake_render: list[dict[str, Any]]
) -> None:
    completions: list[JobCompletion] = []
    queue = _queue(tmp_path, completions)
    workspace, images = _make_workspace(tmp_path, 3)
    job_id = await queue.enqueue(
        job_type=worker_adapter.SLIDESHOW_JOB_TYPE,
        idempotency_key="flow-success",
        payload=_payload(workspace, images),
    )
    await _drain(queue, job_id)

    assert _fake_render, "the worker adapter must call the pure engine entry point"
    composed = _fake_render[0]["request"]
    assert composed.target_duration_us == 30_000_000
    assert composed.provider == "local"
    assert composed.allow_image_upload is False

    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    master = workspace / "master.mp4"
    assert master.is_file() and master.read_bytes() == FAKE_MASTER
    assert result["output_path"] == str(master)
    assert result["shot_count"] == 3
    assert result["size_bytes"] == len(FAKE_MASTER)
    # r7 item 4: inputs are gone, the delivered product is not.
    assert all(not path.exists() for path in images)

    await asyncio.sleep(0.05)
    assert len(completions) == 1
    completion = completions[0]
    assert completion.status is JobStatus.COMPLETED
    assert completion.payload["chat_id"] == 7
    assert completion.result is not None and completion.result.get("success") is True


async def test_engine_failure_maps_to_a_code_and_cleans_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(request: Any, *, output_path: Path, **kwargs: Any) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        raise ProbeError("unsupported image type: '.jpg' (whatever)")

    monkeypatch.setattr(worker_adapter, "render_from_files", _fail)

    completions: list[JobCompletion] = []
    queue = _queue(tmp_path, completions)
    workspace, images = _make_workspace(tmp_path, 2)
    job_id = await queue.enqueue(
        job_type=worker_adapter.SLIDESHOW_JOB_TYPE,
        idempotency_key="flow-fail",
        payload=_payload(workspace, images),
    )
    await _drain(queue, job_id)

    result = await queue.get_result(job_id)
    assert result is not None
    assert result["success"] is False
    assert result["error_code"] == "unusable_image"
    assert "unsupported image type" not in str(result)
    assert not workspace.exists()  # failed runs are fully removed (r7 item 4)
    await asyncio.sleep(0.05)
    assert completions and completions[0].status is JobStatus.COMPLETED


async def test_payload_envelope_is_a_trust_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_render: list[dict[str, Any]]
) -> None:
    def _unused(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("render must not run for an invalid payload")

    monkeypatch.setattr(worker_adapter, "render_from_files", _unused)

    queue = _queue(tmp_path, [])
    workspace, images = _make_workspace(tmp_path, 6)
    outsider = tmp_path / "victim.jpg"
    make_image(outsider, seed=1)

    # Six images exceed the bot ceiling.
    job_id = await queue.enqueue(
        job_type=worker_adapter.SLIDESHOW_JOB_TYPE,
        idempotency_key="envelope-count",
        payload=_payload(workspace, images),
    )
    await _drain(queue, job_id)
    result = await queue.get_result(job_id)
    assert result is not None and result["error_code"] == "invalid_request"

    # A duration above the 30 s queue cap is refused even though the pack allows 60 s.
    too_long, images2 = _make_workspace(tmp_path, 1)
    job_id = await queue.enqueue(
        job_type=worker_adapter.SLIDESHOW_JOB_TYPE,
        idempotency_key="envelope-duration",
        payload=_payload(too_long, images2, target_duration_us=60_000_000),
    )
    await _drain(queue, job_id)
    result = await queue.get_result(job_id)
    assert result is not None and result["error_code"] == "invalid_request"

    # Paths outside the job workspace are refused — and the victim file stays alive.
    sneaky, images3 = _make_workspace(tmp_path, 1)
    job_id = await queue.enqueue(
        job_type=worker_adapter.SLIDESHOW_JOB_TYPE,
        idempotency_key="envelope-traversal",
        payload=_payload(sneaky, images3, image_paths=[str(outsider)]),
    )
    await _drain(queue, job_id)
    result = await queue.get_result(job_id)
    assert result is not None and result["error_code"] == "invalid_request"
    assert outsider.is_file()


async def test_stale_workspaces_are_pruned_by_the_sweep(tmp_path: Path) -> None:
    base = tmp_path / "creative"
    old = base / "slideshow_orphan"
    fresh = base / "slideshow_fresh"
    for directory in (old, fresh):
        directory.mkdir(parents=True)
    ancient = 1_000_000.0
    os.utime(old, (ancient, ancient))
    worker_adapter._prune_stale_workspaces(base)
    assert not old.exists()
    assert fresh.exists()


def test_default_job_handlers_gains_the_slideshow_type() -> None:
    from nexus_ai_agent.worker import default_job_handlers

    handlers = default_job_handlers()
    assert handlers[worker_adapter.SLIDESHOW_JOB_TYPE] is worker_adapter.slideshow_render_job
    assert {"pdf_extract", "story"} <= set(handlers)  # pre-existing jobs untouched
