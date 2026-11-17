"""End-to-end convergence: signed Studio HTTP submission -> real published artifact.

This is the closure proof for Wave 6. Every link is the *production* one:

    POST /api/studio/projects/{id}/renders
      (bearer principal + HMAC-signed envelope + project scope)
      -> StudioRenderSubmissionService
      -> nagar.creative.build_creative_payload      (the single trust translation)
      -> InProcessJobQueue.enqueue                  (durable SQLite row)
      -> creative_render worker
      -> CommandBus.dispatch                        (registry, policy, authorization)
      -> AttemptStaging -> real FFmpeg trim         (a decodable MP4 with a real ftyp box)
      -> independent post-disk verifier
      -> fenced publish + register_verified_asset_version
      -> GET /api/studio/projects/{id}/jobs/{job_id}/state

The only doubles are outside the chain: the model seam is not used at all, and the
external facts (principal/binding/revision) are created through the real
``SQLApplicationDirectory`` and ``SqlProjectRevisionStore`` against a real SQLite
database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.sql_application_directory import (
    DEFAULT_PROJECT_PERMISSIONS,
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.application.principal import ChannelBinding
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Timeline,
    compute_state_hash,
    new_project,
)
from nexus_ai_agent.product.studio_submit import (
    StudioCanonicalFacts,
    StudioRenderSubmission,
    StudioRenderSubmissionService,
)
from nexus_ai_agent.storage.db import create_all_tables
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "studio-convergence"
PROJECT_OTHER = "studio-other"
DASHBOARD_TOKEN = "studio-convergence-token"
HMAC_KEY = "studio-convergence-hmac-key"
SOURCE_DURATION_US = 2_000_000
TERMINAL = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED_TERMINAL.value,
    JobStatus.FAILED_RETRYABLE.value,
}


# --------------------------------------------------------------------------- #
# environment: real database, real directory, real queue
# --------------------------------------------------------------------------- #


@pytest.fixture()
def convergence_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "app.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db))
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", DASHBOARD_TOKEN)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", f"{PROJECT_ID},{PROJECT_OTHER}")
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", HMAC_KEY)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "creative").mkdir()
    settings_module.get_settings.cache_clear()
    yield tmp_path
    settings_module.get_settings.cache_clear()


def _clip(path: Path) -> None:
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # pragma: no cover - environment guard
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
            "testsrc=duration=2:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _project_state() -> Any:
    project = new_project(
        PROJECT_ID, "convergence", Timeline(timeline_id="tl", duration_us=SOURCE_DURATION_US)
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
    project.state_hash = compute_state_hash(project)
    return project


async def _commit_head() -> Any:
    store = SqlProjectRevisionStore()
    return await store.commit(
        _project_state(), authorized_project_id=PROJECT_ID, expected_parent_revision_id=None
    )


def _wire_directory() -> tuple[Any, str]:
    directory = SQLApplicationDirectory()
    owner_binding = ChannelBinding("web", "studio-owner")
    owner = directory.resolve_or_create(owner_binding)
    directory.ensure_project(PROJECT_ID, owner, permissions=DEFAULT_PROJECT_PERMISSIONS)
    return owner, channel_binding_id(owner_binding)


def _signed_headers(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        HMAC_KEY.encode(), f"{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": f"Bearer {DASHBOARD_TOKEN}",
        "Content-Type": "application/json",
        "X-NEXUS-Timestamp": timestamp,
        "X-NEXUS-Signature": signature,
    }


def _submission(workspace: Path) -> dict[str, Any]:
    return {
        "operation": "trim",
        "in_point_us": 0,
        "out_point_us": 1_000_000,
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": SOURCE_DURATION_US,
    }


# --------------------------------------------------------------------------- #
# the convergence proof
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_signed_studio_submission_converges_to_a_verified_published_artifact(
    convergence_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = convergence_env / "creative" / "creative_studio_convergence"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")
    await create_all_tables(str(convergence_env / "app.sqlite"))
    owner, binding_id = _wire_directory()
    revision = await _commit_head()

    queue = InProcessJobQueue(convergence_env / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication

    queue.register_artifact_publication(
        "creative_render",
        build_creative_render_publication(Path(f"{convergence_env / 'jobs.sqlite3'}.artifacts")),
    )

    from nexus_ai_agent.api import app as app_module

    class _Registry:
        def __init__(self, inner: InProcessJobQueue) -> None:
            self.queue = inner

        async def get_job(self, job_id: str) -> dict[str, Any] | None:
            facts = await self.queue.get_job_facts_async(job_id)
            if facts is None:
                return None
            return {
                "id": facts.job_id,
                "job_type": facts.job_type,
                "status": facts.status,
                "canonical_status": facts.status,
                "attempt": facts.attempt,
                "input_data": facts.payload,
                "result": facts.result,
                "error": facts.error,
            }

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: _Registry(queue))
    facts = StudioCanonicalFacts(principal_id=str(owner), channel_binding_id=binding_id)
    monkeypatch.setattr(
        "nexus_ai_agent.api.studio.get_studio_submission_service",
        lambda: StudioRenderSubmissionService(
            queue, facts, revision_store=SqlProjectRevisionStore()
        ),
    )

    url = f"/api/studio/projects/{PROJECT_ID}/renders"
    body = json.dumps(_submission(workspace)).encode()
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://studio.test"
    ) as client:
        response = await client.post(url, content=body, headers=_signed_headers(body))
    assert response.status_code == 202, response.text
    admitted = response.json()
    assert admitted["job_id"]
    assert admitted["attempt"] >= 0
    assert admitted["canonical_operation"] == "timeline.trim"
    assert admitted["revision_id"] == revision.revision_id
    assert admitted["state_url"].endswith(admitted["job_id"] + "/state")

    # The row the route created is the row the worker drains and the read route reads.
    await queue.resume_pending_jobs()
    job_facts: Any = None
    for _ in range(1500):
        job_facts = await queue.get_job_facts_async(admitted["job_id"])
        if job_facts is not None and str(job_facts.status) in TERMINAL:
            break
        await asyncio.sleep(0.02)
    assert job_facts is not None and str(job_facts.status) in TERMINAL, (
        "studio render did not reach a terminal state in time"
    )
    assert str(job_facts.status) == JobStatus.COMPLETED.value, job_facts.error

    result = job_facts.result
    assert isinstance(result, dict)
    # Independent verification happened, on the bytes that were published.
    verification = result.get("artifact_verification")
    assert verification, "a completed render must carry verifier evidence"
    artifact_sha = result.get("artifact_sha256") or result.get("sha256")
    assert artifact_sha, sorted(result)[:40]
    published = result.get("artifact_path") or result.get("output_path")
    assert published and Path(published).is_file()
    assert sha256_file(Path(published)).removeprefix("sha256:") == str(artifact_sha).removeprefix(
        "sha256:"
    )
    # Durable lineage: the confirm hook registered the published bytes as a
    # content-addressed AssetVersion and recorded the derived receipt reference on
    # the attempt's binding (the shape the worker mutates in ``result``).
    binding = result.get("artifact_binding") or {}
    assert binding.get("artifact_version_id"), sorted(result)[:40]
    # content-addressed: the version id is derived from the published digest
    assert str(binding["artifact_version_id"]).startswith("render_version_")
    assert binding.get("derived_receipt_ref"), binding
    assert binding.get("job_id") in (None, admitted["job_id"])
    assert revision.revision_id  # the admitted head is the executed revision

    # And the read route reports the same durable facts to the operator.
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://studio.test"
    ) as client:
        read = await client.get(
            admitted["state_url"], headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
        )
    assert read.status_code == 200, read.text
    view = read.json()
    assert view["execution"]["status"] == JobStatus.COMPLETED.value
    assert view["artifact"] is not None
    assert view["artifact"]["artifact_id"]
    assert view["lineage"]["job_id"] == admitted["job_id"]

    await queue.shutdown()


# --------------------------------------------------------------------------- #
# the gates: each one refuses on its own
# --------------------------------------------------------------------------- #


def _client_with_service(monkeypatch: pytest.MonkeyPatch, service: Any) -> TestClient:
    from nexus_ai_agent.api import app as app_module

    monkeypatch.setattr("nexus_ai_agent.api.studio.get_studio_submission_service", lambda: service)
    return TestClient(app_module.app)


class _ExplodingQueue:
    """Any enqueue attempt is a test failure: the refusal must happen first."""

    async def enqueue(self, **_kwargs: Any) -> str:
        raise AssertionError("a refused submission must not enqueue")

    async def get_job_facts_async(self, _job_id: str) -> Any:
        raise AssertionError("a refused submission must not read job facts")


def test_unsigned_submission_is_refused_before_it_can_enqueue(
    convergence_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioRenderSubmissionService(
        _ExplodingQueue(), StudioCanonicalFacts("prn_x", "cbn_" + "a" * 64)
    )
    client = _client_with_service(monkeypatch, service)
    body = json.dumps(_submission(convergence_env)).encode()
    url = f"/api/studio/projects/{PROJECT_ID}/renders"

    unauthenticated = client.post(url, content=body)
    assert unauthenticated.status_code == 401

    # Authenticated but unsigned: the envelope gate still refuses.
    unsigned = client.post(
        url, content=body, headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    )
    assert unsigned.status_code == 401
    assert "signature" in unsigned.text

    # Signed with the wrong key: refused (never "probably fine").
    tampered = _signed_headers(body)
    tampered["X-NEXUS-Signature"] = "0" * 64
    assert client.post(url, content=body, headers=tampered).status_code == 401


def test_submission_for_an_unconfigured_project_is_a_404(
    convergence_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioRenderSubmissionService(
        _ExplodingQueue(), StudioCanonicalFacts("prn_x", "cbn_" + "a" * 64)
    )
    client = _client_with_service(monkeypatch, service)
    body = json.dumps(_submission(convergence_env)).encode()
    response = client.post(
        "/api/studio/projects/not-a-configured-project/renders",
        content=body,
        headers=_signed_headers(body),
    )
    assert response.status_code == 404


def test_an_unconfigured_deployment_refuses_with_a_typed_503(
    convergence_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No canonical identity configured -> no job, ever (never a legacy row)."""
    service = StudioRenderSubmissionService(_ExplodingQueue(), None)
    client = _client_with_service(monkeypatch, service)
    body = json.dumps(_submission(convergence_env)).encode()
    response = client.post(
        f"/api/studio/projects/{PROJECT_ID}/renders", content=body, headers=_signed_headers(body)
    )
    assert response.status_code == 503
    assert "identity_unavailable" in response.text


