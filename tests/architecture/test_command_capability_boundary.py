"""Nagar Gate 2: the real AI → command → bus → handler boundary.

These guards inspect the production edges already in this repository. They do
not claim generic tools/shells elsewhere in the agent are Nagar operations.
Behavioural gate order and denial-before-handler are tested in the unit suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CREATIVE = ROOT / "src/nexus_ai_agent/creative"
BUS = CREATIVE / "studio/bus.py"
AI_AREAS = (
    ROOT / "src/nexus_ai_agent/agents",
    ROOT / "src/nexus_ai_agent/agent",
    ROOT / "src/nexus_ai_agent/orchestration",
    ROOT / "src/nexus_ai_agent/llm",
)
ENTRIES = (
    CREATIVE / "slideshow/service.py",
    CREATIVE / "slideshow/upscale.py",
    CREATIVE / "render_jobs.py",
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
        for path in sorted(directory.rglob("*.py")):
            for module in _imports(_tree(path)):
                assert not module.startswith(forbidden), (
                    f"{path.relative_to(ROOT)} imports a creative executor/pack directly: {module}"
                )


def test_existing_local_worker_and_cli_paths_bind_authority_to_the_bus() -> None:
    """An entry point dropping auth or calling a handler directly trips this guard."""
    for path in ENTRIES:
        tree = _tree(path)
        constructors = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CommandBus"
        ]
        assert constructors, f"{path.relative_to(ROOT)} must use the Nagar bus"
        assert all(any(kw.arg == "authorizer" for kw in call.keywords) for call in constructors), (
            f"{path.relative_to(ROOT)} creates a bus without a trusted project authorizer"
        )
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dispatch"
            for node in ast.walk(tree)
        ), f"{path.relative_to(ROOT)} creates a bus but does not dispatch a typed command"


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
    for path in sorted((CREATIVE / "studio").rglob("*.py")):
        imports = _imports(_tree(path))
        assert not imports & forbidden, f"{path.relative_to(ROOT)} imports {imports & forbidden}"
