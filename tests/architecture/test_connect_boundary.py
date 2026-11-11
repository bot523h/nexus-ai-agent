"""CONNECT contracts must not become a second execution authority."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "src" / "nexus_ai_agent" / "connect"
FORBIDDEN_TOP_LEVEL = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "sqlmodel",
    "telegram",
    "httpx",
    "requests",
    "boto3",
    "redis",
    "kafka",
    "celery",
    "temporal",
    "torch",
    "transformers",
}
FORBIDDEN_NEXUS_PREFIXES = (
    "nexus_ai_agent.api",
    "nexus_ai_agent.storage",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.provenance",
    "nexus_ai_agent.adapters.in_process_job_queue",
)


def files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def imports(path: Path) -> tuple[set[str], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top: set[str] = set()
    nexus: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top.update(alias.name.split(".")[0] for alias in node.names)
            nexus.extend(
                alias.name for alias in node.names if alias.name.startswith("nexus_ai_agent")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
            if node.module.startswith("nexus_ai_agent"):
                nexus.append(node.module)
    return top, nexus


def test_package_exists_and_has_no_forbidden_framework_or_store_imports():
    assert files()
    for path in files():
        top, nexus = imports(path)
        assert not top & FORBIDDEN_TOP_LEVEL, (path, sorted(top & FORBIDDEN_TOP_LEVEL))
        assert not any(module.startswith(FORBIDDEN_NEXUS_PREFIXES) for module in nexus), (
            path,
            nexus,
        )


def test_package_has_no_dynamic_import_or_process_execution():
    for path in files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"__import__", "eval", "exec"}
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"system", "popen", "run", "Popen"}
