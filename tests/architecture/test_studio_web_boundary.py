"""The Studio web app is delivery + a session wrapper, never a second authority (web/README.md)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
WEB_PKG = ROOT / "src" / "nexus_ai_agent" / "web"
SESSION = ROOT / "src" / "nexus_ai_agent" / "api" / "studio_session.py"
DELIVERY = ROOT / "src" / "nexus_ai_agent" / "api" / "studio_web.py"

#: Execution/authority modules the web layer must never import directly.
FORBIDDEN = (
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.creative",
    "nexus_ai_agent.storage",
    "nexus_ai_agent.application",
    "nexus_ai_agent.agents",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.llm",
)


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_web_package_contains_no_application_code() -> None:
    py = [p for p in WEB_PKG.rglob("*.py")]
    assert py == [WEB_PKG / "__init__.py"]
    assert not _imports(py[0]) - {"__future__"}


def test_session_and_delivery_modules_import_no_execution_or_storage_layer() -> None:
    for path in (SESSION, DELIVERY):
        bad = {m for m in _imports(path) if m.startswith(FORBIDDEN)}
        assert not bad, f"{path.name} imports {sorted(bad)}"


def test_session_module_adds_no_second_credential_or_store() -> None:
    text = SESSION.read_text(encoding="utf-8")
    assert "os.environ" not in text and "getenv" not in text  # no new secret/setting
    for forbidden in ("sqlite", "aiosqlite", "redis", "open(", "write_text", "pickle"):
        assert forbidden not in text, forbidden


def test_delivery_module_never_writes_or_executes() -> None:
    text = DELIVERY.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "write_bytes", "write_text", "open("):
        assert forbidden not in text, forbidden
