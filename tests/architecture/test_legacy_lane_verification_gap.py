"""GAP-D executable evidence: the legacy ``/creative`` HTTP lane, characterized.

Task-180 does NOT change this lane — it is frozen + deprecated by D-0010
and enforced by ``test_legacy_creative_boundary.py``; removal is sequenced
after PR#58. This file converts the GAP-D questions into reproducible
evidence so the recorded gap cannot silently drift:

1. reachability — the two routes exist, are marked ``deprecated=True`` and
   sit behind the fail-closed HMAC gate;
2. false-success potential — the lane's own registry persists ``done`` with
   NO independent artifact verification (the documented risk);
3. production surface — the deployed container runs the bot, not the API
   app, so the lane is code-reachable but not on the deployed surface;
4. isolation — the lane never touches the canonical job queue, so the
   task-178/180 verification contract cannot leak in (or be bypassed) here.

The remediation owner, dependency and acceptance criteria live in
``docs/audits/VERIFICATION_GAP_REPORT_2026-09-24.md`` (GAP-D entry).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nexus_ai_agent.creative.job_registry import JobRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


def _app_source() -> str:
    return (REPO_ROOT / "src" / "nexus_ai_agent" / "api" / "app.py").read_text(encoding="utf-8")


def test_legacy_routes_are_frozen_deprecated_and_hmac_gated() -> None:
    """Q1 reachability: present, deprecated, fail-closed authenticated."""
    source = _app_source()
    assert '@app.post("/creative/video-edit", deprecated=True)' in source
    assert '@app.get("/creative/jobs/{job_id}", deprecated=True)' in source
    # the HMAC gate runs before any form parsing in the POST handler and
    # before any row is read in the GET handler (D-0010 hardening, PR#65)
    post_body = source.split('@app.post("/creative/video-edit"', 1)[1]
    assert "await require_hmac_signature(request)" in post_body.split("async def", 2)[1]
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
