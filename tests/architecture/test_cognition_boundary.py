"""Architecture guards — Cognition Boundary (Gate B recovery)."""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nexus_ai_agent"


def _calls_named(name: str, tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == name:
                return True
            if isinstance(f, ast.Attribute) and f.attr == name:
                return True
    return False


def test_handlers_do_not_construct_gemini_engine() -> None:
    path = SRC / "bot" / "handlers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert not _calls_named("GeminiEngine", tree), "handlers must not construct GeminiEngine"


def test_handlers_accept_injected_cognition() -> None:
    text = (SRC / "bot" / "handlers.py").read_text(encoding="utf-8")
    assert "cognition: Any | None = None" in text
    assert "gemini_engine: GeminiEngine | None = None" in text


def test_app_builds_cognition_at_composition_root() -> None:
    text = (SRC / "bot" / "app.py").read_text(encoding="utf-8")
    assert "build_cognition" in text
    assert 'engines["cognition"]' in text


def test_cognition_never_imports_command_bus() -> None:
    cog_dir = SRC / "cognition"
    for path in cog_dir.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "CommandBus" not in src
        assert "command_bus" not in src
