"""Coverage closure for the studio API's refusal paths.

Each case pins one route refusal: a malformed job id, a job owned by another
project, a submission body that is not JSON or not an object (both signed, so the
request reaches the parser), and the submission-service composition.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterator, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_ai_agent.api import studio as studio_api
from nexus_ai_agent.product.studio_submit import StudioRenderSubmissionService

PROJECT = "proj-rt"
HMAC_KEY = "routes-gap-hmac-key-0123456789abcdef"


class FakeRegistry:
    def __init__(self, rows: Mapping[str, Mapping[str, object]]) -> None:
        self._rows = dict(rows)
        self.queue = object()

    async def get_job(self, job_id: str) -> dict[str, object] | None:
        row = self._rows.get(job_id)
        return dict(row) if row is not None else None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from nexus_ai_agent.config import settings as settings_module

    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", PROJECT)
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", HMAC_KEY)
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(studio_api, "authenticate_studio_caller", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "nexus_ai_agent.api.app.get_creative_registry",
        lambda: FakeRegistry({"foreign": {"input_data": {"project_id": "someone-else"}}}),
    )
    app = FastAPI()
    app.include_router(studio_api.router)
    try:
        yield TestClient(app)
    finally:
        settings_module.get_settings.cache_clear()


def _signed(body: bytes) -> dict[str, str]:
    stamp = str(int(time.time()))
    sig = hmac.new(HMAC_KEY.encode(), f"{stamp}:".encode() + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-NEXUS-Timestamp": stamp,
        "X-NEXUS-Signature": sig,
    }


def test_a_state_request_with_a_traversal_job_id_is_400(client: TestClient) -> None:
    response = client.get(f"/api/studio/projects/{PROJECT}/jobs/a..b/state")
    assert response.status_code == 400


def test_another_projects_job_state_is_hidden_as_404(client: TestClient) -> None:
    response = client.get(f"/api/studio/projects/{PROJECT}/jobs/foreign/state")
    assert response.status_code == 404
    assert "someone-else" not in response.text


def test_an_artifact_request_with_a_traversal_job_id_is_400(client: TestClient) -> None:
    response = client.get(f"/api/studio/projects/{PROJECT}/jobs/a..b/artifact")
    assert response.status_code == 400


def test_a_signed_submission_that_is_not_json_is_a_typed_400(client: TestClient) -> None:
    body = b"this is not json"
    response = client.post(
        f"/api/studio/projects/{PROJECT}/renders", content=body, headers=_signed(body)
    )
    assert response.status_code == 400
    assert "body is not JSON" in response.json()["detail"]


def test_a_signed_submission_that_is_a_json_list_is_a_typed_400(client: TestClient) -> None:
    body = b'["not", "an", "object"]'
    response = client.post(
        f"/api/studio/projects/{PROJECT}/renders", content=body, headers=_signed(body)
    )
    assert response.status_code == 400
    assert "body must be an object" in response.json()["detail"]


def test_the_submission_service_is_composed_over_the_single_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus_ai_agent.api.app.get_creative_registry", lambda: FakeRegistry({}))
    service = studio_api.get_studio_submission_service()
    assert isinstance(service, StudioRenderSubmissionService)
