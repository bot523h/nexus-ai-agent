from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.product.studio_read import ProjectReadDenied, StudioReadService


class FakeJobs:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.calls.append(job_id)
        return self.rows.get(job_id)


@pytest.mark.asyncio
async def test_service_reads_real_row_and_keeps_missing_truth_as_gaps() -> None:
    jobs = FakeJobs(
        {
            "job-a": {
                "id": "job-a",
                "status": "failed_terminal",
                "input_data": {
                    "project_id": "project-a",
                    "intent": {"intent_id": "intent-a"},
                    "revision": 3,
                },
                "result": {"error": "typed_failure:invalid_input"},
            }
        }
    )
    view = await StudioReadService(jobs).read_job(project_id="project-a", job_id="job-a")
    assert view.execution is not None
    assert view.execution.status == "failed_terminal"
    assert view.artifact is not None
    assert view.artifact.verification_status == "not_available"
    assert "verification_evidence_missing" not in view.contract_gaps
    assert "evidence" in view.contract_gaps
    assert view.lineage.artifact_id is None


@pytest.mark.asyncio
async def test_project_b_cannot_read_project_a() -> None:
    jobs = FakeJobs(
        {
            "job-a": {
                "id": "job-a",
                "status": "completed",
                "input_data": {"project_id": "project-a"},
                "result": {},
            }
        }
    )
    with pytest.raises(ProjectReadDenied):
        await StudioReadService(jobs).read_job(project_id="project-b", job_id="job-a")
    assert jobs.calls == ["job-a"]


@pytest.mark.asyncio
async def test_missing_job_returns_valid_empty_state_without_fake_completion() -> None:
    view = await StudioReadService(FakeJobs({})).read_job(project_id="project-a", job_id="missing")
    assert view.execution is None
    assert view.artifact is None
    assert view.contract_gaps[0] == "job_not_found"


def test_http_surface_authenticates_and_enforces_configured_project_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "dashboard-secret")
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "project-a")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    from nexus_ai_agent.api import app as app_module

    class RowReader:
        async def get_job(self, job_id: str) -> dict[str, Any] | None:
            return {
                "id": job_id,
                "status": "verifying",
                "input_data": {"project_id": "project-a", "intent": {"intent_id": "i-a"}},
                "result": {},
            }

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: RowReader())
    client = TestClient(app_module.app)
    base = "/api/studio/projects/project-a/jobs/job-1/state"
    assert client.get(base).status_code == 401
    ok = client.get(base, headers={"Authorization": "Bearer dashboard-secret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["project_id"] == "project-a"
    assert body["execution"]["status"] == "verifying"
    assert body["artifact"] is not None
    assert "artifact_verification_missing" in body["contract_gaps"]
    assert (
        client.get(
            "/api/studio/projects/project-b/jobs/job-1/state",
            headers={"Authorization": "Bearer dashboard-secret"},
        ).status_code
        == 404
    )
    assert (
        "project-a"
        not in client.get(
            "/api/studio/projects/project-b/jobs/job-1/state",
            headers={"Authorization": "Bearer dashboard-secret"},
        ).text
    )
    settings_module.get_settings.cache_clear()
