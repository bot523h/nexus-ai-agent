"""Ratchet the optional cockpit into metadata + input-model validation only.

This package must not grow into a second command bus, runtime composition root,
settings dump, arbitrary shell, or production-health authority. In-product help
and module docstrings carry the same boundary; shared architecture docs remain
with their existing owners (task-197 coordination claim).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "src/nexus_ai_agent/cockpit"


def test_cockpit_only_depends_on_its_package_and_public_pack_introspection() -> None:
    allowed = ("nexus_ai_agent.cockpit", "nexus_ai_agent.creative.packs.runtime")
    forbidden = {"subprocess", "socket", "sqlite3", "httpx", "telegram", "langgraph", "sqlmodel"}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, (path, name)
                if name.startswith("nexus_ai_agent."):
                    assert name.startswith(allowed), (path, name)


def test_no_handler_bus_activation_eval_or_subprocess_execution() -> None:
    denied = {
        "handler",
        "dispatch",
        "execute",
        "activate",
        "activate_all",
        "enqueue",
        "eval",
        "exec",
        "compile",
        "Popen",
    }
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            assert name not in denied, (path, name)
            if name == "build_pack_runtime":
                activation = next(
                    keyword.value for keyword in node.keywords if keyword.arg == "activate"
                )
                assert isinstance(activation, ast.Constant) and activation.value is False


def test_browser_has_no_html_execution_sink_or_remote_runtime_dependency() -> None:
    script = (PACKAGE / "static/cockpit.js").read_text()
    html = (PACKAGE / "static/index.html").read_text()
    css = (PACKAGE / "static/cockpit.css").read_text()
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert sink not in script
    assert not re.search(r"(?:src|href)=[\"']https?://", html)
    assert not re.search(r"url\([\"']?https?://", css)
    assert "localhost" not in script and "127.0.0.1" not in script
    assert "sessionStorage" not in script
    # Saving browser preferences is an explicit, two-key allowlist, not a draft
    # store. Credentials and payloads must remain ephemeral.
    assert set(re.findall(r'savePreference\(\s*["\']([^"\']+)', script)) == {
        "nexus.cockpit.theme",
        "nexus.cockpit.favorites",
    }


def test_ui_discloses_input_only_verdict_and_never_exports_a_typed_command() -> None:
    script = (PACKAGE / "static/cockpit.js").read_text()
    assert '"nexus.cockpit.input-draft.v1"' in script
    assert '"input_schema_only"' in script
    assert "execution_authorized: false" in script
    assert "confirmed: true" not in script
    assert "nagar.command.v1" not in script
    assert "state.labRevision" in script  # stale in-flight verdicts cannot validate edited input
