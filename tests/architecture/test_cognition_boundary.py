"""Architecture guards — Cognition Boundary (Gate B)."""

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
    assert not _calls_named("GeminiEngine", tree)


def test_app_builds_cognition_at_composition_root() -> None:
    text = (SRC / "bot" / "app.py").read_text(encoding="utf-8")
    assert "build_cognition" in text
    assert 'engines["cognition"]' in text
    assert "cognition_surface" in text


def test_app_not_truncated() -> None:
    text = (SRC / "bot" / "app.py").read_text(encoding="utf-8")
    assert "PLACEHOLDER" not in text
    assert "def build_application" in text
    assert "class WebhookApplicationAdapter" in text
    assert len(text) > 10_000


def test_cognition_never_imports_command_bus() -> None:
    for path in (SRC / "cognition").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            s = line.strip()
            if s.startswith("from ") or s.startswith("import "):
                assert "CommandBus" not in s
                assert "command_bus" not in s


def test_composition_exports_build_cognition() -> None:
    text = (SRC / "cognition" / "composition.py").read_text(encoding="utf-8")
    assert "def build_cognition" in text
