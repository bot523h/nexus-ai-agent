from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "src" / "nexus_ai_agent" / "connect"
FORBIDDEN_TOP_LEVEL = {
    "fastapi",
    "starlette",
    "telegram",
    "httpx",
    "requests",
    "boto3",
    "redis",
    "kafka",
    "celery",
    "temporal",
    "flask",
    "subprocess",
    "socket",
}
FORBIDDEN_NEXUS_PREFIXES = (
    "nexus_ai_agent.api",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.provenance",
    "nexus_ai_agent.adapters.in_process_job_queue",
)


def _files():
    return sorted(PACKAGE.glob("*.py"))


def test_connect_persistence_has_no_second_engine_or_execution_authority():
    for path in _files():
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
        assert not top & FORBIDDEN_TOP_LEVEL, (path, top & FORBIDDEN_TOP_LEVEL)
        assert not any(module.startswith(FORBIDDEN_NEXUS_PREFIXES) for module in nexus), (
            path,
            nexus,
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"__import__", "eval", "exec"}
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "create_engine",
                    "create_async_engine",
                    "system",
                    "popen",
                }


def test_persistence_declares_no_orm_table_or_raw_schema():
    persistence = PACKAGE / "persistence.py"
    tree = ast.parse(persistence.read_text(encoding="utf-8"), filename=str(persistence))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"Table", "create_engine", "create_async_engine"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"create_all", "drop_all"}
