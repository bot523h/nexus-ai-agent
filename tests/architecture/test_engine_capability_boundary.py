"""Wave 5 boundary — the Studio core may name engines, never import them.

Two independent seams are pinned here:

1. **Static:** no module under ``creative/studio/`` imports an engine package, a
   pack, an adapter or a verifier. Engine ports are typed ``Any`` on
   ``OperationContext`` precisely so the core stays free of those edges.
2. **Declarative:** the engine capability layer and the render lane agree, id for
   id, on *which* capabilities exist and which engine answers each one. A new
   engine that is registered but not opted in (or vice versa) fails here rather
   than shipping a capability nobody can reach — or one nobody gated.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus_ai_agent.creative.render_jobs import (
    ENGINE_PORT_OPERATIONS,
    ENGINE_VERIFICATION_OPERATIONS,
    EXPERIMENTAL_OPT_IN_OPERATIONS,
)
from nexus_ai_agent.creative.studio.capabilities import ENGINE_PORT_ATTRIBUTES

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDIO_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "studio"

#: Prefixes the Studio core must not depend on: engines, packs, adapters, the
#: verifiers themselves, and the job/queue lane above it.
FORBIDDEN_IMPORT_PREFIXES = (
    "nexus_ai_agent.creative.atlas",
    "nexus_ai_agent.creative.audio_engine",
    "nexus_ai_agent.creative.chronos",
    "nexus_ai_agent.creative.morpheus",
    "nexus_ai_agent.creative.speech_cinema",
    "nexus_ai_agent.creative.veritas",
    "nexus_ai_agent.creative.packs",
    "nexus_ai_agent.creative.project_state",
    "nexus_ai_agent.jobs",
)


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_studio_core_imports_no_engine_pack_or_adapter() -> None:
    violations: list[str] = []
    for path in sorted(STUDIO_DIR.rglob("*.py")):
        for module in sorted(_imports_of(path)):
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert violations == [], violations


def test_engine_port_attributes_are_the_pinned_six() -> None:
    assert ENGINE_PORT_ATTRIBUTES == {
        "omega": "audio_engine",
        "veritas": "veritas_engine",
        "chronos": "chronos_engine",
        "atlas": "atlas_engine",
        "morpheus": "morpheus_engine",
        "hyperion": "hyperion_engine",
    }


def test_render_lane_agrees_with_the_capability_layer_operation_for_operation() -> None:
    from nexus_ai_agent.creative.studio.engine_capabilities import (
        ENGINE_CAPABILITIES,
        verify_operation_id,
    )

    declared = {verify_operation_id(engine): engine for engine in ENGINE_PORT_ATTRIBUTES}
    assert ENGINE_PORT_OPERATIONS == declared
    assert {capability.engine for capability in ENGINE_CAPABILITIES} == set(ENGINE_PORT_ATTRIBUTES)
    assert ENGINE_VERIFICATION_OPERATIONS == frozenset(declared)


def test_engine_capabilities_are_not_merged_into_the_pack_opt_in_surface() -> None:
    """The pack opt-in set is pinned to *exactly* the EXPERIMENTAL pack surface
    by ``tests/unit/test_creative_render_jobs.py``; engine capabilities are a
    second, disjoint axis. Mixing them would silently widen the pack gate."""
    assert EXPERIMENTAL_OPT_IN_OPERATIONS.isdisjoint(ENGINE_VERIFICATION_OPERATIONS)


@pytest.mark.parametrize(
    "module_path",
    [
        "src/nexus_ai_agent/creative/studio/bus.py",
        "src/nexus_ai_agent/creative/studio/capabilities.py",
    ],
)
def test_bus_and_capabilities_import_the_mapping_from_capabilities(module_path: str) -> None:
    """A single source of truth: ``ENGINE_PORT_ATTRIBUTES`` lives in
    ``capabilities.py``; importing it from ``engine_capabilities`` inside the bus
    would invert the layering (bus -> engine layer)."""
    text = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    assert "engine_capabilities import ENGINE_PORT_ATTRIBUTES" not in text
