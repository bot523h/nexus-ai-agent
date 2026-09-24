"""Gate 2 (D-0013): the real AI -> command -> bus -> handler boundary.

Structural pins for the reconciled canonical contract: a single envelope,
a single registry, a single bus-owned handler invocation edge, and an
authorization seam that points inward only. Behavioural gate order and
denial-before-handler are tested in
``tests/unit/test_command_capability_contract.py``.

These guards inspect the production edges already in this repository. They do
not claim generic tools/shells elsewhere in the agent are Nagar operations,
and they deliberately do NOT fence the runtime-owned call sites
(``creative/slideshow/*``, ``creative/render_jobs.py``): binding explicit
service grants there is the runtime owner's follow-up (next_work task-181).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CREATIVE = ROOT / "src/nexus_ai_agent/creative"
STUDIO = CREATIVE / "studio"
BUS = STUDIO / "bus.py"
AI_AREAS = (
    ROOT / "src/nexus_ai_agent/agents",
    ROOT / "src/nexus_ai_agent/agent",
    ROOT / "src/nexus_ai_agent/orchestration",
    ROOT / "src/nexus_ai_agent/llm",
)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_only_command_bus_invokes_an_operation_handler() -> None:
    """No adapter, AI module or pack may call a registered handler directly."""
    direct: list[str] = []
    for path in sorted(CREATIVE.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "handler"
            ):
                direct.append(str(path.relative_to(ROOT)))
    assert direct == [str(BUS.relative_to(ROOT))]


def test_ai_layers_cannot_import_nagar_executors_or_pack_handlers() -> None:
    forbidden = (
        "nexus_ai_agent.creative.rendering",
        "nexus_ai_agent.creative.slideshow.ffmpeg",
        "nexus_ai_agent.creative.ffmpeg_executor",
        "nexus_ai_agent.creative.packs.",
    )
    for directory in AI_AREAS:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            for module in _imports(_tree(path)):
                assert not module.startswith(forbidden), (
                    f"{path.relative_to(ROOT)} imports a creative executor/pack directly: {module}"
                )


def test_studio_core_cannot_execute_shell_media_or_ui_automation() -> None:
    """Narrow structural supplement to test_nagar_studio_isolation.py."""
    forbidden = {
        "subprocess",
        "os",
        "shutil",
        "socket",
        "telegram",
        "fastapi",
        "playwright",
        "selenium",
        "nexus_ai_agent.tools.system_shell",
        "nexus_ai_agent.creative.rendering",
    }
    for path in sorted(STUDIO.rglob("*.py")):
        imports = _imports(_tree(path))
        assert not imports & forbidden, f"{path.relative_to(ROOT)} imports {imports & forbidden}"


def test_authorization_seam_points_inward_only() -> None:
    """The bus consumes the seam; the seam depends only on studio models."""
    bus_imports = _imports(_tree(BUS))
    assert "nexus_ai_agent.creative.studio.authorization" in bus_imports
    seam_imports = _imports(_tree(STUDIO / "authorization.py"))
    nexus_imports = {m for m in seam_imports if m.startswith("nexus_ai_agent")}
    assert nexus_imports == {"nexus_ai_agent.creative.studio.models"}, nexus_imports


def test_single_canonical_envelope_and_protocol() -> None:
    """One envelope (studio TypedCommand), one protocol id, no v2 protocol."""
    from nexus_ai_agent.creative.studio.models import (
        COMMAND_SCHEMA_VERSION,
        PROTOCOL_VERSION,
        TypedCommand,
    )

    assert PROTOCOL_VERSION == "nagar.command.v1"
    assert COMMAND_SCHEMA_VERSION == 2
    assert "schema_version" in TypedCommand.model_fields
    assert "protocol_version" in TypedCommand.model_fields

    envelope_definitions: list[str] = []
    v2_protocol_hits: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in {
                "CommandEnvelope",
                "TypedCommand",
            }:
                envelope_definitions.append(f"{path.relative_to(ROOT)}::{node.name}")
        if "nagar.command.v2" in path.read_text(encoding="utf-8"):
            v2_protocol_hits.append(str(path.relative_to(ROOT)))
    assert envelope_definitions == ["src/nexus_ai_agent/creative/studio/models.py::TypedCommand"], (
        envelope_definitions
    )
    assert v2_protocol_hits == [], (
        "a nagar.command.v2 protocol id must never enter src/ "
        f"(reconciliation decision D-0013): {v2_protocol_hits}"
    )
