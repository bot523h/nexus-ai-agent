"""Serve the standalone Studio product surface at ``/studio/app``.

The product frame says a capability must be reachable without Telegram. The
Studio API (``/api/studio/...``) has existed for a while; this module adds the
human surface that exercises it, as a self-contained static bundle with no build
step and no framework.

It is deliberately separate from ``web/static`` (the PWA shell): that directory
is owned by the in-flight Studio client work, and a second writer there would
produce a merge conflict rather than a product. When the PWA shell grows the
same three panels, delete this mount — the bundle is one directory.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

SURFACE_DIR = Path(__file__).resolve().parents[1] / "web" / "studio_surface"
SURFACE_MOUNT = "/studio/app"

#: Files the surface needs; a missing one is a packaging bug, not a 404.
REQUIRED_FILES = ("index.html", "studio.css", "studio.js")


def install_studio_surface(app: FastAPI) -> None:
    """Mount the static bundle; fail loudly when the wheel omitted it."""
    missing = [name for name in REQUIRED_FILES if not (SURFACE_DIR / name).is_file()]
    if missing:
        raise RuntimeError(f"studio surface bundle is incomplete: {', '.join(missing)}")
    app.mount(SURFACE_MOUNT, StaticFiles(directory=SURFACE_DIR, html=True), name="studio-surface")


__all__ = ["REQUIRED_FILES", "SURFACE_DIR", "SURFACE_MOUNT", "install_studio_surface"]
