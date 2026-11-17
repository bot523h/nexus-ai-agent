"""The Studio web app is delivery + a session wrapper, never a second authority (web/README.md)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
WEB_PKG = ROOT / "src" / "nexus_ai_agent" / "web"
SESSION = ROOT / "src" / "nexus_ai_agent" / "api" / "studio_session.py"
DELIVERY = ROOT / "src" / "nexus_ai_agent" / "api" / "studio_web.py"
PACK_STATUS = ROOT / "src" / "nexus_ai_agent" / "api" / "studio_packs.py"

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


def test_the_pack_status_module_reads_storage_and_nothing_else() -> None:
    """The one module allowed to touch storage from the Studio surface.

    It exists so ``studio_web`` does not have to: the report is read-only, so the
    module may import ``nexus_ai_agent.storage.resource_packs`` and must never
    import the queue, the executor or an agent — reading a pack's integrity is
    not a licence to run anything.
    """
    imports = _imports(PACK_STATUS)
    assert "nexus_ai_agent.storage.resource_packs" in imports
    forbidden = (
        "nexus_ai_agent.jobs",
        "nexus_ai_agent.creative",
        "nexus_ai_agent.application",
        "nexus_ai_agent.agents",
        "nexus_ai_agent.bot",
        "nexus_ai_agent.llm",
    )
    bad = {module for module in imports if module.startswith(forbidden)}
    assert not bad, f"studio_packs imports {sorted(bad)}"
    text = PACK_STATUS.read_text(encoding="utf-8")
    for forbidden_text in ("subprocess", "os.system", "write_text", "write_bytes", "unlink"):
        assert forbidden_text not in text, forbidden_text


def test_delivery_module_never_writes_or_executes() -> None:
    text = DELIVERY.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "write_bytes", "write_text", "open("):
        assert forbidden not in text, forbidden
