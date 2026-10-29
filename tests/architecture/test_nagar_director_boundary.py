from __future__ import annotations

import ast
from pathlib import Path

from nexus_ai_agent.nagar.director import DirectorOutcome

DIRECTOR_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "nagar" / "director"
)
FORBIDDEN_IMPORT_PREFIXES = (
    "subprocess",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "sqlite3",
    "sqlalchemy",
    "sqlmodel",
    "httpx",
    "requests",
    "nexus_ai_agent.adapters",
    "nexus_ai_agent.application.ports.job_queue",
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.nagar.cognition.bridge",
    "nexus_ai_agent.provenance",
)
FORBIDDEN_SYMBOLS = {
    "CapabilityRegistry",
    "CommandBus",
    "ExecutionPolicy",
    "ProjectAuthorizer",
    "TypedCommand",
    "command_id",
    "dispatch",
    "enqueue",
    "execute",
    "open",
    "proposal_to_command",
    "render",
    "run",
    "shell",
    "verify",
    "write_bytes",
    "write_text",
}
FORBIDDEN_OUTCOME_FIELDS = {
    "actor",
    "capability_snapshot",
    "command_id",
    "confirmed",
    "execution_policy",
    "permissions",
    "queue_id",
    "shell",
}


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return modules


def _referenced_symbols(tree: ast.AST) -> set[str]:
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
    return symbols


def test_director_source_has_no_execution_or_persistence_boundary() -> None:
    source_files = sorted(DIRECTOR_ROOT.glob("*.py"))
    assert source_files
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        imports = _imported_modules(tree)
        assert not any(
            imported == prefix or imported.startswith(prefix + ".")
            for imported in imports
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        ), source_file
        assert not (FORBIDDEN_SYMBOLS & _referenced_symbols(tree)), source_file


def test_director_outcome_has_no_command_or_authority_fields() -> None:
    assert not (FORBIDDEN_OUTCOME_FIELDS & set(DirectorOutcome.model_fields))
    assert DirectorOutcome.model_fields["dispatchable"].default is False
    assert DirectorOutcome.model_fields["execution_authority"].default == "none"
    assert not hasattr(DirectorOutcome, "dispatch")
