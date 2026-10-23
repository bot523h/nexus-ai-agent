"""Fitness functions for the knowledge-retrieval boundaries (task-273).

Pure-AST checks (no production imports), matching the house style of
``test_import_boundaries.py``.  The laws under test:

* LlamaIndex is confined to the adapter module — the core knowledge domain
  never imports it (narrow port/protocol, MODULE_MAP R5-style extension).
* The ``knowledge_retrieval`` package never imports ``adapters`` (nor the
  LlamaIndex framework): wiring happens only in the composition root
  (``cli.py``).
* The CLI composition root is the single place that constructs the adapter,
  and it is the only caller wiring adapter → service.
* Retrieval never touches orchestration/authority: no LangGraph, no
  command-bus imports on either side of the boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"
PACKAGE = SRC / "knowledge_retrieval"
ADAPTER = SRC / "adapters" / "llamaindex_retrieval.py"
CLI = SRC / "cli.py"

#: Every file in the repository that may name the LlamaIndex framework.
LLAMA_INDEX_ALLOWLIST = frozenset(
    {
        "src/nexus_ai_agent/adapters/llamaindex_retrieval.py",
        # Test modules exercise/observe the adapter by contract.
        "tests/unit/test_knowledge_retrieval_identity.py",
        "tests/unit/test_knowledge_retrieval_service.py",
        "tests/unit/test_knowledge_retrieval_cli.py",
        "tests/integration/test_knowledge_retrieval_pipeline.py",
        # Docs gate may scan words, but code files must be listed above.
    }
)


def _imports(path: Path) -> set[str]:
    """Top-level names of every import in ``path`` (both import styles)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_llama_index_imports_exist_only_in_the_adapter() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        if relative in LLAMA_INDEX_ALLOWLIST:
            continue
        if any(name == "llama_index" or name.startswith("llama_index.") for name in _imports(path)):
            violations.append(relative)
    assert violations == [], f"llama_index imported outside the adapter: {violations}"


def test_core_package_never_imports_adapters_or_llama_index() -> None:
    for path in PACKAGE.rglob("*.py"):
        imported = _imports(path)
        assert "llama_index" not in {name.split(".")[0] for name in imported} or not any(
            name == "llama_index" or name.startswith("llama_index.") for name in imported
        ), path
        assert not any(name.startswith("nexus_ai_agent.adapters") for name in imported), (
            f"core package imports an adapter: {path}"
        )


def test_adapter_is_imported_only_by_the_composition_root() -> None:
    """``cli.py`` wires settings → adapter → service; nothing else does."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path in {ADAPTER, CLI}:
            continue  # the adapter itself and the composition root
        for name in _imports(path):
            if name.startswith("nexus_ai_agent.adapters.llamaindex_retrieval"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"adapter imported outside the composition root: {offenders}"


def test_retrieval_never_touches_orchestration_or_authority() -> None:
    """Knowledge retrieval must not reach LangGraph or the command bus."""
    forbidden_fragments = ("langgraph", "command_bus", "orchestrat")
    for path in PACKAGE.rglob("*.py"):
        imported = " ".join(sorted(_imports(path)))
        for fragment in forbidden_fragments:
            assert fragment not in imported, f"{path} imports {fragment!r}"


def test_composition_root_is_a_cli_file_that_builds_the_stack() -> None:
    """Guard against the wiring silently moving or disappearing."""
    assert CLI.is_file()
    cli_text = CLI.read_text(encoding="utf-8")
    assert "LlamaIndexKnowledgeIndex" in cli_text
    assert "KnowledgeConfig.from_settings" in cli_text
    assert "KnowledgeRetrievalService(" in cli_text
    # The service package itself must not contain a builder (composition law).
    package_text = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.rglob("*.py"))
    assert "LlamaIndexKnowledgeIndex" not in package_text
