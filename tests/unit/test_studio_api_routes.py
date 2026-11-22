"""Direct route-state coverage for Studio's fail-closed API edges."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from nexus_ai_agent.api import studio as studio_api
from nexus_ai_agent.creative.artifact_download import DownloadRefused
from nexus_ai_agent.creative.storyboard_jobs import StoryboardJobConfigError
from nexus_ai_agent.product.studio_read import ProjectReadDenied
from nexus_ai_agent.product.studio_submit import StudioSubmissionRefused

PROJECT = "project-w3a"


class _RequestStub:
    async def json(self) -> object:
        return {
            "operation": "trim",
            "in_point_us": 0,
            "out_point_us": 1,
            "workspace_dir": "/workspace",
            "input_path": "input.mp4",
            "media_duration_us": 1,
        }


class _Registry:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self.row = row

    async def get_job(self, _job_id: str) -> Mapping[str, object] | None:
        return self.row


class _Authorized:
    size_bytes = 5
    sha256 = "a" * 64

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Stream:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _View:
    def to_dict(self) -> dict[str, str]:
        return {"state": "read"}


def _row(
    *, status: str = "completed", result: Mapping[str, object] | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "input_data": {"project_id": PROJECT},
        "result": dict(result or {"output_rel_path": "projects/p/render.mp4"}),
    }


def test_project_guard_validates_id_and_scope_before_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", PROJECT)
    monkeypatch.setattr(studio_api, "authenticate_studio_caller", lambda *_args: None)
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as invalid:
        studio_api.require_studio_project("../bad", request)
    assert invalid.value.status_code == 400
    with pytest.raises(HTTPException) as not_configured:
        studio_api.require_studio_project("other", request)
    assert not_configured.value.status_code == 404
    studio_api.require_studio_project(PROJECT, request)


@pytest.mark.asyncio
async def test_state_route_rejects_bad_ids_maps_denial_and_returns_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.api import app as app_module

    with pytest.raises(HTTPException) as bad_id:
        await studio_api.get_studio_job_state(PROJECT, "..bad")
    assert bad_id.value.status_code == 400

    class DeniedRead:
        def __init__(self, _registry: object) -> None:
            pass

        async def read_job(self, **_kwargs: object) -> object:
            raise ProjectReadDenied("hidden")

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: _Registry(None))
    monkeypatch.setattr(studio_api, "StudioReadService", DeniedRead)
    with pytest.raises(HTTPException) as denied:
        await studio_api.get_studio_job_state(PROJECT, "foreign")
    assert denied.value.status_code == 404

    class SuccessfulRead(DeniedRead):
        async def read_job(self, **_kwargs: object) -> _View:
            return _View()

    monkeypatch.setattr(studio_api, "StudioReadService", SuccessfulRead)
    assert await studio_api.get_studio_job_state(PROJECT, "job") == {"state": "read"}


def test_download_status_gate_accepts_only_completed_pass_rows() -> None:
    studio_api._require_completed_and_verified(None)
    with pytest.raises(DownloadRefused) as not_done:
        studio_api._require_completed_and_verified(_row(status="running"))
    assert not_done.value.status_code == 409
    with pytest.raises(DownloadRefused):
        studio_api._require_completed_and_verified(_row(result={}))
    with pytest.raises(DownloadRefused):
        studio_api._require_completed_and_verified(
            _row(result={"artifact_verification": {"status": "violated"}})
        )
    studio_api._require_completed_and_verified(
        _row(result={"artifact_verification": {"status": "verified"}})
    )
    with pytest.raises(DownloadRefused):
        studio_api._require_completed_and_verified(
            _row(result={"artifact_verification": {"media_verdict": "FAIL"}})
        )


@pytest.mark.asyncio
async def test_download_route_maps_refusals_releases_stream_and_returns_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.api import app as app_module

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: _Registry(None))
    with pytest.raises(HTTPException) as missing:
        await studio_api.download_studio_artifact(PROJECT, "job")
    assert missing.value.status_code == 404

    class BrokenRegistry:
        async def get_job(self, _job_id: str) -> object:
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: BrokenRegistry())
    with pytest.raises(RuntimeError, match="registry unavailable"):
        await studio_api.download_studio_artifact(PROJECT, "job")

    row = _row(
        result={
            "output_rel_path": "projects/p/render.mp4",
            "artifact_verification": {"status": "verified"},
        }
    )
    monkeypatch.setattr(app_module, "get_creative_registry", lambda: _Registry(row))
    monkeypatch.setattr(studio_api, "studio_settings", lambda _env: ("/workspace", "key"))
    authorized = _Authorized()
    monkeypatch.setattr(studio_api, "authorize_artifact_download", lambda *_args: authorized)

    def refuse_stream(_authorized: object) -> _Stream:
        raise DownloadRefused(403, "stream_refused")

    monkeypatch.setattr(studio_api, "iter_authorized_chunks", refuse_stream)
    with pytest.raises(HTTPException) as refused:
        await studio_api.download_studio_artifact(PROJECT, "job")
    assert refused.value.status_code == 403
    assert authorized.closed

    authorized = _Authorized()
    monkeypatch.setattr(studio_api, "authorize_artifact_download", lambda *_args: authorized)

    def fail_to_build_stream(_authorized: object) -> _Stream:
        raise StoryboardJobConfigError("stream setup failed")

    monkeypatch.setattr(studio_api, "iter_authorized_chunks", fail_to_build_stream)
    with pytest.raises(HTTPException) as config_error:
        await studio_api.download_studio_artifact(PROJECT, "job")
    assert config_error.value.status_code == 503
    assert authorized.closed

    authorized = _Authorized()
    stream = _Stream()
    monkeypatch.setattr(studio_api, "authorize_artifact_download", lambda *_args: authorized)
    monkeypatch.setattr(studio_api, "iter_authorized_chunks", lambda _authorized: stream)
    response = await studio_api.download_studio_artifact(PROJECT, "job")
    assert isinstance(response, StreamingResponse)
    assert response.headers["content-length"] == "5"
    assert response.headers["x-artifact-sha256"] == "a" * 64
    assert response.background is not None
    await response.background()
    assert stream.closed

    authorized = _Authorized()

    def unexpected(_authorized: object) -> _Stream:
        raise RuntimeError("unexpected stream failure")

    monkeypatch.setattr(studio_api, "authorize_artifact_download", lambda *_args: authorized)
    monkeypatch.setattr(studio_api, "iter_authorized_chunks", unexpected)
    with pytest.raises(RuntimeError, match="unexpected stream failure"):
        await studio_api.download_studio_artifact(PROJECT, "job")
    assert authorized.closed


@pytest.mark.asyncio
async def test_submission_route_maps_body_validation_and_service_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.api import app as app_module

    async def noop_signature(_request: object) -> None:
        return None

    monkeypatch.setattr(app_module, "require_hmac_signature", noop_signature)

    class BadJson:
        async def json(self) -> object:
            raise ValueError("invalid JSON")

    with pytest.raises(HTTPException) as malformed:
        await studio_api.submit_studio_render(PROJECT, BadJson())  # type: ignore[arg-type]
    assert malformed.value.status_code == 400

    class NonObject:
        async def json(self) -> object:
            return []

    with pytest.raises(HTTPException) as non_object:
        await studio_api.submit_studio_render(PROJECT, NonObject())  # type: ignore[arg-type]
    assert non_object.value.status_code == 400

    class InvalidSubmission:
        async def json(self) -> object:
            return {"extra": "forbidden"}

    with pytest.raises(HTTPException) as invalid:
        await studio_api.submit_studio_render(PROJECT, InvalidSubmission())  # type: ignore[arg-type]
    assert invalid.value.status_code == 400

    class RefusingService:
        def __init__(self, reason: str) -> None:
            self.reason = reason

        async def submit(self, **_kwargs: Any) -> object:
            raise StudioSubmissionRefused(self.reason, "refused")

    monkeypatch.setattr(
        studio_api, "get_studio_submission_service", lambda: RefusingService("identity_unavailable")
    )
    with pytest.raises(HTTPException) as unavailable:
        await studio_api.submit_studio_render(PROJECT, _RequestStub())  # type: ignore[arg-type]
    assert unavailable.value.status_code == 503

    monkeypatch.setattr(
        studio_api, "get_studio_submission_service", lambda: RefusingService("invalid_request")
    )
    with pytest.raises(HTTPException) as refused:
        await studio_api.submit_studio_render(PROJECT, _RequestStub())  # type: ignore[arg-type]
    assert refused.value.status_code == 400

    class AcceptingService:
        async def submit(self, **_kwargs: Any) -> dict[str, str]:
            return {"job_id": "job-accepted"}

    monkeypatch.setattr(studio_api, "get_studio_submission_service", lambda: AcceptingService())
    response = await studio_api.submit_studio_render(PROJECT, _RequestStub())  # type: ignore[arg-type]
    assert response.status_code == 202
