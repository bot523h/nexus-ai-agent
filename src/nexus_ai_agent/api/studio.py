"""Read-only Studio API surface.

Authorization is performed before the job reader is called. Project scope is a
deployment policy (``NEXUS_STUDIO_PROJECT_IDS``), not a request claim, and the
existing dashboard bearer-token gate authenticates the caller.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from nexus_ai_agent.api.dashboard import require_dashboard_token
from nexus_ai_agent.product.studio_read import ProjectReadDenied, StudioReadService

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
    authorization: str | None = Header(default=None),
) -> None:
    """Authenticate the existing dashboard principal and enforce project scope."""
    # Reuse the existing caller authentication; do not create a second token system.
    require_dashboard_token(authorization)
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


__all__ = ["get_studio_job_state", "require_studio_project", "router"]
