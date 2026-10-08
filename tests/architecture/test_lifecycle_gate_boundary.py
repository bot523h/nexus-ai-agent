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


# ---------------------------------------------------------------------------
# task-183 trust boundary: the EXPERIMENTAL opt-in is server policy
# ---------------------------------------------------------------------------

RENDER_JOBS = SRC / "creative/render_jobs.py"
OPT_IN_POLICY = "EXPERIMENTAL_OPT_IN_OPERATIONS"


def test_no_production_code_reads_an_opt_in_off_an_object() -> None:
    """``<anything>.allow_experimental`` would mean the opt-in is carried by a
    data structure (a queue row, a request, a command) -- i.e. by the caller.
    The only legal holders are the bus's private ``_allow_experimental`` and
    plain parameters of the lifecycle gate."""
    readers = sorted(
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in SRC.rglob("*.py")
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Attribute) and node.attr == "allow_experimental"
    )
    assert readers == []


def test_bus_opt_in_is_only_ever_derived_from_the_server_policy() -> None:
    """Every production ``CommandBus(..., allow_experimental=...)`` lives in the
    render worker and is computed from ``EXPERIMENTAL_OPT_IN_OPERATIONS`` --
    never a literal, a parameter, or a payload value."""
    sites: list[tuple[str, ast.expr]] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == BUS:
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CommandBus"
            ):
                sites.extend(
                    (str(path.relative_to(ROOT)), kw.value)
                    for kw in node.keywords
                    if kw.arg == "allow_experimental" or kw.arg is None
                )
    assert [site for site, _ in sites] == [str(RENDER_JOBS.relative_to(ROOT))]
    (_, value) = sites[0]
    names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    assert OPT_IN_POLICY in names
    assert not any(isinstance(n, ast.Constant) and n.value is True for n in ast.walk(value))
