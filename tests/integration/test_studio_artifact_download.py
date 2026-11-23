"""NAGAR One V1: the authorized artifact download route, end to end.

REAL-COMPUTE: the MP4 comes from a real ffmpeg render. REAL-DISK: the artifact,
its passport and the durable approval file live in a temporary studio workspace.
The route itself runs under FastAPI's TestClient against that workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_ai_agent.api import studio as studio_api
from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_jobs import (
    ENV_SIGNING_KEY,
    ENV_WORKSPACE,
    storyboard_render_job,
)
from nexus_ai_agent.creative.storyboard_render import issue_render_approval
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)

SIGNING_KEY = "d" * 48
SCRIPT = "INT. KITCHEN - DAY\nSara: Hello there.\nEXT. STREET - NIGHT\nAli: Goodbye.\n"
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
PROJECT_ID = "proj-dl-1"
JOB_ID = "job-dl-1"
PRINCIPAL_ID = "owner-dl-1"


class FakeRegistry:
    """Stands in for the durable job registry: ``get_job`` returns one row or None."""

    def __init__(self, rows: Mapping[str, Mapping[str, object]]) -> None:
        self._rows = dict(rows)

    async def get_job(self, job_id: str) -> dict[str, object] | None:
        row = self._rows.get(job_id)
        return dict(row) if row is not None else None


@pytest.fixture()
def rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Render one approved storyboard and return its result plus the workspace."""
    workspace = tmp_path / "studio"
    workspace.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(ENV_SIGNING_KEY, SIGNING_KEY)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", PROJECT_ID)
    monkeypatch.setattr(studio_api, "authenticate_studio_caller", lambda *_a, **_k: None)

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
    payload = build_storyboard_render_payload(
        {
            "project_id": PROJECT_ID,
            "job_id": JOB_ID,
            "screenplay_text": SCRIPT,
            "source_sha256": src,
            "manifest_sha256": compiled.manifest_sha256,
            "approval_id": approval_id,
            "principal_id": PRINCIPAL_ID,
            **PROFILE,
        }
    )
    result = asyncio.run(storyboard_render_job(payload))
    return {"workspace": workspace, "result": dict(result)}


def _client(
    monkeypatch: pytest.MonkeyPatch, rows: Mapping[str, Mapping[str, object]]
) -> TestClient:
    monkeypatch.setattr("nexus_ai_agent.api.app.get_creative_registry", lambda: FakeRegistry(rows))
    app = FastAPI()
    app.include_router(studio_api.router)
    return TestClient(app)


def _row(result: Mapping[str, object] | None, project: str = PROJECT_ID) -> dict[str, object]:
    """A registry row the way the queue leaves it after a verified completion."""
    row: dict[str, object] = {
        "input_data": {"project_id": project},
        "status": "completed",
        "canonical_status": "completed",
    }
    if result is not None:
        stored = dict(result)
        # The queue's independent verification block, as durably recorded for
        # a COMPLETED job (the download gate refuses anything without it).
        stored.setdefault(
            "artifact_verification",
            {
                "status": "verified",
                "reason_code": None,
                "job_type": "storyboard_render",
                "output_sha256": stored.get("output_sha256"),
                "passport_seal_verified": True,
                "media_verdict": "PASS",
            },
        )
        row["result"] = stored
    return row


def _result_dict(rendered: Mapping[str, object]) -> dict[str, object]:
    result = rendered.get("result")
    assert isinstance(result, Mapping)
    return dict(result)


def _url(job_id: str = JOB_ID) -> str:
    return f"/api/studio/projects/{PROJECT_ID}/jobs/{job_id}/artifact"


