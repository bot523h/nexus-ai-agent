"""Boundary gates for the Living Core's constitutional zone (roadmap Phase 1).

* ``selfhood/`` is a pure core: stdlib + ``provenance.models`` + itself only;
* the Self-Ledger exposes no UPDATE/DELETE/DROP path (append-only);
* the charter amendment door (``selfhood.amend``) is imported by nothing in
  ``src/`` — learners and agent code have no code path that edits the charter.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/nexus_ai_agent"
SELF = SRC / "selfhood"

ALLOWED_TOP_LEVEL = {
    "__future__",
    "dataclasses",
    "enum",
    "json",
    "logging",
    "sqlite3",
    "threading",
    "datetime",
    "types",
    "pathlib",
    "typing",
    "collections",
    "hashlib",
    "contextlib",
    "nexus_ai_agent",
}
ALLOWED_INTERNAL_PREFIXES = ("nexus_ai_agent.selfhood", "nexus_ai_agent.provenance.models")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
            names.extend(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_selfhood_stays_inside_its_allowlist() -> None:
    files = sorted(SELF.rglob("*.py"))
    assert files, "expected the selfhood package to exist"
    for path in files:
        for name in _imports(path):
            assert name.split(".")[0] in ALLOWED_TOP_LEVEL, f"{path.name} imports {name}"
            if name.startswith("nexus_ai_agent"):
                assert name.startswith(ALLOWED_INTERNAL_PREFIXES), f"{path.name} imports {name}"


def test_ledger_has_no_mutating_sql_besides_insert() -> None:
    text = (SELF / "ledger.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(UPDATE|DELETE|DROP|ALTER|REPLACE)\b", text), (
        "the Self-Ledger is append-only: no UPDATE/DELETE/DROP/ALTER/REPLACE"
    )


def test_nothing_in_src_can_reach_the_amendment_door() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if SELF in path.parents:
            continue
        for name in _imports(path):
            if name.startswith("nexus_ai_agent.selfhood.amend"):
                offenders.append(f"{path.relative_to(ROOT)} -> {name}")
    assert not offenders, "charter amendment must stay human-only:\n" + "\n".join(offenders)
