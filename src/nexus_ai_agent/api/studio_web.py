"""Same-origin delivery of the NAGAR Studio web app (web/README.md).

The app is a dependency-free set of static files (``nexus_ai_agent/web/static``)
served by the existing FastAPI application. This module owns only *delivery*:

* an explicit allow-list of assets (no directory serving, no path from the request
  ever reaches the filesystem),
* a strict Content-Security-Policy and hardening headers on every app response,
* ``Cache-Control: no-store`` on every ``/api/studio`` response (a browser or proxy
  must never keep job state, plans or evidence),
* ``GET /api/studio/overview``: the one place that states, in code, which product
  capabilities are real and which are deferred, so the UI cannot drift into
  presenting an unsupported capability as working.

It introduces no execution path, queue, store or authority.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from nexus_ai_agent.api import studio_session
from nexus_ai_agent.api.studio_session import require_studio_caller

_PKG_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = _PKG_ROOT / "web" / "static"
#: The already-shipped OFL Vazirmatn face (caption pack). Served, never copied, so
#: the repository keeps a single copy of the binary.
FONT_PATH = _PKG_ROOT / "creative" / "packs" / "caption" / "fonts" / "Vazirmatn.ttf"

_BUILD_PLACEHOLDER = b"__BUILD_ID__"

#: Every script/style/font/image/worker/manifest must be same-origin; nothing inline.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_APP_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class _Asset:
    media_type: str
    #: path relative to ``STATIC_DIR`` (or absolute for the shared font)
    source: Path


#: URL name (under ``/studio/``) -> file. This dict *is* the public surface.
_ASSETS: dict[str, _Asset] = {
    "index.html": _Asset("text/html; charset=utf-8", STATIC_DIR / "index.html"),
    "app.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "app.js"),
    "api.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "api.js"),
    "dom.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "dom.js"),
    "views.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "views.js"),
    "i18n.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "i18n.js"),
    "style.css": _Asset("text/css; charset=utf-8", STATIC_DIR / "style.css"),
    "sw.js": _Asset("text/javascript; charset=utf-8", STATIC_DIR / "sw.js"),
    "manifest.webmanifest": _Asset(
        "application/manifest+json; charset=utf-8", STATIC_DIR / "manifest.webmanifest"
    ),
    "icon.svg": _Asset("image/svg+xml", STATIC_DIR / "icon.svg"),
    "icon-192.png": _Asset("image/png", STATIC_DIR / "icon-192.png"),
    "icon-512.png": _Asset("image/png", STATIC_DIR / "icon-512.png"),
    "fonts/vazirmatn.ttf": _Asset("font/ttf", FONT_PATH),
}

_build_id: str | None = None


def build_id() -> str:
    """Stable id of the shipped bytes; changes when any asset changes (SW cache key)."""
    global _build_id
    if _build_id is None:
        digest = hashlib.sha256()
        for name in sorted(_ASSETS):
            if name == "sw.js":
                continue  # the worker embeds this id
            digest.update(name.encode())
            digest.update(_ASSETS[name].source.read_bytes())
        _build_id = digest.hexdigest()[:16]
    return _build_id


def _body(name: str) -> bytes:
    data = _ASSETS[name].source.read_bytes()
    if name == "sw.js":
        data = data.replace(_BUILD_PLACEHOLDER, build_id().encode())
    return data


def _app_response(name: str, request: Request) -> Response:
    asset = _ASSETS[name]
    data = _body(name)
    etag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
    headers = dict(_APP_HEADERS)
    headers["ETag"] = etag
    # Revalidate every time: assets are tiny and a stale shell is worse than a 304.
    headers["Cache-Control"] = "no-cache"
    if name == "sw.js":
        headers["Service-Worker-Allowed"] = "/studio/"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=asset.media_type, headers=headers)


# --- capability truth ---------------------------------------------------------------
#: Vocabulary matches the repository's truth matrix. ``reason`` is shown to the user
#: verbatim; ``evidence`` names where the claim can be checked.
CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "id": "session",
        "status": "VERIFIED",
        "reason": "HttpOnly session derived from the existing dashboard gate.",
        "evidence": "tests/unit/test_studio_session.py",
    },
    {
        "id": "job_state",
        "status": "VERIFIED",
        "reason": "Durable job state, plan, artifact passport, evidence and lineage gaps "
        "read through StudioReadService.",
        "evidence": "tests/unit/test_studio_read_api.py",
    },
    {
        "id": "project_scope",
        "status": "VERIFIED_WITH_LIMITATIONS",
        "reason": "Projects are a deployment policy (NEXUS_STUDIO_PROJECT_IDS), not per-user "
        "membership.",
        "evidence": "src/nexus_ai_agent/api/studio.py",
    },
    {
        "id": "user_identity",
        "status": "BLOCKED",
        "reason": "AppPrincipal/AppChannelBinding exist in storage but no HTTP login binds to "
        "them. Every browser session is the single operator credential.",
        "evidence": "src/nexus_ai_agent/storage/models.py",
    },
    {
        "id": "job_listing",
        "status": "DEFERRED",
        "reason": "No list/query contract exists: JobRegistry only exposes get_job(job_id).",
        "evidence": "src/nexus_ai_agent/creative/job_registry.py",
    },
    {
        "id": "intent_submit",
        "status": "DEFERRED",
        "reason": "No preview/approval session API: IBPC sessions and approvals are "
        "in-process (Stage 3 of NAGAR-ONE).",
        "evidence": "docs/engineering/NAGAR_ONE_TRUTH_MATRIX.md",
    },
    {
        "id": "approvals",
        "status": "DEFERRED",
        "reason": "Approval persistence and cross-process single-use are not durable yet.",
        "evidence": "docs/engineering/NAGAR_ONE_TRUTH_MATRIX.md",
    },
    {
        "id": "asset_library",
        "status": "DEFERRED",
        "reason": "The Asset Library has no HTTP surface; the app cannot browse or ingest assets.",
        "evidence": "src/nexus_ai_agent/creative/assets/",
    },
    {
        "id": "artifact_download",
        "status": "DEFERRED",
        "reason": "No authorized artifact download route; the app shows the passport "
        "(hash, size, verification), not the bytes.",
        "evidence": "src/nexus_ai_agent/product/studio_experience.py",
    },
)

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def configured_projects() -> list[str]:
    ids = {
        value.strip()
        for value in os.getenv("NEXUS_STUDIO_PROJECT_IDS", "").split(",")
        if value.strip() and _PROJECT_ID.fullmatch(value.strip())
    }
    return sorted(ids)


api_router = APIRouter(prefix="/api/studio", tags=["studio-app"])


@api_router.get("/overview", dependencies=[Depends(require_studio_caller)])
async def overview() -> dict[str, object]:
    """Projects this deployment exposes plus the honest capability table."""
    return {
        "projects": configured_projects(),
        "capabilities": list(CAPABILITIES),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "build": build_id(),
    }


web_router = APIRouter(tags=["studio-app"], include_in_schema=False)


@web_router.get("/studio")
async def studio_redirect() -> RedirectResponse:
    # Relative module URLs only resolve correctly under a trailing slash.
    return RedirectResponse("/studio/", status_code=307, headers=dict(_APP_HEADERS))


@web_router.get("/studio/")
async def studio_index(request: Request) -> Response:
    return _app_response("index.html", request)


@web_router.get("/studio/{asset_path:path}")
async def studio_asset(asset_path: str, request: Request) -> Response:
    # Lookup by exact key in a fixed dict: the request value is never joined to a path.
    if asset_path not in _ASSETS or asset_path == "index.html":
        return Response(status_code=404, headers=dict(_APP_HEADERS))
    return _app_response(asset_path, request)


def install_studio_web(app: FastAPI) -> None:
    """Mount the session routes, overview route, static app and API cache policy."""
    app.include_router(studio_session.router)
    app.include_router(api_router)
    app.include_router(web_router)

    @app.middleware("http")
    async def _studio_api_no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/studio"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = [
    "CAPABILITIES",
    "CONTENT_SECURITY_POLICY",
    "FONT_PATH",
    "STATIC_DIR",
    "build_id",
    "configured_projects",
    "install_studio_web",
]