def test_authorized_download_streams_the_verified_bytes(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = rendered["result"]
    assert isinstance(result, dict)
    workspace = rendered["workspace"]
    assert isinstance(workspace, Path)
    mp4 = workspace / str(result["output_rel_path"])
    client = _client(monkeypatch, {JOB_ID: _row(result)})

    response = client.get(_url())

    assert response.status_code == 200
    assert response.content == mp4.read_bytes()
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(mp4.stat().st_size)
    assert response.headers["x-artifact-sha256"] == hashlib.sha256(mp4.read_bytes()).hexdigest()
    assert "attachment" in response.headers["content-disposition"]


def test_real_verified_descriptor_streams_and_background_closes_fd(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAL-DISK: exercise the route, passport verifier, stream, and descriptor lease."""
    import json
    import os

    from nexus_ai_agent.creative.artifact_passport import PASSPORT_SUFFIX

    result = rendered["result"]
    assert isinstance(result, dict)
    workspace = rendered["workspace"]
    assert isinstance(workspace, Path)
    mp4 = workspace / str(result["output_rel_path"])
    passport_path = mp4.with_name(f"{mp4.stem}{PASSPORT_SUFFIX}")
    passport = json.loads(passport_path.read_text(encoding="utf-8"))
    assert passport["passport_version"] == 2
    assert len(passport["hmac_sha256"]) == 64
    assert durable_approval_ledger_path(workspace).is_file()
    registry = FakeRegistry({JOB_ID: _row(result)})
    monkeypatch.setattr("nexus_ai_agent.api.app.get_creative_registry", lambda: registry)

    from nexus_ai_agent.creative.artifact_download import (
        AuthorizedDownload,
        _FdLease,
        authorize_artifact_download,
    )

    real_authorize = authorize_artifact_download
    captured: list[AuthorizedDownload] = []

    def capture_authorized(
        workspace_root: Path,
        rel_path: str,
        signing_key: str | bytes,
    ) -> AuthorizedDownload:
        opened = real_authorize(workspace_root, rel_path, signing_key)
        captured.append(opened)
        return opened

    monkeypatch.setattr(studio_api, "authorize_artifact_download", capture_authorized)
    response = asyncio.run(studio_api.download_studio_artifact(PROJECT_ID, JOB_ID))
    assert len(captured) == 1
    response_authorized = captured[0]
    assert isinstance(response_authorized, AuthorizedDownload)
    response_lease = response_authorized._lease
    assert isinstance(response_lease, _FdLease)
    fd = response_authorized.fd
    assert fd is not None
    original_fstat = os.fstat(fd)

    # /proc is an optional supplemental diagnostic only; the portable proof
    # below uses the descriptor exposed by the actual _FdLease and fstat/EBADF.
    proc_fd = Path("/proc/self/fd")
    if proc_fd.is_dir():
        proc_entry = proc_fd / str(fd)
        if proc_entry.is_symlink():
            assert proc_entry.resolve() == mp4.resolve()

    assert response.headers["x-artifact-sha256"] == hashlib.sha256(mp4.read_bytes()).hexdigest()

    # Consume exactly Content-Length bytes without requesting the EOF sentinel:
    # the descriptor remains open until the response's BackgroundTask runs.
    async def read_exact_response_body() -> bytes:
        remaining = int(response.headers["content-length"])
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunk_bytes = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
            chunks.append(chunk_bytes)
            remaining -= len(chunk_bytes)
            if remaining <= 0:
                break
        assert remaining == 0
        return b"".join(chunks)

    body = asyncio.run(read_exact_response_body())
    assert body == mp4.read_bytes()
    assert hashlib.sha256(body).hexdigest() == response.headers["x-artifact-sha256"]
    assert os.fstat(fd).st_ino == original_fstat.st_ino

    assert response.background is not None
    asyncio.run(response.background())
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    import errno

    assert closed.value.errno == errno.EBADF
    assert response_authorized.fd is None
    assert response_lease.fd is None


def test_real_symlinked_mp4_is_refused_by_the_download_route(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAL-DISK: the verifier refuses a symlink swapped onto the artifact path."""
    result = rendered["result"]
    assert isinstance(result, dict)
    workspace = rendered["workspace"]
    assert isinstance(workspace, Path)
    mp4 = workspace / str(result["output_rel_path"])
    original = mp4.with_name("original-real-artifact.mp4")
    mp4.rename(original)
    mp4.symlink_to(original.name)
    client = _client(monkeypatch, {JOB_ID: _row(result)})

    response = client.get(_url())

    assert response.status_code == 403
    assert response.json()["detail"] == "symlink_refused"


def test_another_projects_job_is_hidden_as_not_found(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, {JOB_ID: _row(_result_dict(rendered), project="someone-else")})

    response = client.get(_url())

    assert response.status_code == 404


def test_unknown_job_is_not_found(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, {})

    assert client.get(_url("no-such-job")).status_code == 404


def test_job_without_an_artifact_is_not_ready(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, {JOB_ID: _row(None)})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.json()["detail"] == "artifact_not_ready"


def test_bytes_changed_after_render_are_refused_with_409(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = rendered["result"]
    assert isinstance(result, dict)
    workspace = rendered["workspace"]
    assert isinstance(workspace, Path)
    mp4 = workspace / str(result["output_rel_path"])
    mp4.write_bytes(mp4.read_bytes() + b"tamper")
    client = _client(monkeypatch, {JOB_ID: _row(result)})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.content != mp4.read_bytes()


def test_traversal_is_refused_with_403(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result_dict(rendered)
    result["output_rel_path"] = "projects/../escape.mp4"
    client = _client(monkeypatch, {JOB_ID: _row(result)})

    response = client.get(_url())

    assert response.status_code == 403
    assert response.json()["detail"] == "path_traversal"


def test_malformed_path_is_refused_with_422(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result_dict(rendered)
    result["output_rel_path"] = "/etc/passwd"
    client = _client(monkeypatch, {JOB_ID: _row(result)})

    assert client.get(_url()).status_code == 422


def test_unconfigured_studio_fails_closed_with_503(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_WORKSPACE)
    client = _client(monkeypatch, {JOB_ID: _row(_result_dict(rendered))})

    assert client.get(_url()).status_code == 503


def test_a_job_that_is_not_completed_is_refused(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership of the project is not enough: only COMPLETED may serve bytes."""
    row = _row(_result_dict(rendered))
    row["status"] = "processing"
    row["canonical_status"] = "processing"
    client = _client(monkeypatch, {JOB_ID: row})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.json()["detail"] == "job_not_completed"


def test_a_completed_job_without_a_recorded_pass_verification_is_refused(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler's own "rendered" claim is never download authority."""
    row = _row(None)
    row["result"] = _result_dict(rendered)
    client = _client(monkeypatch, {JOB_ID: row})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.json()["detail"] == "artifact_not_verified"


def test_a_completed_job_whose_verification_records_a_non_pass_verdict_is_refused(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row(_result_dict(rendered))
    row_result = row.get("result")
    assert isinstance(row_result, Mapping)
    result = dict(row_result)
    verification = result.get("artifact_verification")
    assert isinstance(verification, Mapping)
    verification = dict(verification)
    verification["media_verdict"] = "VIOLATED"
    result["artifact_verification"] = verification
    row["result"] = result
    client = _client(monkeypatch, {JOB_ID: row})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.json()["detail"] == "artifact_not_verified"


def test_the_download_gate_accepts_an_absent_row_for_the_route_to_decide() -> None:
    """``None`` is the unknown-job spelling: the gate returns and the route answers 404."""
    studio_api._require_completed_and_verified(None)


def test_a_refusal_after_authorization_releases_the_verified_descriptor(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.artifact_download import AuthorizedDownload, DownloadRefused

    closed: list[bool] = []
    original_close = AuthorizedDownload.close

    def _spy_close(self: AuthorizedDownload) -> None:
        closed.append(True)
        original_close(self)

    def _refuse_stream(_authorized: object) -> object:
        raise DownloadRefused(409, "passport_missing")

    monkeypatch.setattr(AuthorizedDownload, "close", _spy_close)
    monkeypatch.setattr(studio_api, "iter_authorized_chunks", _refuse_stream)
    client = _client(monkeypatch, {JOB_ID: _row(_result_dict(rendered))})

    response = client.get(_url())

    assert response.status_code == 409
    assert response.json()["detail"] == "passport_missing"
    assert closed == [True], "the verified descriptor must be released on refusal"


def test_an_unexpected_failure_after_authorization_releases_the_descriptor(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nexus_ai_agent.creative.artifact_download import AuthorizedDownload

    closed: list[bool] = []
    original_close = AuthorizedDownload.close

    def _spy_close(self: AuthorizedDownload) -> None:
        closed.append(True)
        original_close(self)

    def _crash_stream(_authorized: object) -> object:
        raise RuntimeError("stream construction failed")

    monkeypatch.setattr(AuthorizedDownload, "close", _spy_close)
    monkeypatch.setattr(studio_api, "iter_authorized_chunks", _crash_stream)
    monkeypatch.setattr(
        "nexus_ai_agent.api.app.get_creative_registry",
        lambda: FakeRegistry({JOB_ID: _row(_result_dict(rendered))}),
    )
    app = FastAPI()
    app.include_router(studio_api.router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(_url())

    assert response.status_code == 500
    assert closed == [True], "nothing may outlive a failed request"


def test_a_failure_before_authorization_has_no_descriptor_to_release(
    rendered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected failure before a descriptor exists returns 500 and closes nothing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nexus_ai_agent.creative.artifact_download import AuthorizedDownload

    closed: list[bool] = []
    original_close = AuthorizedDownload.close

    def _spy_close(self: AuthorizedDownload) -> None:
        closed.append(True)
        original_close(self)

    def _fail_before_authorization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("authorization backend unavailable")

    monkeypatch.setattr(AuthorizedDownload, "close", _spy_close)
    monkeypatch.setattr(studio_api, "authorize_artifact_download", _fail_before_authorization)
    monkeypatch.setattr(
        "nexus_ai_agent.api.app.get_creative_registry",
        lambda: FakeRegistry({JOB_ID: _row(_result_dict(rendered))}),
    )
    app = FastAPI()
    app.include_router(studio_api.router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(_url())

    assert response.status_code == 500
    assert closed == [], "no descriptor was ever opened, so none is closed"
