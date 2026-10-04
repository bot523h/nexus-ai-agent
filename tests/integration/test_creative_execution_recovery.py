"""Process-boundary recovery for creative queue identity and evidence checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import CREATIVE_RENDER_JOB_TYPE
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.worker import default_job_handlers


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def _clip(path: Path, seconds: float = 2.0) -> None:
    subprocess.run(
        [
            resolve_ffmpeg_bin(),
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


def _trim_payload(workspace: Path, key: str) -> dict[str, Any]:
    return {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 7,
        "chat_id": 8,
        "lang": "en",
        "idempotency_key": key,
    }


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
        }:
            return status
        await asyncio.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


def _child_env(tmp_path: Path, db: Path, payload: dict[str, Any]) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(repo_root / "src")
    if existing_pythonpath:
        pythonpath += os.pathsep + existing_pythonpath
    return {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "CREATIVE_TEMP_DIR": str(tmp_path / "creative_tmp"),
        "QUEUE_DB_PATH": str(db),
        "JOB_PAYLOAD_JSON": json.dumps(payload),
        "JOB_TYPE": CREATIVE_RENDER_JOB_TYPE,
        "JOB_KEY": str(payload["idempotency_key"]),
        "JOB_ID_FILE": str(tmp_path / "job-id.txt"),
    }


def _child_run(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.asyncio
async def test_duplicate_delivery_across_queue_instances_executes_one_handler(
    tmp_path: Path,
) -> None:
    db = tmp_path / "duplicate.sqlite3"
    first_queue = InProcessJobQueue(db, artifact_verifiers={})
    second_queue = InProcessJobQueue(db, artifact_verifiers={})
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"executions": calls}

    first_queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    second_queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    payload: dict[str, object] = {"operation": "legacy-test-payload"}
    first_id = await first_queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="duplicate-delivery", payload=payload
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    second_id = await second_queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="duplicate-delivery", payload=payload
    )
    assert second_id == first_id
    await asyncio.sleep(0.05)
    release.set()

    assert await _drain(first_queue, first_id) is JobStatus.COMPLETED
    await asyncio.sleep(0.05)
    assert calls == 1
    history = await first_queue.get_attempt_history(first_id)
    assert len(history) == 1 and history[0]["status"] == "completed"
    first_identity = await first_queue.get_request_identity(first_id)
    second_identity = await second_queue.get_request_identity(second_id)
    assert first_identity == second_identity


@pytest.mark.asyncio
async def test_process_crash_before_render_recovery_runs_real_trim_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creative_root = tmp_path / "creative_tmp"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()
    workspace = creative_root / "creative_before_render_crash"
    workspace.mkdir()
    input_path = workspace / "input.mp4"
    _clip(input_path)
    key = "creative:crash-before-render"
    payload = _trim_payload(workspace, key)
    db = tmp_path / "before-render.sqlite3"
    marker = tmp_path / "entered-handler.txt"
    env = _child_env(tmp_path, db, payload)
    env["HANDLER_MARKER"] = str(marker)

    child = _child_run(
        """
import asyncio, json, os
from pathlib import Path
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

async def crash_before_effect(payload):
    Path(os.environ['HANDLER_MARKER']).write_text('entered', encoding='utf-8')
    os._exit(71)

async def main():
    queue = InProcessJobQueue(os.environ['QUEUE_DB_PATH'])
    queue.register_handler(os.environ['JOB_TYPE'], crash_before_effect)
    job_id = await queue.enqueue(
        job_type=os.environ['JOB_TYPE'],
        idempotency_key=os.environ['JOB_KEY'],
        payload=json.loads(os.environ['JOB_PAYLOAD_JSON']),
    )
    Path(os.environ['JOB_ID_FILE']).write_text(job_id, encoding='utf-8')
    await asyncio.sleep(180)

asyncio.run(main())
""",
        env,
    )
    assert child.returncode == 71, child.stderr
    assert marker.read_text(encoding="utf-8") == "entered"
    job_id = (tmp_path / "job-id.txt").read_text(encoding="utf-8")

    queue = InProcessJobQueue(db)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    resumed = await queue.resume_pending()
    assert resumed == [job_id]
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    history = await queue.get_attempt_history(job_id)
    assert [item["status"] for item in history] == ["interrupted", "completed"]
    passport = await queue.get_artifact_passport(job_id)
    assert passport["project_id"] == f"shot-{key}"
    assert (
        passport["input_asset"]["sha256"]
        == "sha256:" + hashlib.sha256(input_path.read_bytes()).hexdigest()
    )
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_process_crash_after_verified_checkpoint_reconciles_without_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creative_root = tmp_path / "creative_tmp"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()
    workspace = creative_root / "creative_after_verified_crash"
    workspace.mkdir()
    input_path = workspace / "input.mp4"
    _clip(input_path)
    key = "creative:crash-after-verified"
    payload = _trim_payload(workspace, key)
    db = tmp_path / "after-verified.sqlite3"
    env = _child_env(tmp_path, db, payload)

    child = _child_run(
        """
import asyncio, os
from pathlib import Path
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.worker import default_job_handlers

class CrashAfterCheckpointQueue(InProcessJobQueue):
    def _mark_completed(
        self, claim, result, stored_passport=None,
        attempt_status='completed', reconciled_from_attempt_id=None
    ):
        os._exit(72)

async def main():
    queue = CrashAfterCheckpointQueue(os.environ['QUEUE_DB_PATH'])
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    job_id = await queue.enqueue(
        job_type=os.environ['JOB_TYPE'],
        idempotency_key=os.environ['JOB_KEY'],
        payload=__import__('json').loads(os.environ['JOB_PAYLOAD_JSON']),
    )
    Path(os.environ['JOB_ID_FILE']).write_text(job_id, encoding='utf-8')
    await asyncio.sleep(180)

asyncio.run(main())
""",
        env,
    )
    assert child.returncode == 72, child.stderr
    job_id = (tmp_path / "job-id.txt").read_text(encoding="utf-8")
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status, attempt, artifact_passport_json, attempt_history_json "
            "FROM nexus_job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == JobStatus.VERIFYING.value
        assert row[1] == 1
        assert row[2] is not None
        attempt_history = json.loads(str(row[3]))
        assert len(attempt_history) == 1
        assert attempt_history[0]["status"] == "verified"
        assert attempt_history[0]["passport_json"] is not None

    queue = InProcessJobQueue(db)
    handlers = default_job_handlers()
    render_calls = 0
    original_render = handlers[CREATIVE_RENDER_JOB_TYPE]

    async def counted_render(payload: dict[str, object]) -> dict[str, object]:
        nonlocal render_calls
        render_calls += 1
        return await original_render(payload)

    for job_type, handler in handlers.items():
        queue.register_handler(job_type, handler)
    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, counted_render)
    checkpoint_passport = await queue.get_artifact_passport(job_id)
    assert checkpoint_passport["job"]["idempotency_key"] == key
    resumed = await queue.resume_pending()
    assert resumed == [job_id]
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    assert render_calls == 0, "a valid verified checkpoint must be reconciled, not rendered again"

    history = await queue.get_attempt_history(job_id)
    assert [item["status"] for item in history] == ["verified", "reconciled"]
    assert history[1]["reconciled_from_attempt_id"] == history[0]["attempt_id"]
    passport = await queue.get_artifact_passport(job_id)
    assert passport["job"]["idempotency_key"] == key
    assert passport["artifact"]["sha256"].startswith("sha256:")
    settings_module.get_settings.cache_clear()
