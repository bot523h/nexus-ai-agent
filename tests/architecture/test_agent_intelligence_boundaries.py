"""Single-authority and layer-import ratchets for Agent Intelligence."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_proposal_authoring_compiler_and_orchestrator_have_no_execution_authority() -> None:
    files = (
        ROOT / "src/nexus_ai_agent/creative/spine/intent.py",
        ROOT / "src/nexus_ai_agent/creative/spine/strategy.py",
        ROOT / "src/nexus_ai_agent/creative/spine/authoring.py",
        ROOT / "src/nexus_ai_agent/creative/spine/compiler.py",
        ROOT / "src/nexus_ai_agent/creative/spine/lineage.py",
        ROOT / "src/nexus_ai_agent/orchestration/agent_intelligence.py",
    )
    forbidden = (
        "subprocess",
        "nexus_ai_agent.creative.packs.",
        "nexus_ai_agent.creative.rendering",
        "nexus_ai_agent.creative.slideshow.ffmpeg",
        "nexus_ai_agent.creative.studio.bus",
        "nexus_ai_agent.creative.studio.authorization",
        "nexus_ai_agent.tools.system_shell",
    )
    for path in files:
        imports = _imports(path)
        violations = {
            module for module in imports if module in forbidden or module.startswith(forbidden[1:])
        }
        assert not violations, f"{path.relative_to(ROOT)} imports execution authority: {violations}"


def test_registry_schema_is_injected_and_compiler_does_not_bind_a_pack_model() -> None:
    compiler = ROOT / "src/nexus_ai_agent/creative/spine/compiler.py"
    runtime = ROOT / "src/nexus_ai_agent/orchestration/agent_intelligence.py"
    compiler_imports = _imports(compiler)
    runtime_imports = _imports(runtime)

    assert not any(
        module.startswith("nexus_ai_agent.creative.packs.") for module in compiler_imports
    )
    assert not any(
        module.startswith("nexus_ai_agent.creative.packs.") for module in runtime_imports
    )
    assert "nexus_ai_agent.creative.studio.capabilities" in compiler_imports
    assert "nexus_ai_agent.creative.studio.capabilities" in runtime_imports


def test_llm_payload_and_worker_contract_expose_no_authorization_override() -> None:
    from nexus_ai_agent.creative.render_contracts import CreativeRenderPayload

    fields = set(CreativeRenderPayload.model_fields)
    assert "allow_experimental" not in fields
    assert "actor" not in fields
    assert "permissions" not in fields
    assert "command_id" not in fields
    assert "agent_lineage" in fields


def test_there_is_one_canonical_intent_and_one_creative_work_ir() -> None:
    src = ROOT / "src/nexus_ai_agent"
    expected = {
        "Intent": src / "creative/spine/models.py",
        "CreativeWork": src / "creative/intelligence/ir.py",
    }
    found: dict[str, list[Path]] = {name: [] for name in expected}
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in found:
                found[node.name].append(path)
    for name, canonical_path in expected.items():
        assert found[name] == [canonical_path], (
            f"{name} has competing definitions: {[path.relative_to(ROOT) for path in found[name]]}"
        )


def test_creative_intelligence_package_stays_spine_independent() -> None:
    package = ROOT / "src/nexus_ai_agent/creative/intelligence"
    for path in package.rglob("*.py"):
        imports = _imports(path)
        violations = {
            module for module in imports if module.startswith("nexus_ai_agent.creative.spine")
        }
        assert not violations, (
            f"{path.relative_to(ROOT)} crosses from the declarative IR into the spine: "
            f"{sorted(violations)}"
        )


def test_creative_context_is_routed_before_keyword_tool_execution() -> None:
    graph = (ROOT / "src/nexus_ai_agent/orchestration/graph.py").read_text(encoding="utf-8")
    route_start = graph.index("def route_intent(state: NexusState)")
    route_end = graph.index("def route_persona(state: NexusState)", route_start)
    route = graph[route_start:route_end]

    assert route.index('state.get("creative_asset")') < route.index('state.get("intent", "chat")')
    assert 'return "agent_intelligence"' in route
    assert 'graph.add_edge("agent_intelligence", "moderation")' in graph
