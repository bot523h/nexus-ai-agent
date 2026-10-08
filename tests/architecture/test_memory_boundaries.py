"""Architecture guard — memory/ is a leaf (wave-4 step4).

The memory layer must not import from the product surface (features/,
bot/) — it is a pure capability (store/search) with no Telegram or
feature-engine knowledge.  This keeps the hexagonal core testable and
prevents a hidden circular dependency (memory → features → memory).

The check is AST-based (no import execution) and fails fast with a
human-readable message naming the offending import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
MEMORY_ROOT = REPO_ROOT / "src" / "nexus_ai_agent" / "memory"

FORBIDDEN_PREFIXES = ("nexus_ai_agent.features", "nexus_ai_agent.bot", "features", "bot")


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
                for alias in node.names:
                    imports.append(f"{node.module}.{alias.name}")
    return imports


@pytest.mark.parametrize("path", list(MEMORY_ROOT.glob("*.py")))
def test_memory_module_has_no_forbidden_imports(path: Path) -> None:
    for imp in _imports_of(path):
        for prefix in FORBIDDEN_PREFIXES:
            assert not imp.startswith(prefix), (
                f"{path.name} imports forbidden '{imp}' (prefix '{prefix}') — "
                "memory/ must remain a leaf, not depend on features/ or bot/"
            )
