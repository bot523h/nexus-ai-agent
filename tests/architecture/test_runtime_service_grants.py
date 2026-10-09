"""Architecture guards for the task-181 runtime authorization boundary."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
RUNTIME_FILES = (
    REPO_ROOT / "src/nexus_ai_agent/creative/slideshow/service.py",
    REPO_ROOT / "src/nexus_ai_agent/creative/slideshow/upscale.py",
    REPO_ROOT / "src/nexus_ai_agent/creative/render_jobs.py",
)


def _calls(source: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _keyword_names(call: ast.Call) -> set[str]:
    return {keyword.arg for keyword in call.keywords if keyword.arg is not None}


def test_direct_slideshow_buses_require_a_trusted_service_grant() -> None:
    for source in RUNTIME_FILES[:2]:
        calls = _calls(source, "CommandBus")
        assert calls, f"{source} must construct its canonical CommandBus"
        assert all("authorizer" in _keyword_names(call) for call in calls), (
            f"{source} must not reintroduce implicit local trust"
        )


def test_render_job_factory_requires_a_trusted_service_grant() -> None:
    calls = _calls(RUNTIME_FILES[2], "build_job_bus")
    assert calls, "render_jobs must use the canonical job bus factory"
    assert all("authorizer" in _keyword_names(call) for call in calls), (
        "render_jobs must pass an explicit service authorizer"
    )


def test_all_runtime_commands_declare_service_identity_and_owned_refs() -> None:
    for source in RUNTIME_FILES:
        text = source.read_text(encoding="utf-8")
        assert 'kind="service"' in text, f"{source} must bind a service principal"
        assert "InputRef" in text, f"{source} must declare project-bound source references"
        assert "input_refs" in text, f"{source} must carry owned source references"
