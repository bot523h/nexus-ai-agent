"""Architecture test forbidding deprecated datetime.utcnow()/utcfromtimestamp() in src/.

Supports:
- import datetime / import datetime as dt -> dt.datetime.utcnow() / dt.datetime.utcfromtimestamp()
- from datetime import datetime / from datetime import utcnow, utcfromtimestamp
- Aliases and attributes while ignoring unrelated object method calls (e.g. foo.utcnow()).
"""

from __future__ import annotations

import ast
from pathlib import Path


def check_ast_for_datetime_deprecations(tree: ast.AST) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []

    # Map aliases in file scope
    datetime_module_aliases: set[str] = set()
    datetime_class_aliases: set[str] = set()
    utcnow_imported_aliases: set[str] = set()
    utcfromtimestamp_imported_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    datetime_module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "datetime":
                for alias in node.names:
                    name = alias.name
                    as_name = alias.asname or name
                    if name == "datetime":
                        datetime_class_aliases.add(as_name)
                    elif name == "utcnow":
                        utcnow_imported_aliases.add(as_name)
                    elif name == "utcfromtimestamp":
                        utcfromtimestamp_imported_aliases.add(as_name)

    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 0)

        # Direct function calls: utcnow(...) or utcfromtimestamp(...) from direct import
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in utcnow_imported_aliases:
                    violations.append((lineno, f"{func.id}()"))
                elif func.id in utcfromtimestamp_imported_aliases:
                    violations.append((lineno, f"{func.id}()"))
            elif isinstance(func, ast.Attribute):
                # datetime.utcnow() or dt.datetime.utcnow() or datetime.utcfromtimestamp()
                attr = func.attr
                if attr in ("utcnow", "utcfromtimestamp"):
                    val = func.value
                    if isinstance(val, ast.Name):
                        # from datetime import datetime -> datetime.utcnow()
                        if val.id in datetime_class_aliases:
                            violations.append((lineno, f"{val.id}.{attr}()"))
                    elif isinstance(val, ast.Attribute):
                        # import datetime -> datetime.datetime.utcnow()
                        if val.attr == "datetime" and isinstance(val.value, ast.Name):
                            if val.value.id in datetime_module_aliases:
                                violations.append((lineno, f"{val.value.id}.datetime.{attr}()"))

    return sorted(list(set(violations)))


def test_no_datetime_utcnow_or_utcfromtimestamp_in_src() -> None:
    src_dir = Path(__file__).parents[2] / "src"
    all_violations: list[str] = []
    for path in src_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        violations = check_ast_for_datetime_deprecations(tree)
        for lineno, usage in violations:
            rel = path.relative_to(src_dir.parent)
            all_violations.append(f"{rel}:{lineno}: {usage}")

    msg = "Found deprecated datetime methods:\n" + "\n".join(all_violations)
    assert not all_violations, msg


def test_datetime_hygiene_ast_detector_positive_and_negative_cases() -> None:
    positive_cases = [
        "import datetime\nx = datetime.datetime.utcnow()",
        "import datetime as dt\nx = dt.datetime.utcnow()",
        "import datetime\nx = datetime.datetime.utcfromtimestamp(100)",
        "from datetime import datetime\nx = datetime.utcnow()",
        "from datetime import utcnow\nx = utcnow()",
        "from datetime import utcfromtimestamp as uft\nx = uft(100)",
    ]

    negative_cases = [
        "class Foo:\n  def utcnow(self): pass\nf = Foo()\nf.utcnow()",
        "some_obj.utcfromtimestamp(123)",
        "from datetime import datetime, timezone\nx = datetime.now(timezone.utc)",
    ]

    for code in positive_cases:
        tree = ast.parse(code)
        assert len(check_ast_for_datetime_deprecations(tree)) > 0, f"Failed to detect: {code}"

    for code in negative_cases:
        tree = ast.parse(code)
        assert len(check_ast_for_datetime_deprecations(tree)) == 0, f"False positive: {code}"
