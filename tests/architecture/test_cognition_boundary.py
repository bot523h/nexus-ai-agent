"""Architecture guards — Cognition Boundary (Gate B recovery)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nexus_ai_agent"


def test_cognition_package_exists() -> None:
    assert (SRC / "cognition" / "port.py").is_file()
    assert (SRC / "cognition" / "router.py").is_file()
    assert (SRC / "cognition" / "service.py").is_file()
    assert (SRC / "cognition" / "composition.py").is_file()


def test_cognition_never_imports_command_bus() -> None:
    for path in (SRC / "cognition").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "CommandBus" not in src
        assert "command_bus" not in src


def test_composition_exports_build_cognition() -> None:
    text = (SRC / "cognition" / "composition.py").read_text(encoding="utf-8")
    assert "def build_cognition" in text


def test_service_fail_closed_mentions_provider_unavailable() -> None:
    text = (SRC / "cognition" / "service.py").read_text(encoding="utf-8")
    assert "provider_unavailable" in text
    assert "timeout" in text
    assert "FALLBACK" in text
