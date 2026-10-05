"""Architecture test forbidding deprecated datetime.utcnow() across shipped source."""

from __future__ import annotations

import ast
from pathlib import Path


def _check_ast_for_utcnow(tree: ast.AST) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "utcnow":
            if isinstance(node.value, ast.Name) and node.value.id == "datetime":
                violations.append((node.lineno, "datetime.utcnow"))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "datetime":
                for alias in node.names:
                    if alias.name == "utcnow":
                        violations.append((node.lineno, "from datetime import utcnow"))
    return violations


def test_no_datetime_utcnow_in_src() -> None:
    src_dir = Path(__file__).parents[2] / "src"
    all_violations: list[str] = []
    for path in src_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        violations = _check_ast_for_utcnow(tree)
        for lineno, usage in violations:
            rel = path.relative_to(src_dir.parent)
            all_violations.append(f"{rel}:{lineno}: {usage}")

    msg = "Found deprecated datetime.utcnow usage:\n" + "\n".join(all_violations)
    assert not all_violations, msg
