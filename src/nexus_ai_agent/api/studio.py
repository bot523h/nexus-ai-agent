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
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from nexus_ai_agent.api.studio_session import authenticate_studio_caller
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
