"""End-to-end proof: free text -> DURABLE queue -> existing worker -> verified artifact.

This is the Gate-C step-2 vertical slice, exercised with **no mock at the
execution seam**: the real ``InProcessJobQueue``, the real ``creative_render``
worker, the real runtime registry, the real ``CommandBus`` and the real FFmpeg
lane.  The only substituted dependency is the *external model* (a scripted
producer that emits one JSON proposal), exactly as the composition root injects
a provider.

Three properties are proven here, each with a real artifact:

* **artifact-proven** — a real ``.mp4`` exists, its canonical sha256 matches the
  bytes on disk, the probe measures the requested duration, and the registered
  verifier says ``verified``;
* **durable + recovery-proven** — the job survives the death of the enqueuing
  process and is re-executed exactly once by the existing worker;
* **tamper-refusing** — mutating the artifact after the fact turns the
  independent verification red.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import build_job_bus
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    Timeline,
    new_project,
)
from nexus_ai_agent.jobs.creative_verification import creative_render_verifier
from nexus_ai_agent.nagar.cognition import build_cognition_gateway
from nexus_ai_agent.nagar.creative import run_free_text_intent_durable
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "nagar-durable-e2e"
SOURCE_DURATION_US = 2_000_000


class ScriptedProducer:
    """A declared test double for the external model seam only."""

    consults_model = True

    def __init__(self, in_us: int = 0, out_us: int = 1_000_000) -> None:
        self._in = in_us
        self._out = out_us

    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str:
        return json.dumps(
            {
                "schema_id": "nagar.gateway.proposal.v1",
                "schema_version": 1,
                "operation": "timeline.trim",
                "input": {
                    "clip_asset_id": "src",
                    "in_point_us": self._in,
                    "out_point_us": self._out,
                },
                "rationale": "scripted",
                "confidence": 1.0,
            }
        )


def _clip(path: Path, seconds: float = 2.0) -> None:
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")
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


def _project() -> tuple[ActorIdentity, Any]:
    actor = ActorIdentity(kind="user", actor_id="operator")
    project = new_project(
        PROJECT_ID, "durable", Timeline(timeline_id="tl", duration_us=SOURCE_DURATION_US)
    ).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "0" * 64,
                    duration_us=SOURCE_DURATION_US,
                )
            ]
        }
    )
    return actor, project


def _gateway(actor: ActorIdentity, project: Any, producer: Any) -> Any:
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=frozenset({"project:read", "project:write"})
    )
    bus = build_job_bus(project, authorizer=access)
    return build_cognition_gateway(
        bus=bus, actor=actor, project_id=PROJECT_ID, enabled=True, completion=producer
    )


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    while loop.time() < deadline:
        status = await queue.get_status(job_id)
        if status in terminal:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    creative_root = tmp_path / "creative_tmp"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, creative_root  # type: ignore[misc]
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_free_text_to_durable_queue_to_verified_artifact(harness) -> None:  # noqa: ANN001
    queue, creative_root = harness
    actor, project = _project()
    workspace = creative_root / "creative_handoff_e2e"
    workspace.mkdir()
    _clip(workspace / "real_source.mp4")

    outcome = await run_free_text_intent_durable(
        "trim the first second",
        gateway=_gateway(actor, project, ScriptedProducer()),
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=42,
        chat_id=4242,
    )
    assert outcome.status == "enqueued"
    assert outcome.job_id

    # Same intent again -> same durable row, no second execution.
    again = await run_free_text_intent_durable(
        "trim the first second",
        gateway=_gateway(actor, project, ScriptedProducer()),
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=42,
        chat_id=4242,
    )
    assert again.job_id == outcome.job_id
    # AC-4: a duplicate enqueue creates NO second durable row and NO second
    # execution — one logical job for one intent.
    assert queue.job_ids() == [outcome.job_id]

    status = await _drain(queue, outcome.job_id)
    assert status is JobStatus.COMPLETED
    result = await queue.get_result(outcome.job_id)
    assert result is not None and result["success"] is True
    assert result["operation"] == "timeline.trim"

    # ── real artifact, measured independently ──
    artifact = Path(str(result["artifact_path"]))
    assert artifact.is_file() and artifact.stat().st_size > 0
    measured = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert measured == result["sha256"]
    assert 700_000 <= result["duration_us"] <= 1_300_000
    verification = result["artifact_verification"]
    assert verification["status"] == "verified"
    assert verification["probe"]["duration_us"] == result["duration_us"]

    # ── one logical execution for the duplicate intent ──
    history = await queue.get_attempt_history(outcome.job_id)
    assert len([h for h in history if h["status"] == "completed"]) == 1


@pytest.mark.asyncio
async def test_tampered_artifact_fails_independent_verification(harness) -> None:  # noqa: ANN001
    queue, creative_root = harness
    actor, project = _project()
    workspace = creative_root / "creative_handoff_tamper"
    workspace.mkdir()
    _clip(workspace / "real_source.mp4")

    outcome = await run_free_text_intent_durable(
        "trim",
        gateway=_gateway(actor, project, ScriptedProducer()),
        queue=queue,
        project=project,
        source_path=str(workspace / "real_source.mp4"),
        workspace_dir=str(workspace),
        user_id=1,
        chat_id=1,
    )
    assert await _drain(queue, outcome.job_id) is JobStatus.COMPLETED
    result = await queue.get_result(outcome.job_id)
    # The queue verified against the per-attempt workspace it rendered into; the
    # independent verifier call below must use that same expected root.
    verification = result["artifact_verification"]
    verifier_payload = {
        "workspace_dir": verification["physical_identity"]["workspace"],
        "idempotency_key": json.loads(str(queue._fetch_row_full(outcome.job_id)["payload_json"]))[
            "idempotency_key"
        ],
    }

    # Sanity: the pristine artifact verifies.
    assert creative_render_verifier(verifier_payload, result).ok is True

    # Mutate the artifact bytes after the fact.
    artifact = Path(str(result["artifact_path"]))
    blob = bytearray(artifact.read_bytes())
    blob[-1] ^= 0xFF
    artifact.write_bytes(bytes(blob))

    tampered = creative_render_verifier(verifier_payload, result)
    assert tampered.ok is False
    assert tampered.reason_code is not None


def _child_env(tmp_path: Path, db: Path, creative_root: Path) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    pythonpath = str(repo_root / "src")
    return {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "CREATIVE_TEMP_DIR": str(creative_root),
        "QUEUE_DB_PATH": str(db),
        "JOB_ID_FILE": str(tmp_path / "job-id.txt"),
        "SOURCE_PATH": str(creative_root / "creative_handoff_crash" / "real_source.mp4"),
    }


@pytest.mark.asyncio
async def test_enqueue_survives_process_death_and_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creative_root = tmp_path / "creative_tmp"
    workspace = creative_root / "creative_handoff_crash"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()
    source = workspace / "real_source.mp4"
    _clip(source)
    db = tmp_path / "crash.sqlite3"
    env = _child_env(tmp_path, db, creative_root)

    # A separate process composes the chain, enqueues the typed job, and dies
    # immediately — the durable row must outlive it.
    child = subprocess.run(
        [sys.executable, "-c", _CHILD_ENQUEUE_AND_CRASH],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    job_id = (tmp_path / "job-id.txt").read_text(encoding="utf-8")

    # A fresh process (this one) recovers the pending row through the existing
    # worker and produces the real artifact.
    queue = InProcessJobQueue(db)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    resumed = await queue.resume_pending_jobs()
    assert job_id in resumed
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    artifact = Path(str(result["artifact_path"]))
    assert artifact.is_file()
    assert "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest() == result["sha256"]
    settings_module.get_settings.cache_clear()


_CHILD_ENQUEUE_AND_CRASH = """
import asyncio, json, os
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.creative.render_jobs import build_job_bus
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AssetRecord, Timeline, new_project
from nexus_ai_agent.nagar.cognition import build_cognition_gateway
from nexus_ai_agent.nagar.creative import run_free_text_intent_durable


