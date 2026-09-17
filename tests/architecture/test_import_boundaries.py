"""Fitness tests for the contract-freeze architecture boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_domain_is_framework_free() -> None:
    forbidden = {"langgraph", "sqlmodel", "telegram"}
    domain = ROOT / "src/nexus_ai_agent/domain"
    imported = set().union(*(_imports(path) for path in domain.rglob("*.py")))
    assert not imported & forbidden


def test_ports_do_not_import_adapters() -> None:
    ports = ROOT / "src/nexus_ai_agent/application/ports"
    imported = set().union(*(_imports(path) for path in ports.rglob("*.py")))
    assert "adapters" not in imported
