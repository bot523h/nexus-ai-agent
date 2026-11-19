"""NAGAR One V1: the real ``storyboard_render`` job through the real queue.

REAL-COMPUTE: a real ffmpeg render. REAL-DISK: the MP4, passport and durable
approval SQLite file are written to and read from a temporary studio workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    default_artifact_verifiers,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_jobs import (
    ENV_SIGNING_KEY,
    ENV_WORKSPACE,
    storyboard_render_job,
    storyboard_render_verifier,
)
from nexus_ai_agent.creative.storyboard_render import issue_render_approval
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.worker import default_job_handlers

SIGNING_KEY = "k" * 48
SCRIPT = (
    "INT. KITCHEN - DAY\n"
    "Sara: Hello, are you there?\n"
    "Ali: Yes, I am here.\n"
    "EXT. STREET - NIGHT\n"
    "سارا: سلام، حالت چطوره؟\n"
)
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
JOB_ID = "job-queue-1"
PROJECT_ID = "proj-queue-1"
PRINCIPAL_ID = "owner-queue-1"


@pytest.fixture()
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure the studio via environment, approve one render, return the workspace."""
    workspace = tmp_path / "studio"
    workspace.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(ENV_SIGNING_KEY, SIGNING_KEY)
    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=profile_hash,
        )
    finally:
        ledger.close()
    assert approval_id
    return workspace


def _payload(approval_id: str) -> dict[str, object]:
    compiled = compile_storyboard(SCRIPT)
    return build_storyboard_render_payload(
        {
            "project_id": PROJECT_ID,
            "job_id": JOB_ID,
            "screenplay_text": SCRIPT,
            "source_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
            "manifest_sha256": compiled.manifest_sha256,
            "approval_id": approval_id,
            "principal_id": PRINCIPAL_ID,
            **PROFILE,
        }
    )


def _approval_id(workspace: Path) -> str:
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        rows = ledger._all()
        assert len(rows) == 1
        return rows[0].approval_id
    finally:
        ledger.close()


async def _drive_through_queue(
    db_path: Path, payload: dict[str, object]
) -> tuple[JobStatus, dict[str, object] | None]:
    queue = InProcessJobQueue(
        db_path,
        handlers=default_job_handlers(),
        artifact_verifiers=default_artifact_verifiers(),
    )
    await queue.acquire_ownership()
    job_id = await queue.enqueue(
        job_type="storyboard_render",
        idempotency_key="storyboard-queue-1",
        payload=payload,
    )
    deadline = time.monotonic() + 180
    while True:
        status = await queue.get_status(job_id)
        if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL):
            return status, await queue.get_result(job_id)
        if time.monotonic() > deadline:
            raise AssertionError(f"job still {status} after 180s")
        await asyncio.sleep(0.2)


def test_storyboard_job_completes_through_real_queue_with_verified_artifact(
    studio: Path, tmp_path: Path
) -> None:
    payload = _payload(_approval_id(studio))
    status, result = asyncio.run(_drive_through_queue(tmp_path / "queue.sqlite3", payload))

    assert status is JobStatus.COMPLETED
    assert result is not None
    assert result["status"] == "rendered"
    assert result["verifier_status"] == "PASS"
    mp4 = studio / str(result["output_rel_path"])
    passport = studio / str(result["passport_rel_path"])
    assert mp4.is_file() and mp4.stat().st_size > 0
    assert passport.is_file()


def _render_once(studio: Path) -> tuple[dict[str, object], dict[str, object]]:
    payload = _payload(_approval_id(studio))
    result = asyncio.run(storyboard_render_job(payload))
    return payload, result


def test_forged_passport_is_refused_with_passport_prefixed_code(studio: Path) -> None:
    payload, result = _render_once(studio)
    passport_path = studio / str(result["passport_rel_path"])
    forged = json.loads(passport_path.read_text(encoding="utf-8"))
    forged["probed_duration_s"] = 9999.0  # field changed, seal left untouched
    passport_path.write_text(json.dumps(forged), encoding="utf-8")

    outcome = storyboard_render_verifier(payload, result)

    assert outcome.ok is False
    assert outcome.reason_code == "passport_seal_mismatch"
    assert outcome.reason_code is not None and outcome.reason_code.startswith("passport_")


def test_output_outside_workspace_is_refused_explicitly(studio: Path) -> None:
    payload, result = _render_once(studio)
    escaped = dict(result)
    escaped["output_rel_path"] = "../escaped.mp4"

    outcome = storyboard_render_verifier(payload, escaped)

    assert outcome.ok is False
    assert outcome.reason_code == "path_outside_workspace"


def test_verifier_refuses_when_studio_is_not_configured(
    studio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _render_once(studio)
    monkeypatch.delenv(ENV_WORKSPACE)

    outcome = storyboard_render_verifier(payload, result)

    assert outcome.ok is False
    assert outcome.reason_code == "studio_not_configured"
