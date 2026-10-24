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
    """Fully-resolved dotted names of every import in ``path``.

    ``from package import submodule`` records ``package.submodule`` — the
    naive ``ast.ImportFrom.module``-only view would let
    ``from nexus_ai_agent import adapters`` or
    ``from nexus_ai_agent.adapters import llamaindex_retrieval`` slip past
    every boundary check below. Relative imports (``level > 0``) are resolved
    against the importing file's package first.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    def _package_of(file: Path) -> str:
        """Import package of ``file`` — relative to the *import* root.

        Production files resolve against ``SRC.parent`` (``…/src``), so a
        relative import inside ``src/nexus_ai_agent/…`` becomes
        ``nexus_ai_agent.…`` — the exact runtime dotted name the boundary
        checks test for. Deriving it from the repo ROOT instead would produce
        a phantom ``src.`` prefix and silently bypass every adapter/authority
        check (review finding). Files outside ``src`` (tests, fixtures) fall
        back to a repo-root-relative package; files outside the repo get none.
        """
        resolved = file.resolve()
        try:
            return ".".join(resolved.relative_to(SRC.parent).parts[:-1])
        except ValueError:
            pass
        try:
            return ".".join(resolved.relative_to(ROOT).parts[:-1])
        except ValueError:
            return ""

    def _record(module: str | None, alias_name: str | None) -> None:
        # Record BOTH the module itself (``from pkg import submodule`` must
        # expose ``pkg.submodule`` for module-level bans) and the joined
        # dotted name for each imported member.
        if module:
            names.add(module)
        if module and alias_name and alias_name != "*":
            names.add(f"{module}.{alias_name}")
        elif alias_name and not module:
            names.add(alias_name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package = _package_of(path)
                parts = package.split(".") if package else []
                # level=1 → current package; level=2 → one package up, etc.
                keep = max(len(parts) - (node.level - 1), 0)
                base = ".".join(parts[:keep])
                module = f"{base}.{module}".strip(".") if module else base
            for alias in node.names:
                if alias.name == "*":
                    names.add(module or "")
                else:
                    _record(module, alias.name)
    names.discard("")
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


#: Substrings that mark orchestration/authority modules (LangGraph runtime,
#: the command bus, orchestrator wiring) — checked on BOTH sides of the
#: boundary: the core package and the LlamaIndex adapter.
_AUTHORITY_FRAGMENTS = ("langgraph", "command_bus", "orchestrat")


def _authority_violation(imported: set[str]) -> set[str]:
    """Return the violating import names for a scanned module's imports."""
    return {name for name in imported if any(fragment in name for fragment in _AUTHORITY_FRAGMENTS)}


def test_retrieval_never_touches_orchestration_or_authority() -> None:
    """Knowledge retrieval must not reach LangGraph or the command bus.

    Applies to the core package *and* the adapter (the docstring's "both
    sides of the boundary" is now mechanically true).
    """
    scanned = [*PACKAGE.rglob("*.py"), ADAPTER]
    for path in scanned:
        violation = _authority_violation(_imports(path))
        assert not violation, f"{path} imports authority module(s) {sorted(violation)}"


def test_authority_check_rejects_a_prohibited_import_shape() -> None:
    """Self-test: the predicate actually flags orchestration/command-bus names.

    Guards the guard — if the fragments or the name matching weaken, this
    fails before any real violation can slip through (review finding: the
    check must be proven to reject the import it forbids).
    """
    # Prohibited shapes must be flagged (llama_index itself is governed by the
    # confinement tests above, not by this authority predicate):
    assert _authority_violation({"langgraph.checkpoint.postgres"})
    assert _authority_violation({"nexus_ai_agent.orchestration.graph"})
    assert _authority_violation({"nexus_ai_agent.creative.studio.command_bus"})
    assert _authority_violation({"some.module.command_bus"})
    assert not _authority_violation({"nexus_ai_agent.knowledge_retrieval.service"})
    assert not _authority_violation({"nexus_ai_agent.adapters.llamaindex_retrieval"})


def test_import_resolution_catches_from_package_import_submodule() -> None:
    """Self-test for the resolver: the R22 checks see ``from pkg import mod``.

    Review finding: recording only ``ast.ImportFrom.module`` lets
    ``from nexus_ai_agent import adapters`` and
    ``from nexus_ai_agent.adapters import llamaindex_retrieval`` evade both
    the adapter-ban and the single-importer checks.
    """
    import tempfile

    # The two shapes a module-only resolver cannot see, written to a real file
    # and parsed through the actual helper:
    sample_source = (
        "from nexus_ai_agent import adapters\n"
        "from nexus_ai_agent.adapters import llamaindex_retrieval\n"
        "from nexus_ai_agent.knowledge_retrieval import service\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "evasion_shapes.py"
        sample.write_text(sample_source, encoding="utf-8")
        resolved = _imports(sample)
    assert "nexus_ai_agent.adapters" in resolved
    assert "nexus_ai_agent.adapters.llamaindex_retrieval" in resolved
    assert "nexus_ai_agent.knowledge_retrieval.service" in resolved
    # The adapter file itself must surface its real package imports this way:
    adapter_imports = _imports(ADAPTER)
    assert any(name.startswith("nexus_ai_agent.") for name in adapter_imports)


def test_relative_imports_resolve_to_runtime_dotted_names() -> None:
    """Relative imports in production files must not gain a phantom ``src.`` prefix.

    Review finding: deriving the package from the repo root yields
    ``src.nexus_ai_agent…`` for ``from .x import y`` — a name no boundary
    check recognises, so a relative import of an adapter or authority module
    would sail past R22.
    """
    production_with_relative = SRC / "bot" / "handlers.py"
    assert production_with_relative.is_file()
    resolved = _imports(production_with_relative)
    assert "nexus_ai_agent.bot.middleware" in resolved
    assert not any(name.startswith("src.") for name in resolved)
    assert all(not name.startswith("src.") for name in resolved)


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
