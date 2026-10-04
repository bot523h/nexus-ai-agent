"""Authority and storage boundaries of the causal evidence layer.

These are fitness tests: they fail the build when the *shape* of the code
allows something the contract forbids, independent of whether a test exercises
it today.

1. **No execution authority.**  The causal package must not be able to run
   anything (no ``subprocess``/``os.system``/``eval``), must not import the
   render lane, the command bus, the compiler, an LLM, storage or an adapter.
   A witness that can execute is a second execution path.
2. **No second authority.**  The journal's SQL may only ever touch its own
   table, and it contains no ``UPDATE`` and no ``DELETE`` — the only write in
   the whole module is the ``INSERT`` of the next chain link.
3. **No bypass around the durable facts.**  The queue's observer call is
   optional and fail-safe; the durable job row stays the only authority over
   job state (``adapters.in_process_job_queue`` notices exceptions from the
   observer and never lets evidence failure re-open a decided transition).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
CAUSAL = ROOT / "src" / "nexus_ai_agent" / "causal"
PORT = ROOT / "src" / "nexus_ai_agent" / "application" / "ports" / "job_lifecycle_observer.py"

#: Top-level modules the causal layer may import (stdlib + pydantic + itself).
ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "contextlib",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "math",
    "pathlib",
    "sqlite3",
    "threading",
    "typing",
    "pydantic",
    "nexus_ai_agent",
}

#: Specific Nagar modules the causal layer must never reach for.
FORBIDDEN_MODULES = (
    "nexus_ai_agent.adapters",
    "nexus_ai_agent.creative",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.llm",
    "nexus_ai_agent.storage",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.api",
)

#: Execution/inspection primitives that would turn a witness into an actor.
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}


def _docstring_literals(tree: ast.Module) -> set[int]:
    """ids of the string constants that are docstrings, not data."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            value = body[0].value
            if isinstance(value.value, str):
                ids.add(id(value))
    return ids


def _looks_like_sql(text: str) -> bool:
    """A real statement mentions a FROM/INTO clause or creates/alters a table."""
    if "\n" in text and len(text) > 400:
        return False  # prose, not a statement
    upper = text.upper()
    if "CREATE TABLE" in upper or "CREATE INDEX" in upper:
        return True
    return (" FROM " in upper) or (" INTO " in upper)


SQL_VERB = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", re.IGNORECASE)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_causal_package_imports_only_permitted_modules() -> None:
    violations: list[str] = []
    for path in CAUSAL.rglob("*.py"):
        tree = _parse(path)
        foreign = _top_level_imports(tree) - ALLOWED_TOP_LEVEL
        if foreign:
            violations.append(f"{path.name}: {sorted(foreign)}")
        for module in _imported_modules(tree):
            if module.startswith(FORBIDDEN_MODULES):
                violations.append(f"{path.name}: forbidden import {module}")
    assert not violations, "causal layer reached outside its boundary:\n" + "\n".join(violations)


def test_causal_package_cannot_execute_processes() -> None:
    violations: list[str] = []
    for path in CAUSAL.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Attribute) and node.attr in {
                "system",
                "popen",
                "spawn",
                "execv",
                "execve",
                "run",
                "Popen",
            }:
                # ``run`` and friends are only a violation when they hang off
                # an execution module; the import test above already bans
                # subprocess/os, so any Attribute here is unexpected.
                if isinstance(node.value, ast.Name) and node.value.id in {"os", "subprocess"}:
                    violations.append(f"{path.name}: {node.value.id}.{node.attr}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
                violations.append(f"{path.name}: {node.id}()")
    assert not violations, "causal layer can execute:\n" + "\n".join(violations)


def test_journal_is_append_only_and_owns_one_table() -> None:
    source = (CAUSAL / "journal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    sql_texts = [text for text in _sql_texts(tree) if _looks_like_sql(text)]
    assert sql_texts, "expected the journal to contain SQL"
    for statement in sql_texts:
        assert "{TABLE_NAME}" in statement, f"SQL without the owned table: {statement!r}"
        verbs = {match.group(1).upper() for match in SQL_VERB.finditer(statement)}
        assert verbs <= {"SELECT", "INSERT", "CREATE"}, f"non-append-only SQL: {statement!r}"

    # Belt and braces: no raw UPDATE/DELETE anywhere in the module.
    assert not re.search(r"\bUPDATE\b", source), "the journal must never UPDATE"
    assert not re.search(r"\bDELETE\b", source), "the journal must never DELETE"


def _sql_texts(tree: ast.Module) -> list[str]:
    """Every string an SQL statement could be built from (f-strings included).

    ``f"SELECT ... FROM {TABLE_NAME}"`` is a ``JoinedStr`` whose literal parts
    are separate constants; rendering the placeholders back as ``{name}`` is
    what lets this test assert *which* table each statement names.
    """
    docstrings = _docstring_literals(tree)
    fragments: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            fragments.update(
                id(value)
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            rendered = ""
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    rendered += value.value
                elif isinstance(value, ast.FormattedValue):
                    if isinstance(value.value, ast.Name):
                        rendered += "{" + value.value.id + "}"
                    else:
                        rendered += "{expr}"
            texts.append(rendered)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in fragments
        ):
            texts.append(node.value)
    return texts


def test_observer_port_has_no_adapter_or_execution_dependency() -> None:
    tree = _parse(PORT)
    modules = _imported_modules(tree)
    assert not any(module.startswith("nexus_ai_agent.adapters") for module in modules)
    assert _top_level_imports(tree) <= {"__future__", "dataclasses", "typing"}


def test_queue_observer_is_optional_and_fail_safe() -> None:
    source = (ROOT / "src" / "nexus_ai_agent" / "adapters" / "in_process_job_queue.py").read_text(
        encoding="utf-8"
    )
    assert "causal_observer: JobLifecycleObserverPort | None = None" in source
    assert "noqa: BLE001 - evidence failure must not break the job" in source
    # The observer may never be consulted for scheduling or state: its only
    # call sites are the post-commit observation points.
    # Ten post-commit points: enqueue, reservation, rejected reservation,
    # execution (crash path), execution (result path), verification, terminal
    # (no verifier), terminal (verified), terminal (failure), claim-time failure.
    assert source.count("await self._observe(") == 10, source.count("await self._observe(")
