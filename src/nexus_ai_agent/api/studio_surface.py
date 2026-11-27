"""Serve the standalone Studio product surface at ``/studio/app``.

The product frame says a capability must be reachable without Telegram. The
Studio API (``/api/studio/...``) has existed for a while; this module adds the
human surface that exercises it, as a self-contained static bundle with no build
step and no framework.

It is deliberately separate from ``web/static`` (the PWA shell): that directory
is owned by the in-flight Studio client work, and a second writer there would
produce a merge conflict rather than a product. When the PWA shell grows the
same panels, delete this module and its directory.

Delivery follows the shell's rule: one literal route per allow-listed asset, no
directory serving and no request-derived path, so the API-gate inventory can
enumerate every operation this module exposes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

SURFACE_DIR = Path(__file__).resolve().parents[1] / "web" / "studio_surface"
SURFACE_MOUNT = "/studio/app"

#: Files the surface needs; a missing one is a packaging bug, not a 404.
REQUIRED_FILES = ("index.html", "studio.css", "studio.js")

_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "studio.css": "text/css; charset=utf-8",
    "studio.js": "text/javascript; charset=utf-8",
}

_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}

router = APIRouter(tags=["studio-surface"])


def _asset_response(name: str) -> Response:
    return FileResponse(
        SURFACE_DIR / name,
        media_type=_CONTENT_TYPES[name],
        headers=dict(_HEADERS),
    )


@router.get(SURFACE_MOUNT)
async def studio_surface_redirect() -> Response:
    # Relative asset URLs only resolve under the trailing slash.
    return RedirectResponse(f"{SURFACE_MOUNT}/", status_code=307, headers=dict(_HEADERS))


@router.get(f"{SURFACE_MOUNT}/")
async def studio_surface_index(request: Request) -> Response:
    return _asset_response("index.html")


def _register_asset_route(name: str) -> None:
    async def _serve(request: Request) -> Response:
        return _asset_response(name)

    router.add_api_route(
        f"{SURFACE_MOUNT}/{name}", _serve, methods=["GET"], name=f"studio_surface_asset_{name}"
    )


for _asset in REQUIRED_FILES:
    if _asset != "index.html":
        _register_asset_route(_asset)


#: Every operation this bundle serves; the allow-list *is* the route table.
PUBLIC_SURFACE_PATHS: tuple[str, ...] = (
    SURFACE_MOUNT,
    f"{SURFACE_MOUNT}/",
    *(f"{SURFACE_MOUNT}/{name}" for name in REQUIRED_FILES if name != "index.html"),
)


def install_studio_surface(app: FastAPI) -> None:
    """Register the surface routes; fail loudly when the wheel omitted a file."""
    missing = [name for name in REQUIRED_FILES if not (SURFACE_DIR / name).is_file()]
    if missing:
        raise RuntimeError(f"studio surface bundle is incomplete: {', '.join(missing)}")
    app.include_router(router)


__all__ = [
    "PUBLIC_SURFACE_PATHS",
    "REQUIRED_FILES",
    "SURFACE_DIR",
    "SURFACE_MOUNT",
    "install_studio_surface",
]
