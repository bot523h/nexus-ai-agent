"""Legacy creative HTTP pipeline — frozen boundary (task-165, P0-A ratchet).

Context (ADR 0006): ``POST /creative/video-edit`` + ``GET /creative/jobs/{id}``
and their support modules (``creative/job_registry.py``,
``creative/video_director.py``, ``creative/ffmpeg_executor.py``) are the
*legacy* media lane. The canonical path is the Telegram creative surface →
durable job queue → packs runtime registry → render lane. These tests make the
boundary load-bearing instead of aspirational:

1. the exact set of ``/creative*`` routes in ``api/app.py`` is frozen — nobody
   may grow the legacy surface;
2. no *new* importer of the legacy support modules may appear anywhere in
   ``src/`` (whitelisted importers only);
3. both legacy routes are behind the fail-closed HMAC gate — a route can
   never silently lose its auth by an edit in between.

Nothing here forbids *removing* legacy code (the ADR's end state): when the
routes are deleted, update this file to assert their absence instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "nexus_ai_agent"
API_APP = SRC / "api" / "app.py"

#: The complete, frozen legacy route surface. Anything else under /creative
#: is a regression against ADR 0006.
LEGACY_ROUTES: dict[tuple[str, str], str] = {
    ("post", "/creative/video-edit"): "create_video_edit_job",
    ("get", "/creative/jobs/{job_id}"): "get_job_status",
}

#: Modules that implement the legacy lane; new importers are forbidden.
LEGACY_MODULES = (
    "nexus_ai_agent.creative.job_registry",
    "nexus_ai_agent.creative.video_director",
    "nexus_ai_agent.creative.ffmpeg_executor",
)

#: Import sites that may reference legacy modules today (verified on main).
ALLOWED_IMPORTERS = {
    "api/app.py",  # the routes themselves
    "creative/__init__.py",  # package re-exports (removal tracked by the ADR)
    "creative/ffmpeg_executor.py",  # imports video_director.VideoEditPlan
    "creative/job_registry.py",
    "creative/video_director.py",
}

_AUTH_HELPER = "require_hmac_signature"


def _decorator_route(dec: ast.expr) -> tuple[str, str] | None:
    """Return (method, path) for ``@app.<method>("<path>")`` decorators."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id != "app" or not dec.args or not isinstance(dec.args[0], ast.Constant):
        return None
    return func.attr.lower(), str(dec.args[0].value)


def _route_table() -> dict[tuple[str, str], str]:
    tree = ast.parse(API_APP.read_text(encoding="utf-8"))
    routes: dict[tuple[str, str], str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            route = _decorator_route(dec)
            if route is not None:
                routes[route] = node.name
    return routes


def test_legacy_creative_route_set_is_frozen() -> None:
    actual = {key: name for key, name in _route_table().items() if key[1].startswith("/creative")}
    assert actual == LEGACY_ROUTES, (
        "the legacy /creative surface must match ADR 0006 exactly; "
        f"unexpected delta: {set(actual) ^ set(LEGACY_ROUTES)}"
    )


def test_no_new_importers_of_legacy_support_modules() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED_IMPORTERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in LEGACY_MODULES:
                        offenders.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module in LEGACY_MODULES:
                offenders.append(f"{rel}: from {node.module} import …")
    assert offenders == [], "new legacy-pipeline importers (ADR 0006):\n" + "\n".join(offenders)


def test_legacy_routes_call_the_fail_closed_hmac_gate() -> None:
    """Every legacy handler body must call ``require_hmac_signature`` directly."""
    tree = ast.parse(API_APP.read_text(encoding="utf-8"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    for (_, _), func_name in LEGACY_ROUTES.items():
        func = next(n for n in tree.body if isinstance(n, kinds) and n.name == func_name)
        calls = {
            node.func.id
            for node in ast.walk(func)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert _AUTH_HELPER in calls, (
            f"{func_name} no longer calls {_AUTH_HELPER} — the legacy route "
            "must stay fail-closed until removal (ADR 0006)"
        )
