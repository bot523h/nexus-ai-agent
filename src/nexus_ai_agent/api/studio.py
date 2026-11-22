"""Studio API surface: one read route and one submission route.

Authorization is performed before the job reader is called. Project scope is a
deployment policy (``NEXUS_STUDIO_PROJECT_IDS``), not a request claim, and the
existing dashboard bearer-token gate authenticates the caller.

Wave 6 adds the missing half: ``POST …/renders`` admits a render into the
*existing* durable queue. It is gated twice — bearer/HttpOnly session (who the
caller is) and the HMAC-signed envelope over the raw body (that this exact body
was signed within the freshness window) — and it owns no execution: the
``creative_render`` worker runs the canonical chain.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from nexus_ai_agent.api.studio_session import authenticate_studio_caller
from nexus_ai_agent.creative.artifact_download import (
    AuthorizedDownload,
    DownloadRefused,
    artifact_rel_path_for_job,
    authorize_artifact_download,
    iter_authorized_chunks,
)
from nexus_ai_agent.creative.storyboard_jobs import StoryboardJobConfigError, studio_settings
from nexus_ai_agent.product.studio_read import ProjectReadDenied, StudioReadService
from nexus_ai_agent.product.studio_submit import (
    StudioRenderSubmission,
    StudioRenderSubmissionService,
    StudioSubmissionRefused,
    resolve_studio_facts_from_env,
)

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

router = APIRouter(prefix="/api/studio", tags=["studio"])


def _configured_projects() -> frozenset[str]:
    return frozenset(
        value.strip()
        for value in os.getenv("NEXUS_STUDIO_PROJECT_IDS", "").split(",")
        if value.strip()
    )


def require_studio_project(
    project_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Authenticate the existing dashboard principal and enforce project scope."""
    # Reuse the existing caller authentication (bearer header, or the HttpOnly browser
    # session that is derived from the same token); do not create a second token system.
    authenticate_studio_caller(request, authorization)
    if not _PROJECT_ID.fullmatch(project_id):
        raise HTTPException(status_code=400, detail="invalid project id")
    if project_id not in _configured_projects():
        # No project existence oracle: both unconfigured and unauthorized projects
        # are indistinguishable to the caller.
        raise HTTPException(status_code=404, detail="studio project not found")


@router.get(
    "/projects/{project_id}/jobs/{job_id}/state",
    dependencies=[Depends(require_studio_project)],
)
async def get_studio_job_state(project_id: str, job_id: str) -> dict[str, Any]:
    """Return only durable read facts for one authorized project job."""
    if not job_id or len(job_id) > 128 or "/" in job_id or ".." in job_id:
        raise HTTPException(status_code=400, detail="invalid job id")
    from nexus_ai_agent.api.app import get_creative_registry

    try:
        view = await StudioReadService(get_creative_registry()).read_job(
            project_id=project_id, job_id=job_id
        )
    except ProjectReadDenied:
        # Hide cross-project job existence and never return its row.
        raise HTTPException(status_code=404, detail="studio state not found") from None
    return view.to_dict()


def get_studio_submission_service() -> StudioRenderSubmissionService:
    """Compose the submission service over the single job authority.

    The queue is the one the read route observes (``get_creative_registry``), so
    a submitted job is immediately readable through ``…/state``. Canonical facts
    come from deployment configuration; an unconfigured deployment gets a service
    that refuses every submission with a typed ``identity_unavailable``.
    """
    from nexus_ai_agent.api.app import get_creative_registry
    from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore

    registry = get_creative_registry()
    return StudioRenderSubmissionService(
        registry.queue,
        resolve_studio_facts_from_env(),
        revision_store=SqlProjectRevisionStore(),
    )


def _require_completed_and_verified(row: Mapping[str, object] | None) -> None:
    """Fail-closed download gate: a COMPLETED row with a recorded PASS verdict.

    Project ownership alone is not enough: a job that is still running, was
    reopened or failed must never serve bytes, and neither may a row whose
    durable result lacks the queue's independent ``artifact_verification``
    PASS block — a handler's own "rendered" claim is never download authority.
    """
    if row is None:
        return  # the unknown/foreign-project spelling is decided below (404)
    status = str(row.get("canonical_status") or row.get("status") or "")
    if status.lower() not in {"completed", "done"}:
        raise DownloadRefused(409, "job_not_completed")
    result = row.get("result")
    block = result.get("artifact_verification") if isinstance(result, Mapping) else None
    if not isinstance(block, Mapping):
        raise DownloadRefused(409, "artifact_not_verified")
    media_verdict = block.get("media_verdict")
    # The single verification gate: the queue's independent block must record
    # ``PASS`` — a ``media_verdict`` when it carries one, otherwise the block's
    # own ``verified`` status. Anything else (VIOLATED, UNOBSERVED, missing)
    # is refused: an unverified artifact is never downloadable.
    if (media_verdict is not None and media_verdict != "PASS") or (
        media_verdict is None and block.get("status") != "verified"
    ):
        raise DownloadRefused(409, "artifact_not_verified")