class ScriptedProducer:
    consults_model = True

    async def complete(self, prompt, *, idempotency_key=None):
        return json.dumps({
            "schema_id": "nagar.gateway.proposal.v1",
            "schema_version": 1,
            "operation": "timeline.trim",
            "input": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1000000},
            "rationale": "scripted", "confidence": 1.0,
        })


actor = ActorIdentity(kind="user", actor_id="operator")
project = new_project("nagar-durable-e2e", "durable",
                      Timeline(timeline_id="tl", duration_us=2000000)).model_copy(
    update={"assets": [AssetRecord(asset_id="src", media_kind="video",
                                   content_sha256="sha256:" + "0" * 64, duration_us=2000000)]})
access = ProjectAccess(actor=actor, project_id="nagar-durable-e2e",
                       permissions=frozenset({"project:read", "project:write"}))
gateway = build_cognition_gateway(bus=build_job_bus(project, authorizer=access),
                                  actor=actor, project_id="nagar-durable-e2e",
                                  enabled=True, completion=ScriptedProducer())
queue = InProcessJobQueue(os.environ["QUEUE_DB_PATH"])


async def main():
    outcome = await run_free_text_intent_durable(
        "trim", gateway=gateway, queue=queue, project=project,
        source_path=os.environ["SOURCE_PATH"],
        workspace_dir=str(Path(os.environ["SOURCE_PATH"]).parent),
        user_id=1, chat_id=1)
    Path(os.environ["JOB_ID_FILE"]).write_text(outcome.job_id, encoding="utf-8")
    os._exit(0)  # die before the scheduled task can run: the row stays PENDING


