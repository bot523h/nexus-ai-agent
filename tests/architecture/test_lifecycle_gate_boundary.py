"""Structural pins for the Gate-2 x lifecycle seam (task-183).

Behavioural order lives in ``tests/unit/test_gate2_lifecycle_seam.py``. These
guards keep the seam *singular*: one gate call site, one dispatch path, and a
lifecycle module that stays a leaf (no pack/runtime imports, so it can never
become a second capability registry).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src/nexus_ai_agent"
STUDIO = SRC / "creative/studio"
BUS = STUDIO / "bus.py"
LIFECYCLE = STUDIO / "lifecycle.py"

#: The only production call sites allowed to construct the canonical bus.
BUS_CALL_SITES = {
    "src/nexus_ai_agent/creative/render_jobs.py",
    "src/nexus_ai_agent/creative/slideshow/service.py",
    "src/nexus_ai_agent/creative/slideshow/upscale.py",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_only_the_bus_invokes_the_pack_lifecycle_gate() -> None:
    callers: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "check_required_packs"
            ):
                callers.append(str(path.relative_to(ROOT)))
    assert callers == [str(BUS.relative_to(ROOT))]


def test_the_gate_is_called_exactly_once_in_the_pipeline() -> None:
    calls = [
        node
        for node in ast.walk(_tree(BUS))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check_required_packs"
    ]
    assert len(calls) == 1


def test_no_second_command_bus_construction_path() -> None:
    sites = set()
    for path in sorted(SRC.rglob("*.py")):
        if path == BUS:
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CommandBus"
            ):
                sites.add(str(path.relative_to(ROOT)))
    assert sites == BUS_CALL_SITES


def test_lifecycle_module_stays_a_leaf() -> None:
    """The lifecycle table must not import packs, runtime, storage or I/O."""
    forbidden = ("packs", "rendering", "storage", "jobs", "api", "bot")
    modules: set[str] = set()
    for node in ast.walk(_tree(LIFECYCLE)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    offenders = {
        module
        for module in modules
        if module.startswith("nexus_ai_agent")
        and any(f".{part}" in f".{module}." for part in forbidden)
    }
    assert offenders == set()


def test_allow_experimental_is_never_read_from_a_command_envelope() -> None:
    """The opt-in is composition-root state; no envelope field may carry it."""
    source = (STUDIO / "models.py").read_text(encoding="utf-8")
    assert "allow_experimental" not in source
