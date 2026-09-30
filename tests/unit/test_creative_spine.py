"""The creative execution spine: the intent-first vertical slice (D-0024).

This suite proves the end-to-end path the direction document describes::

    Intent -> graph node -> capability selection -> policy/authority
           -> typed command -> CommandBus -> deterministic execution
           -> artifact -> lineage/evidence -> graph

It also pins the *security* invariants: the spine never bypasses the bus, so a
command the bus refuses produces no artifact, and an intent with no compiled
operations is refused rather than silently "succeeding".
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.spine import (
    ArtifactRecord,
    CompilationError,
    CreativeExecutionSpine,
    CreativeGraph,
    EvidenceRecord,
    GraphError,
    Intent,
    IntentError,
    RulesCapabilityCompiler,
    RulesIntentResolver,
    RulesRecipeAnalyzer,
    abstract_recipe,
    recipe_to_intent,
)
from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    CommandBus,
    ProjectAccess,
    Timeline,
    build_wave1_registry,
    new_project,
)
from nexus_ai_agent.creative.studio.models import UnknownOperationError


def _bus(project_id: str = "proj1", *, authorizer: object | None = None) -> CommandBus:
    timeline = Timeline(timeline_id="tl1", duration_us=30_000_000)
    return CommandBus(new_project(project_id, "demo", timeline), authorizer=authorizer)  # type: ignore[arg-type]


def test_intent_to_artifact_end_to_end() -> None:
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)

    run = spine.run("این ویدیو را پخش کن و یک علامت بگذار")

    # capability selection (two operations) -> typed commands -> bus -> execution
    assert run.compiled.operations == ("media.play", "timeline.mark")
    assert run.result.status == "applied"
    assert run.result.state_revision == 2
    assert isinstance(run.artifact, ArtifactRecord)
    assert run.artifact.content_hash == run.result.state_hash
    assert isinstance(run.evidence, EvidenceRecord)
    assert run.evidence.operation == "timeline.mark"

    # lineage: intent -> capability nodes -> artifact chain -> evidence
    kinds = [node.kind for node in graph.nodes()]
    assert kinds.count("intent") == 1
    assert kinds.count("capability") == 2
    assert kinds.count("artifact") == 2
    assert kinds.count("evidence") == 1
    intent_node = next(n for n in graph.nodes() if n.kind == "intent")
    # both capability nodes are children of the intent (the first artifact
    # hangs off the intent too, as the start of the artifact chain)
    capability_children = [
        child
        for child in graph.children(intent_node.node_id)
        if graph.node(child).kind == "capability"
    ]
    assert len(capability_children) == 2


def test_artifact_lineage_is_traceable_to_its_intent() -> None:
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)
    run = spine.run("play the video")

    # walking parents from the evidence node reaches the intent node
    evidence = next(n for n in graph.nodes() if n.kind == "evidence")
    ancestors: set[str] = set()
    frontier = list(evidence.parents)
    while frontier:
        node_id = frontier.pop()
        ancestors.add(node_id)
        frontier.extend(graph.parents(node_id))
    intent_node = next(n for n in graph.nodes() if n.kind == "intent")
    assert intent_node.node_id in ancestors
    assert run.artifact.node_id in ancestors


def test_bus_refusal_produces_no_artifact() -> None:
    """Policy/Authority stay authoritative: a refused command leaves no artifact."""
    actor = ActorIdentity(kind="user", actor_id="alice")

    class _Denier:
        def authorize(self, actor_claim: ActorIdentity, project_id: str) -> ProjectAccess:
            return ProjectAccess(
                actor=actor_claim,
                project_id=project_id,
                permissions=frozenset(),  # grants nothing
            )

    bus = _bus(authorizer=_Denier())
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, actor=actor)

    before = len(graph.nodes())
    with pytest.raises(AuthorizationError):
        spine.run("پخش کن")
    # the graph recorded the intent, but no artifact/evidence was written
    assert not any(n.kind == "artifact" for n in graph.nodes())
    assert not any(n.kind == "evidence" for n in graph.nodes())
    assert len(graph.nodes()) > before  # the intent node was recorded


def test_spine_emits_schema2_commands_for_an_authorized_actor() -> None:
    actor = ActorIdentity(kind="agent", actor_id="planner-1")

    class _Granter:
        def authorize(self, actor_claim: ActorIdentity, project_id: str) -> ProjectAccess:
            return ProjectAccess(
                actor=actor_claim,
                project_id=project_id,
                permissions=frozenset({"project:read", "project:write"}),
            )

    bus = _bus(authorizer=_Granter())
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, actor=actor)
    run = spine.run("mark this moment")
    assert run.result.status == "applied"


def test_unknown_operation_is_refused_by_the_bus_registry() -> None:
    """A compiler cannot smuggle an operation the registry does not expose."""
    registry = build_wave1_registry()
    with pytest.raises(UnknownOperationError):
        registry.get_spec("media.teleport")


def test_compiler_refuses_an_operation_absent_from_the_registry() -> None:
    """A rule that names an operation the registry lacks must be a CompilationError.

    This is what makes the registry the single allow-list: the compiler cannot
    emit an operation the bus would later reject as unknown.
    """
    from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry

    empty_registry = CapabilityRegistry()  # exposes no operations
    intent = Intent(project_id="proj1", goal="این ویدیو را پخش کن")
    with pytest.raises(CompilationError):
        RulesCapabilityCompiler().compile(intent, empty_registry)


def test_intent_compiling_to_no_operations_is_refused() -> None:
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)
    with pytest.raises(ValueError):
        spine.run("درباره فلسفه حرف بزن")  # no creative operation matches


def test_compiler_refuses_to_guess_a_target_operation() -> None:
    """split needs an explicit target clip; the rules compiler will not invent one."""
    compiler = RulesCapabilityCompiler()
    registry = build_wave1_registry()
    intent = Intent(project_id="proj1", goal="این کلیپ را برش بزن")
    with pytest.raises(CompilationError):
        compiler.compile(intent, registry)


def test_intent_for_a_different_project_is_rejected() -> None:
    bus = _bus("proj1")
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)
    intent = Intent(project_id="proj-other", goal="play")
    with pytest.raises(ValueError):
        spine.execute_intent(intent)


def test_resolver_rejects_an_empty_request() -> None:
    with pytest.raises(IntentError):
        RulesIntentResolver().resolve("   ", project_id="proj1")


def test_graph_rejects_an_unknown_parent() -> None:
    graph = CreativeGraph("proj1")
    with pytest.raises(GraphError):
        graph.add_node("artifact", parents=("node_missing",))


# --------------------------------------------------------------------------- #
# reference -> creative recipe (first killer capability, honest MVP)
# --------------------------------------------------------------------------- #
def test_reference_becomes_a_strategy_not_a_copy() -> None:
    analysis = RulesRecipeAnalyzer().analyze_hints(
        "https://example.test/ref", {"pacing": "fast", "contrast": "high"}
    )
    recipe = abstract_recipe(analysis)
    assert recipe.dimensions["pacing"] == "fast hook strategy: front-load the payoff"
    assert recipe.dimensions["contrast"] == "high-contrast grade intent"
    # heuristic, unmeasured -> low confidence (never presented as fact)
    assert recipe.confidence == 0.3
    assert recipe.recipe_hash.startswith("sha256:")


def test_recipe_to_intent_carries_the_no_copy_constraint() -> None:
    recipe = abstract_recipe(RulesRecipeAnalyzer().analyze_hints("ref", {"pacing": "fast"}))
    intent = recipe_to_intent(recipe, project_id="proj1")
    assert intent.source == "reference"
    assert any("do not copy" in c for c in intent.constraints)
    assert "fast hook strategy" in intent.goal


def test_recipe_is_content_addressed_and_deterministic() -> None:
    analyzer = RulesRecipeAnalyzer()
    first = abstract_recipe(analyzer.analyze_hints("ref", {"pacing": "fast"}))
    second = abstract_recipe(analyzer.analyze_hints("ref", {"pacing": "fast"}))
    assert first.recipe_hash == second.recipe_hash