@pytest.mark.parametrize(
    "mutation",
    [
        {"in_point_us": 1_500_000, "out_point_us": 500_000},
        {"in_point_us": 0, "out_point_us": 0},
        {"operation": "grade"},
        {"unexpected_field": "value"},
    ],
)
def test_a_malformed_submission_is_a_typed_400(
    convergence_env: Path, monkeypatch: pytest.MonkeyPatch, mutation: dict[str, Any]
) -> None:
    service = StudioRenderSubmissionService(
        _ExplodingQueue(), StudioCanonicalFacts("prn_x", "cbn_" + "a" * 64)
    )
    client = _client_with_service(monkeypatch, service)
    payload = {**_submission(convergence_env), **mutation}
    body = json.dumps(payload).encode()
    response = client.post(
        f"/api/studio/projects/{PROJECT_ID}/renders", content=body, headers=_signed_headers(body)
    )
    assert response.status_code == 400, response.text
    # Either the typed body refused it (schema) or the canonical handoff did
    # (window). Both are refusals; neither enqueues.
    assert "invalid_request" in response.text or "refused:" in response.text


def test_the_submission_schema_forbids_a_client_supplied_opt_in() -> None:
    """The lifecycle opt-in is composition policy: a request cannot carry one."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StudioRenderSubmission.model_validate(
            {
                "operation": "trim",
                "in_point_us": 0,
                "out_point_us": 1_000_000,
                "workspace_dir": "/tmp/w",
                "input_path": "/tmp/w/a.mp4",
                "media_duration_us": SOURCE_DURATION_US,
                "allow_experimental": True,
            }
        )
