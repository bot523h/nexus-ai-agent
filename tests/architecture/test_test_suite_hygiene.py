"""Test-suite portability guards (task-183 CI root cause, commit 91fbaff).

``tests/`` is not an importable package (no ``__init__.py``; pytest runs with
the default ``rootdir``-relative module discovery). A test module doing
``from tests.unit.x import ...`` therefore imports only when the repository
root happens to be on ``sys.path`` -- true under ``python -m pytest``, false
under the bare ``pytest`` entry point CI uses. 91fbaff went red in CI with
``ModuleNotFoundError: No module named 'tests'`` (exit code 2, collection
error) while the ``python -m pytest`` run was green. Shared helpers belong in
a ``conftest.py`` fixture or in the module itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).parents[1]


def _imports_tests_package(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return node.level == 0 and (module == "tests" or module.startswith("tests."))
    if isinstance(node, ast.Import):
        return any(a.name == "tests" or a.name.startswith("tests.") for a in node.names)
    return False


def test_no_test_module_imports_the_tests_tree_as_a_package() -> None:
    offenders = sorted(
        f"{path.relative_to(TESTS.parent)}:{node.lineno}"
        for path in TESTS.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if _imports_tests_package(node)
    )
    assert offenders == []


def test_the_guard_recognises_the_91fbaff_shape() -> None:
    """Positive control: the exact import that broke CI is detected."""
    tree = ast.parse("from tests.unit.test_gate2_lifecycle_seam import command\n")
    assert any(_imports_tests_package(node) for node in ast.walk(tree))
    assert not any(
        _imports_tests_package(node)
        for node in ast.walk(ast.parse("from nexus_ai_agent.creative import render_jobs\n"))
    )