asyncio.run(main())
"""


_CHILD_RENDER_THEN_CRASH_IN_VERIFY = """
import asyncio, json, os
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.creative.render_jobs import build_job_bus
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AssetRecord, Timeline, new_project
from nexus_ai_agent.nagar.cognition import build_cognition_gateway
from nexus_ai_agent.nagar.creative import run_free_text_intent_durable
from nexus_ai_agent.worker import default_job_handlers


class ScriptedProducer:
    consults_model = True

    async def complete(self, prompt, *, idempotency_key=None):
        return json.dumps({
            "schema_id": "nagar.gateway.proposal.v1",
            "schema_version": 1,
            "operation": "timeline.trim",
            "input": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1000000},
            "rationale": "scripted", "confidence": 1.0,
        })


class CrashDuringVerificationQueue(InProcessJobQueue):
    async def _verify_safely(self, verifier, payload, result):
        os._exit(73)


actor = ActorIdentity(kind="user", actor_id="operator")
project = new_project("nagar-durable-e2e", "durable",
                      Timeline(timeline_id="tl", duration_us=2000000)).model_copy(
    update={"assets": [AssetRecord(asset_id="src", media_kind="video",
                                   content_sha256="sha256:" + "0" * 64, duration_us=2000000)]})
access = ProjectAccess(actor=actor, project_id="nagar-durable-e2e",
                       permissions=frozenset({"project:read", "project:write"}))
gateway = build_cognition_gateway(bus=build_job_bus(project, authorizer=access),
                                  actor=actor, project_id="nagar-durable-e2e",
                                  enabled=True, completion=ScriptedProducer())
queue = CrashDuringVerificationQueue(os.environ["QUEUE_DB_PATH"])
for job_type, handler in default_job_handlers().items():
    queue.register_handler(job_type, handler)


async def main():
    outcome = await run_free_text_intent_durable(
        "trim", gateway=gateway, queue=queue, project=project,
        source_path=os.environ["SOURCE_PATH"],
        workspace_dir=str(Path(os.environ["SOURCE_PATH"]).parent),
        user_id=1, chat_id=1)
    Path(os.environ["JOB_ID_FILE"]).write_text(outcome.job_id, encoding="utf-8")
    await asyncio.sleep(180)  # the scheduled worker runs and crashes in _verify_safely


asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_stale_attempt_cannot_finalize_and_retry_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creative_root = tmp_path / "creative_tmp"
    workspace = creative_root / "creative_handoff_fencing"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()
    source = workspace / "real_source.mp4"
    _clip(source)
    db = tmp_path / "fencing.sqlite3"
    env = _child_env(tmp_path, db, creative_root)
    env["SOURCE_PATH"] = str(source)

    # A process renders the artifact but dies during verification: the attempt
    # is left uncommitted, its fencing token superseded by the recovery below.
    child = subprocess.run(
        [sys.executable, "-c", _CHILD_RENDER_THEN_CRASH_IN_VERIFY],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 73, child.stderr
    job_id = (tmp_path / "job-id.txt").read_text(encoding="utf-8")

    queue = InProcessJobQueue(db)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    # Startup recovery takes over the orphaned in-flight row (a fresh fencing
    # token supersedes the dead process's), then re-executes it exactly once.
    resumed = await queue.resume_pending()
    assert job_id in resumed
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    artifact = Path(str(result["artifact_path"]))
    assert "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest() == result["sha256"]

    # The stale attempt is recorded as interrupted; exactly one attempt completed,
    # and the fenced CAS means the dead process cannot finalize the current job.
    history = await queue.get_attempt_history(job_id)
    statuses = [item["status"] for item in history]
    assert statuses[-1] == "completed"
    assert statuses.count("completed") == 1
    assert any(s in {"interrupted", "verifying", "failed"} for s in statuses[:-1])
    settings_module.get_settings.cache_clear()
