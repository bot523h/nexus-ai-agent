"""One production path for creative work — enforced, not documented.

Owner directive §3 lists the parallel routes that must not exist:

    Telegram → legacy FFmpeg          ✗
    Telegram → second renderer        ✗
    API → private renderer            ✗
    Bot → inline render               ✗
    Creative Surface → direct subprocess ✗

These tests read the tree (AST + text) and fail when a second entry appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"


def _tree(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text(encoding="utf-8"))


def _imported_names(relative: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(relative)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


# ---------------------------------------------------------------------------
# exactly one handler, in exactly one place
# ---------------------------------------------------------------------------


def test_worker_registers_the_creative_handler() -> None:
    from nexus_ai_agent.adapters.creative_render_job import creative_render_job
    from nexus_ai_agent.worker import default_job_handlers

    assert default_job_handlers()["creative_render"] is creative_render_job


def test_creative_render_has_exactly_one_definition() -> None:
    definitions: list[str] = []
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in {"creative_render_job", "creative_render"}:
                    definitions.append(f"{path.relative_to(SRC)}::{node.name}")
    assert definitions == ["adapters/creative_render_job.py::creative_render_job"], definitions


def test_only_the_canonical_lane_executes_media() -> None:
    """No second renderer: only the adapter may call the lane executor."""
    callers: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(SRC))
        if relative.startswith("creative/rendering/"):
            continue
        text = path.read_text(encoding="utf-8")
        if "render_lane(" in text or "encode_lane(" in text:
            callers.add(relative)
    assert callers == {"adapters/creative_render_job.py"}, callers


def test_surface_never_spawns_a_process_or_imports_a_renderer() -> None:
    text = (SRC / "bot" / "creative_surface.py").read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "os.system" not in text
    imports = _imported_names("bot/creative_surface.py")
    assert not [name for name in imports if "rendering" in name], imports
    assert not [name for name in imports if name.endswith("ffmpeg_executor")], imports


def test_api_does_not_render_creative_media_inline() -> None:
    imports = _imported_names("api/app.py")
    assert not [name for name in imports if "creative.rendering" in name], imports


# ---------------------------------------------------------------------------
# the surface is registered by the composition root, once
# ---------------------------------------------------------------------------


def test_composition_root_registers_the_surface_exactly_once() -> None:
    app_text = (SRC / "bot" / "app.py").read_text(encoding="utf-8")
    assert "build_creative_handlers(" in app_text
    assert app_text.count("build_creative_handlers(") == 1
    assert "CommandHandler" in app_text

    others = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "creative_surface.py"
        and "build_creative_handlers" in path.read_text(encoding="utf-8")
    ]
    assert others == ["bot/app.py"], others


def test_completion_notifier_routes_creative_jobs() -> None:
    app_text = (SRC / "bot" / "app.py").read_text(encoding="utf-8")
    assert "notify_creative_completion" in app_text
    assert 'job_type == "creative_render"' in app_text


def test_surface_and_notifier_stay_framework_free() -> None:
    """The Telegram client lives in the composition root, never in these two.

    Both modules are duck-typed on purpose (unit-testable without PTB) *and*
    they must not grow the frozen legacy import baseline
    (``tests/architecture/test_import_boundaries.py``): the framework imports
    belong to ``bot/app.py``, which already pays that debt.
    """
    for relative in ("bot/creative_surface.py", "bot/creative_notify.py"):
        imports = _imported_names(relative)
        leaked = sorted(
            name for name in imports if name.split(".")[0] in {"telegram", "langgraph", "sqlmodel"}
        )
        assert not leaked, f"{relative} imports the framework: {leaked}"


# ---------------------------------------------------------------------------
# the legacy lane stays guarded
# ---------------------------------------------------------------------------


def _route_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"route handler {name!r} disappeared")


def _decorator_keywords(function: ast.AST, attribute: str) -> dict[str, ast.expr]:
    for decorator in getattr(function, "decorator_list", []):
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == attribute
        ):
            return {keyword.arg: keyword.value for keyword in decorator.keywords if keyword.arg}
    return {}


def test_legacy_routes_are_deprecated_and_the_read_is_gated() -> None:
    tree = _tree("api/app.py")

    read_route = _route_function(tree, "get_job_status")
    assert "deprecated" in _decorator_keywords(read_route, "get"), "GET route lost deprecated=True"
    calls = {
        node.func.id
        for node in ast.walk(read_route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "require_hmac_signature" in calls, "the legacy read gate disappeared"

    write_route = _route_function(tree, "create_video_edit_job")
    assert "deprecated" in _decorator_keywords(write_route, "post")
    calls = {
        node.func.id
        for node in ast.walk(write_route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "require_hmac_signature" in calls


def test_every_http_client_in_the_api_is_ssrf_guarded() -> None:
    text = (SRC / "api" / "app.py").read_text(encoding="utf-8")
    clients = text.count("httpx.AsyncClient(")
    guarded = text.count("transport=SafeAsyncTransport()")
    assert clients == guarded == 1, (clients, guarded)
    assert "validate_url" in text


def test_the_ssrf_guard_is_the_repository_guard() -> None:
    """No second SSRF implementation: the guard is imported, never re-implemented."""
    imports = _imported_names("api/app.py")
    assert "nexus_ai_agent.core.ssrf_guard.validate_url" in imports
    assert "nexus_ai_agent.core.ssrf_guard.SafeAsyncTransport" in imports

    private_ranges = ("127.0.0.0/8", "10.0.0.0/8", "169.254.0.0/16", "fc00::/7")
    for path in (SRC / "api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for network in private_ranges:
            assert network not in text, f"{path.name} re-implements the SSRF ranges"


def test_private_ranges_live_in_the_canonical_guard_only() -> None:
    owners = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "169.254.0.0/16" in path.read_text(encoding="utf-8")
    ]
    assert owners == ["core/ssrf_guard.py"], owners
