"""GAP-D executable evidence after STOP-B retires legacy video editing.

The ``POST /creative/video-edit`` route now exists only as an unconditional
410 tombstone; job-status GET remains HMAC-gated. This file keeps the residual
legacy-lane questions reproducible:

1. reachability — POST is inert and GET stays behind the fail-closed HMAC gate;
2. false-success potential — the standalone legacy registry can still persist
   ``done`` with NO independent artifact verification, but the retired POST
   cannot create or process jobs;
3. production surface — the deployed container runs the bot, not the API app;
4. isolation — the legacy lane never touches the canonical job queue.

The evidence/report entry is updated in
``docs/audits/VERIFICATION_GAP_REPORT_2026-09-24.md``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nexus_ai_agent.creative.job_registry import JobRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


def _app_source() -> str:
    return (REPO_ROOT / "src" / "nexus_ai_agent" / "api" / "app.py").read_text(encoding="utf-8")


def test_legacy_post_is_inert_410_and_job_status_get_stays_hmac_gated() -> None:
    """Q1 reachability: POST is retired; sensitive GET remains authenticated."""
    source = _app_source()
    assert '@app.post("/creative/video-edit", deprecated=True, status_code=410)' in source
    assert '@app.get("/creative/jobs/{job_id}", deprecated=True)' in source
    post_body = source.split('@app.post("/creative/video-edit"', 1)[1].split(
        '@app.get("/creative/jobs/{job_id}"', 1
    )[0]
    assert "require_hmac_signature" not in post_body
    assert "get_creative_registry" not in post_body
    assert "background_tasks" not in post_body
    assert "execute_ffmpeg_commands" not in source
    get_body = source.split('@app.get("/creative/jobs/{job_id}"', 1)[1]
    assert "await require_hmac_signature(request)" in get_body.split("async def", 2)[1]


def test_legacy_registry_can_record_done_without_any_artifact(tmp_path: Path) -> None:
    """Q2 false-success potential: ``done`` with no verification — the GAP.

    This is a characterization test: it PINS the current (unsafe) semantics
    so any future change — closing or widening the gap — is a visible,
    intentional diff. The canonical queue's rule (execution success ≠ job
    success) does NOT apply here; that asymmetry is exactly GAP-D.
    """
    registry = JobRegistry(tmp_path / "creative_jobs.sqlite3")

    async def _scenario() -> dict[str, object]:
        await registry.initialize()
        job_id = await registry.create_job("video_edit", {"source_type": "upload"})
        await registry.update_job_status(job_id, "done", result={"output": "claimed.mp4"})
        job = await registry.get_job(job_id)
        assert job is not None
        return job

    job = asyncio.run(_scenario())
    # "done" was persisted with zero artifact evidence — the recorded risk.
    assert job["status"] == "done"


def test_legacy_lane_is_not_on_the_deployed_production_surface() -> None:
    """Q3 production surface: the container CMD runs the bot, not the API."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_lines = [line for line in dockerfile.splitlines() if line.strip().startswith("CMD")]
    assert cmd_lines, "Dockerfile must define a CMD"
    assert any("run-bot" in line for line in cmd_lines)
    assert not any("run-api" in line or "uvicorn" in line for line in cmd_lines)


def test_legacy_lane_never_touches_the_canonical_job_queue() -> None:
    """Q4 isolation: no import path from the legacy lane into the contract."""
    source = _app_source()
    assert "in_process_job_queue" not in source
    assert "jobs.lifecycle" not in source
    assert "artifact_verifiers" not in source
