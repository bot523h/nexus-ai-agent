"""Fitness function: every boundary law names a *live* enforcing test.

`AGENTS.md` §7 requires that "every boundary rule must name its enforcing test in
`docs/architecture/MODULE_MAP.md` §3". That rule was prose-only: the table was
maintained by hand, so a law could silently point at a renamed or deleted test
and the documentation contract would decay into decoration. This gate turns the
contract into an executable check, following the repository's
architecture-fitness-function practice (see `MODULE_MAP.md` §4) and the sibling
docs-as-code gate `tests/unit/test_docs_integrity.py`.

It parses §3, extracts every ``test_*.py`` / ``test_*.py::symbol`` reference from
each law row, resolves each file on disk (under ``tests/``) and each ``::symbol``
via AST, and fails naming the offending law. It is pure text/AST analysis — no
imports of production modules, no I/O beyond reading the two files.

A law may name a *set* of guards (R7, R9, R12, R13, R14 do); every named guard
must resolve. Rows are keyed by their ``R<n>`` id, so a law is never silently
dropped by a formatting change.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
TESTS = REPO_ROOT / "tests"
MODULE_MAP = REPO_ROOT / "docs" / "architecture" / "MODULE_MAP.md"

#: ``test_<name>.py`` optionally followed by a ``::symbol`` (a test function or class).
_TEST_REF = re.compile(r"test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)?")

#: The section that carries the law → enforcement table.
_SECTION_HEADING = "## 3. Boundary laws"

#: A table row whose first cell is a law id (``R1``, ``R1b``, ``R14`` …).
_LAW_ROW = re.compile(r"^\|\s*(R[0-9]+[a-z]?)\s*\|")


def _section_three() -> str:
    text = MODULE_MAP.read_text(encoding="utf-8")
    assert _SECTION_HEADING in text, (
        f"{MODULE_MAP.relative_to(REPO_ROOT)} no longer contains {_SECTION_HEADING!r}; "
        "this gate cannot verify the law table"
    )
    after = text.split(_SECTION_HEADING, 1)[1]
    return after.split("\n## ", 1)[0]


def _law_rows() -> list[tuple[str, str]]:
    """Return ``(law_id, row_text)`` for every boundary-law row in §3."""
    rows: list[tuple[str, str]] = []
    for line in _section_three().splitlines():
        match = _LAW_ROW.match(line)
        if match:
            rows.append((match.group(1), line))
    return rows


def _test_refs(row_text: str) -> list[str]:
    """Extract ``test_*.py[::symbol]`` references from the code spans of a row."""
    refs: list[str] = []
    for token in re.findall(r"`([^`]+)`", row_text):
        refs.extend(_TEST_REF.findall(token))
    return refs


def _resolve_file(ref: str) -> Path | None:
    name = ref.split("::", 1)[0]
    if "/" in name:
        candidate = REPO_ROOT / name
        return candidate if candidate.is_file() else None
    hits = sorted(TESTS.rglob(name))
    return hits[0] if hits else None


@cache
def _defined_symbols(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def test_the_law_table_is_not_empty() -> None:
    """Guard against a vacuous pass: a renamed heading must fail, not skip."""
    rows = _law_rows()
    assert len(rows) >= 10, f"parsed only {len(rows)} boundary laws from MODULE_MAP §3"
    assert any(law == "R1" for law, _ in rows), "the R1 law row was not parsed"


def test_every_law_names_at_least_one_test() -> None:
    offenders = [law for law, row in _law_rows() if not _test_refs(row)]
    assert not offenders, (
        "boundary laws with no enforcing test (AGENTS.md §7 requires one): " + ", ".join(offenders)
    )


def test_every_named_test_file_exists() -> None:
    offenders: list[str] = []
    for law, row in _law_rows():
        for ref in _test_refs(row):
            if _resolve_file(ref) is None:
                offenders.append(f"{law} -> {ref.split('::', 1)[0]} (no such file under tests/)")
    assert not offenders, "boundary laws naming a missing test file:\n" + "\n".join(offenders)


def test_every_named_test_symbol_exists() -> None:
    offenders: list[str] = []
    for law, row in _law_rows():
        for ref in _test_refs(row):
            if "::" not in ref:
                continue
            name, _, symbol = ref.partition("::")
            path = _resolve_file(name)
            if path is None:  # reported by test_every_named_test_file_exists
                continue
            if symbol not in _defined_symbols(path):
                offenders.append(f"{law} -> {ref} (symbol not defined in {name})")
    assert not offenders, "boundary laws naming a missing test symbol:\n" + "\n".join(offenders)


def test_the_gate_recognises_a_dangling_reference() -> None:
    """Positive control: the exact decay this gate prevents is detected."""
    row = "| R99 | a made-up law | `test_module_map_law_coverage.py::test_never_defined_zzz` |"
    refs = _test_refs(row)
    assert refs == ["test_module_map_law_coverage.py::test_never_defined_zzz"]
    name, _, symbol = refs[0].partition("::")
    path = _resolve_file(name)
    assert path is not None  # the file is real …
    assert symbol not in _defined_symbols(path)  # … but the symbol is dangling → detected

    missing = "| R98 | another law | `test_no_such_guard_9f3a.py` |"
    assert _resolve_file(_test_refs(missing)[0]) is None
