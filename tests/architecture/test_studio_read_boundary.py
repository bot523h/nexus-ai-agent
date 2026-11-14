from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PRODUCT = ROOT / "src/nexus_ai_agent/product"
API_STUDIO = ROOT / "src/nexus_ai_agent/api/studio.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_product_read_surface_has_no_storage_or_execution_imports() -> None:
    imports = set().union(*(_imports(path) for path in PRODUCT.glob("*.py")))
    forbidden = {
        "nexus_ai_agent.storage",
        "nexus_ai_agent.creative.studio.bus",
        "nexus_ai_agent.creative.rendering.executor",
    }
    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )


def test_studio_api_exposes_exactly_one_read_and_one_submission_route() -> None:
    """The Studio API surface is closed, and every route is authenticated.

    Wave 6 added the submission half (``POST …/renders``): the surface is now
    exactly one read route and one submission route. The pinned properties are
    what make that safe — a third route, an unauthenticated submission, a
    submission without the signed envelope, or a widened product read port all
    turn this test red.
    """
    tree = ast.parse(API_STUDIO.read_text(encoding="utf-8"))
    routes: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "router"
            ):
                path = ""
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    path = str(decorator.args[0].value)
                routes.append((func.attr, path))
    assert sorted(routes) == [
        ("get", "/projects/{project_id}/jobs/{job_id}/state"),
        ("post", "/projects/{project_id}/renders"),
    ]

    submission = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "submit_studio_render"
    )
    decorated = [
        decorator
        for decorator in submission.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
    ]

    def _is_project_gate(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Depends"
            and bool(node.args)
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "require_studio_project"
        )

    def _carries_gate(decorator: ast.Call) -> bool:
        # Either ``@router.post(..., dependencies=[Depends(...)])`` or a plain
        # ``Depends(...)`` parameter; both express the same FastAPI gate.
        nodes: list[ast.AST] = list(decorator.args)
        for keyword in decorator.keywords:
            nodes.extend(ast.walk(keyword.value))
        return any(_is_project_gate(node) for node in nodes)

    assert any(_carries_gate(decorator) for decorator in decorated), (
        "the submission route must carry the same principal/project gate as the read route"
    )
    body = ast.get_source_segment(API_STUDIO.read_text(encoding="utf-8"), submission) or ""
    assert "await require_hmac_signature(request)" in body, (
        "the submission route must verify the signed envelope before it reads the body"
    )

    source = API_STUDIO.read_text(encoding="utf-8")
    assert "from nexus_ai_agent.product.studio_read import" in source
    assert "from nexus_ai_agent.api.studio_session import authenticate_studio_caller" in source
