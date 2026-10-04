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
3. POST /creative/video-edit is an inert 410 response with no processing
   or executor call path, while the job-status GET stays HMAC-gated.

The POST route remains registered temporarily to provide an explicit 410;
its legacy executor module is retained but must have no production importer.
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


LEGACY_SHORT_NAMES = {"job_registry", "video_director", "ffmpeg_executor"}


def _check_imports_in_tree(tree: ast.AST, rel_path: str) -> list[str]:
    offenders: list[str] = []
    rel_parent_parts = Path(rel_path).parent.parts
    if rel_parent_parts == (".",):
        rel_parent_parts = ()
    file_pkg_parts = ["nexus_ai_agent"] + list(rel_parent_parts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in LEGACY_MODULES or alias.name in LEGACY_SHORT_NAMES:
                    offenders.append(f"{rel_path}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module in LEGACY_MODULES:
                    offenders.append(f"{rel_path}: from {node.module} import …")
                elif node.module == "nexus_ai_agent.creative":
                    for alias in node.names:
                        if alias.name in LEGACY_SHORT_NAMES:
                            offenders.append(f"{rel_path}: from {node.module} import {alias.name}")
            else:
                if node.level <= len(file_pkg_parts):
                    base_parts = file_pkg_parts[: len(file_pkg_parts) - (node.level - 1)]
                    if node.module:
                        mod_parts = base_parts + node.module.split(".")
                    else:
                        mod_parts = base_parts
                    resolved = ".".join(mod_parts)
                    if resolved in LEGACY_MODULES:
                        offenders.append(f"{rel_path}: relative import resolved to {resolved}")
                    else:
                        for alias in node.names:
                            is_legacy_alias = (
                                f"{resolved}.{alias.name}" in LEGACY_MODULES
                                or alias.name in LEGACY_SHORT_NAMES
                            )
                            if is_legacy_alias:
                                offenders.append(
                                    f"{rel_path}: relative import {alias.name} from {resolved}"
                                )
        elif isinstance(node, ast.Call):
            is_dynamic_import = False
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                is_dynamic_import = True
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ):
                is_dynamic_import = True

            if is_dynamic_import and node.args and isinstance(node.args[0], ast.Constant):
                target = str(node.args[0].value)
                is_legacy_target = any(m in target for m in LEGACY_MODULES) or any(
                    s in target for s in LEGACY_SHORT_NAMES
                )
                if is_legacy_target:
                    offenders.append(f"{rel_path}: dynamic import of {target}")

    return offenders


def test_no_new_importers_of_legacy_support_modules() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED_IMPORTERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        file_offenders = _check_imports_in_tree(tree, rel)
        offenders.extend(file_offenders)
    assert offenders == [], "new legacy-pipeline importers (ADR 0006):\n" + "\n".join(offenders)


def test_legacy_import_guard_catches_relative_package_and_dynamic_bypasses() -> None:
    """Prove that relative, package, and dynamic import bypasses are detected."""
    package_import = ast.parse("from nexus_ai_agent.creative import job_registry")
    assert len(_check_imports_in_tree(package_import, "bot/foo.py")) > 0

    relative_import = ast.parse("from . import ffmpeg_executor")
    assert len(_check_imports_in_tree(relative_import, "creative/foo.py")) > 0

    relative_from_import = ast.parse("from .video_director import VideoEditPlan")
    assert len(_check_imports_in_tree(relative_from_import, "creative/foo.py")) > 0

    dynamic_importlib = ast.parse("importlib.import_module('nexus_ai_agent.creative.job_registry')")
    assert len(_check_imports_in_tree(dynamic_importlib, "bot/foo.py")) > 0

    dynamic_builtin = ast.parse("__import__('nexus_ai_agent.creative.ffmpeg_executor')")
    assert len(_check_imports_in_tree(dynamic_builtin, "bot/foo.py")) > 0


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return next(node for node in tree.body if isinstance(node, kinds) and node.name == name)


def _direct_calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_video_edit_post_is_a_410_without_processing_or_auth_gates() -> None:
    tree = ast.parse(API_APP.read_text(encoding="utf-8"))
    handler = _function_node(tree, "create_video_edit_job")
    route = next(
        dec
        for dec in handler.decorator_list
        if isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "post"
    )
    assert any(
        keyword.arg == "status_code"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == 410
        for keyword in route.keywords
    )
    assert not (
        handler.args.posonlyargs
        or handler.args.args
        or handler.args.vararg
        or handler.args.kwonlyargs
        or handler.args.kwarg
    ), "the retired POST must not accept request data or processing dependencies"
    forbidden_calls = {
        "require_hmac_signature",
        "get_creative_registry",
        "_process_video_edit_job",
        "_save_upload_to_temp",
        "_download_video_to_temp",
        "execute_ffmpeg_commands",
        "analyze_video_with_gemini",
    }
    assert _direct_calls(handler).isdisjoint(forbidden_calls)
    assert "background_tasks" not in {arg.arg for arg in handler.args.args}


def test_job_status_get_remains_fail_closed_hmac_gated() -> None:
    tree = ast.parse(API_APP.read_text(encoding="utf-8"))
    handler = _function_node(tree, "get_job_status")
    assert _AUTH_HELPER in _direct_calls(handler)


def test_ffmpeg_executor_module_is_retained_but_not_imported_by_production() -> None:
    executor = SRC / "creative" / "ffmpeg_executor.py"
    assert executor.is_file(), "STOP-B retires the call path, not the executor module"
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == executor:
            continue
        text = path.read_text(encoding="utf-8")
        if "execute_ffmpeg_commands" in text or "creative.ffmpeg_executor" in text:
            offenders.append(path.relative_to(SRC).as_posix())
    assert offenders == [], "production executor call/import path remains: " + ", ".join(offenders)
