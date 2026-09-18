"""Frozen import-boundary fitness tests for Stage 0."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
BASELINE = ROOT / "tests/architecture/legacy_baseline.json"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_approved_baseline_is_present_and_frozen() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["approval"] == "ARCH_BASELINE_APPROVED"
    assert baseline["baseline_version"] == 1
    assert baseline["files"]
    assert all((ROOT / path).is_file() for path in baseline["files"])


def test_domain_and_ports_respect_baseline() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    forbidden = set(baseline["forbidden_imports"])
    for relative in baseline["files"]:
        imported = _imports(ROOT / relative)
        assert not imported & forbidden, f"forbidden import in {relative}"


def test_new_boundary_files_do_not_import_adapters() -> None:
    paths = [
        *(ROOT / "src/nexus_ai_agent/domain").rglob("*.py"),
        *(ROOT / "src/nexus_ai_agent/application/ports").rglob("*.py"),
    ]
    for path in paths:
        assert "adapters" not in _imports(path), path


#: The sanctioned LangGraph runtime boundary (DoD): the composition root and
#: ``adapters/langgraph`` may import the runtime framework by design; the
#: frozen legacy baseline governs everything else.
LANGGRAPH_RUNTIME_BOUNDARY = (
    "src/nexus_ai_agent/storage/langgraph_checkpoint.py",
    "src/nexus_ai_agent/adapters/langgraph/",
)


def test_global_legacy_baseline_has_no_new_violations() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    expected = {(item["file"], tuple(item["imports"])) for item in baseline["legacy_violations"]}
    actual: set[tuple[str, tuple[str, ...]]] = set()
    for path in (ROOT / "src").rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        if relative.startswith(LANGGRAPH_RUNTIME_BOUNDARY):
            continue
        imports = tuple(sorted(_imports(path) & {"langgraph", "sqlmodel", "telegram"}))
        if imports:
            actual.add((relative, imports))
    assert actual <= expected, f"new legacy boundary violations: {sorted(actual - expected)}"