@router.get(
    "/projects/{project_id}/jobs/{job_id}/artifact",
    dependencies=[Depends(require_studio_project)],
)
async def download_studio_artifact(project_id: str, job_id: str) -> StreamingResponse:
    """Stream the verified MP4 of one authorized project job.

    Passport first, bytes second: the artifact is authorized (passport seal,
    PASS verdict, digest of the bytes on disk) before a single byte is sent. A
    job of another project, or an unknown job, is a ``404``. The row must
    moreover be ``COMPLETED`` with the queue's independent verification block
    recording ``PASS`` — anything else is a ``409`` (``job_not_completed`` /
    ``artifact_not_verified`` / ``artifact_not_ready``). Status codes come
    from the authorization gate: ``403`` traversal/symlink/outside workspace,
    ``404`` no artifact, ``409`` passport missing or stale, ``422`` malformed path.
    """
    if not job_id or len(job_id) > 128 or "/" in job_id or ".." in job_id:
        raise HTTPException(status_code=400, detail="invalid job id")
    from nexus_ai_agent.api.app import get_creative_registry

    authorized: AuthorizedDownload | None = None
    try:
        row = await get_creative_registry().get_job(job_id)
        rel = artifact_rel_path_for_job(row, project_id)
        _require_completed_and_verified(row)
        workspace, signing_key = studio_settings(os.environ)
        authorized = authorize_artifact_download(workspace, rel, signing_key)
        # The stream reads from the descriptor that was verified — the same
        # inode and bytes (BytesVerified == BytesStreamed), never a second
        # path-based open. Ownership of the descriptor moves into the stream
        # AT CALL TIME (it is a plain iterator, not a generator).
        stream = iter_authorized_chunks(authorized)
    except DownloadRefused as exc:
        if authorized is not None:
            authorized.close()  # never leak the verified descriptor on refusal
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None
    except StoryboardJobConfigError:
        if authorized is not None:  # pragma: no cover - refused before opening
            authorized.close()
        raise HTTPException(status_code=503, detail="studio not configured") from None
    except BaseException:
        if authorized is not None:
            authorized.close()  # belt and braces: nothing may outlive the request
        raise
    return StreamingResponse(
        stream,
        media_type="video/mp4",
        background=BackgroundTask(stream.close),
        headers={
            "Content-Length": str(authorized.size_bytes),
            "Content-Disposition": 'attachment; filename="storyboard.mp4"',
            "X-Artifact-SHA256": authorized.sha256,
        },
    )


@router.post("/projects/{project_id}/renders", dependencies=[Depends(require_studio_project)])
async def submit_studio_render(project_id: str, request: Request) -> JSONResponse:
    """Admit one signed render into the canonical durable queue.

    Gates, in order (each one refuses closed):

    1. bearer/HttpOnly-session authentication + project scope (dependency);
    2. HMAC signed envelope over the raw body (``X-NEXUS-Timestamp`` /
       ``X-NEXUS-Signature``), disabled outright without a configured key;
    3. typed body (``extra="forbid"``) — a hand-crafted field never reaches the
       canonical handoff;
    4. the canonical handoff itself (``build_creative_payload``), which validates
       the trim window and refuses anything it cannot admit.
    """
    from nexus_ai_agent.api.app import require_hmac_signature

    # The signature covers the raw bytes, so the gate reads the body first and the
    # JSON parse below reuses that same buffer (starlette caches it).
    await require_hmac_signature(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a typed 400, never a 500
        raise HTTPException(status_code=400, detail="invalid_request: body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_request: body must be an object")
    try:
        submission = StudioRenderSubmission.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_request: {exc}") from None

    service = get_studio_submission_service()
    try:
        result = await service.submit(project_id=project_id, submission=submission)
    except StudioSubmissionRefused as exc:
        status = 503 if exc.reason == "identity_unavailable" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return JSONResponse(result, status_code=202)


__all__ = [
    "get_studio_job_state",
    "get_studio_submission_service",
    "require_studio_project",
    "router",
    "submit_studio_render",
]
