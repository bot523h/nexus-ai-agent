"""The creative execution spine: the intent-first vertical slice (D-0025).

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
    CompiledIntent,
    CreativeExecutionSpine,
    CreativeGraph,
    EvidenceRecord,
    GraphError,
    Intent,
    IntentError,
    PlannedOperation,
    RulesCapabilityCompiler,
    RulesIntentResolver,
    RulesRecipeAnalyzer,
    SpineRollbackError,
    abstract_recipe,
    recipe_to_intent,
)
from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    CommandBus,
    CommandValidationError,
    ProjectAccess,
    Timeline,
    TypedCommand,
    build_wave1_registry,
    new_project,
)
from nexus_ai_agent.creative.studio.models import UndoConflictError, UnknownOperationError


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


# --------------------------------------------------------------------------- #
# plan atomicity (task-221): a multi-step plan is all-or-nothing
# --------------------------------------------------------------------------- #
class _TwoStepCompiler:
    """A compiler whose second operation is always refused by the bus."""

    def compile(self, intent: Intent, registry: object) -> CompiledIntent:
        return CompiledIntent(
            intent_id=intent.intent_id,
            operations=("media.play", "timeline.mark"),
            plan=(
                PlannedOperation(operation="media.play"),
                # a raw int is not a resolvable reference -> the bus refuses it
                PlannedOperation(operation="timeline.mark", input={"at": 999999, "label": "x"}),
            ),
            rationale="probe: the second step is invalid",
        )


def test_a_failed_multi_step_run_leaves_no_committed_step() -> None:
    """All-or-nothing: a refused second step must not leave the first committed.

    Regression for a false-green: the earlier suite only exercised
    single-operation intents, so a plan that failed halfway silently committed
    its first half (revision advanced, an artifact node was written).
    """
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, compiler=_TwoStepCompiler())
    before_hash = bus.state_hash
    before_revision = bus.state_revision

    with pytest.raises(CommandValidationError):
        spine.execute_intent(Intent(project_id="proj1", goal="probe"))

    # content is restored exactly, and no artifact/evidence survives
    assert bus.state_hash == before_hash
    assert bus.project.timeline.markers == []
    assert not any(n.kind == "artifact" for n in graph.nodes())
    assert not any(n.kind == "evidence" for n in graph.nodes())
    # the bus's revision is monotonic by design; the rollback is not a rewound
    # revision but a restored content identity (state_hash above).
    assert bus.state_revision > before_revision


def test_a_successful_multi_step_run_still_commits_every_step() -> None:
    """The atomicity guard must not turn a good plan into a no-op."""
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)

    run = spine.run("این ویدیو را پخش کن و یک علامت بگذار")

    assert run.compiled.operations == ("media.play", "timeline.mark")
    assert run.result.state_revision == 2
    assert len(bus.project.timeline.markers) == 1
    assert sum(1 for n in graph.nodes() if n.kind == "artifact") == 2


def test_rollback_goes_through_the_bus_and_can_be_refused() -> None:
    """A run only rolls back what it is authorized to undo (no private write path).

    A read-only actor may apply ``media.play`` (A: project:read) but may not
    ``system.undo`` (needs project:write). The rollback is therefore refused, and
    the spine must raise it loudly instead of masking the original failure.
    """
    actor = ActorIdentity(kind="agent", actor_id="planner")

    class _ReadOnly:
        def authorize(self, actor_claim: ActorIdentity, project_id: str) -> ProjectAccess:
            return ProjectAccess(
                actor=actor_claim, project_id=project_id, permissions=frozenset({"project:read"})
            )

    bus = _bus(authorizer=_ReadOnly())
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, actor=actor, compiler=_TwoStepCompiler())

    with pytest.raises(SpineRollbackError):
        spine.execute_intent(Intent(project_id="proj1", goal="probe"))


# --------------------------------------------------------------------------- #
# task-222: a rollback is scoped to the run's OWN transactions
# --------------------------------------------------------------------------- #
class _TwoMarkCompiler:
    """Step 1 = a valid mark "OURS"; step 2 = a mark the bus refuses."""

    def compile(self, intent: Intent, registry: object) -> CompiledIntent:
        return CompiledIntent(
            intent_id=intent.intent_id,
            operations=("timeline.mark", "timeline.mark"),
            plan=(
                PlannedOperation(operation="timeline.mark", input={"at": "اینجا", "label": "OURS"}),
                PlannedOperation(operation="timeline.mark", input={"at": 999999, "label": "BAD"}),
            ),
            rationale="probe: the second step is invalid",
        )


def _foreign_mark(bus: CommandBus, label: str = "FOREIGN"):
    """A fully valid edit dispatched as another actor would dispatch it."""
    return bus.dispatch(
        TypedCommand(
            command_id=f"foreign-{label}",
            operation="timeline.mark",
            target={"project_id": bus.project.project_id},
            input={"at": {"kind": "absolute", "timecode_us": 5_000_000}, "label": label},
        )
    )


def test_failed_run_never_rolls_back_a_foreign_edit(monkeypatch) -> None:
    """Failure mode D: a concurrent edit between two steps must survive.

    The spine's step 1 commits, then another actor commits a valid edit, then the
    spine's step 2 is refused. A naive "undo the most recent transaction" rollback
    would destroy the foreign edit and leave the spine's own half-applied step.
    The transaction-scoped rollback must instead leave the foreign edit and fail
    closed (the run cannot sheathe its own step without touching foreign work).
    """
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, compiler=_TwoMarkCompiler())

    real_dispatch = bus.dispatch
    state = {"n": 0}

    def dispatch_then_interleave(command):
        result = real_dispatch(command)
        state["n"] += 1
        if state["n"] == 1:
            _foreign_mark(bus)
        return result

    monkeypatch.setattr(bus, "dispatch", dispatch_then_interleave)

    with pytest.raises(SpineRollbackError) as excinfo:
        spine.execute_intent(Intent(project_id="proj1", goal="probe"))

    # The refusal is the bus contract (task-223), not a caller-side pre-check:
    # the undo named the run's own transaction, the identity gate saw a foreign
    # newest, and the spine translated UndoConflictError into SpineRollbackError.
    assert isinstance(excinfo.value.__cause__, UndoConflictError)

    labels = [marker.label for marker in bus.project.timeline.markers]
    # the foreign edit survives; only the run's own work is a candidate to undo
    assert "FOREIGN" in labels
    # no artifact/evidence node is left for a run that did not complete
    assert not any(node.kind == "artifact" for node in graph.nodes())
    assert not any(node.kind == "evidence" for node in graph.nodes())


def test_duplicate_delivery_of_an_intent_is_idempotent() -> None:
    """Failure mode F: a redelivered intent must not mutate the project twice."""
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)
    intent = Intent(project_id="proj1", goal="این ویدیو را پخش کن و یک علامت بگذار")

    first = spine.execute_intent(intent)
    hash_after_first = bus.state_hash
    markers_after_first = len(bus.project.timeline.markers)

    second = spine.execute_intent(intent)

    assert second is first or second.result == first.result
    assert bus.state_hash == hash_after_first
    assert len(bus.project.timeline.markers) == markers_after_first == 1


def test_duplicate_intent_id_with_different_content_is_refused() -> None:
    """Reusing an intent_id for different work is a conflict, not a replay."""
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph)
    first = Intent(project_id="proj1", goal="mark this moment")
    spine.execute_intent(first)

    conflicting = Intent(
        intent_id=first.intent_id, project_id="proj1", goal="mark a different moment"
    )
    with pytest.raises(ValueError):
        spine.execute_intent(conflicting)


def test_a_crash_between_commit_and_graph_write_is_bounded(monkeypatch) -> None:
    """Failure mode J: a hard crash after the bus commit must not corrupt state.

    A real process death cannot run the ``except`` handler, so we simulate it with
    a BaseException (not ``Exception``) raised exactly after the first commit. The
    honest contract: the bus transaction is real and the graph node is absent --
    a *bounded, detectable* divergence, not silent corruption. The committed
    content is a normal transaction the bus can undo, and no orphan artifact node
    exists.
    """
    bus = _bus()
    graph = CreativeGraph("proj1")
    spine = CreativeExecutionSpine(bus, graph, compiler=_TwoMarkCompiler())

    real_add = graph.add_node
    calls = {"n": 0}

    def crash_on_first_artifact(*args, **kwargs):
        if kwargs.get("label", "").startswith("timeline.mark@") or args[:1] == ("artifact",):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt("simulated process death after commit")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(graph, "add_node", crash_on_first_artifact)

    with pytest.raises(KeyboardInterrupt):
        spine.execute_intent(Intent(project_id="proj1", goal="probe"))

    # The commit happened (one editable transaction), the artifact node did not.
    assert bus.state_revision == 1
    assert [t.operation for t in bus.history] == ["timeline.mark"]
    assert not any(node.kind == "artifact" for node in graph.nodes())
    # The divergence is a normal bus transaction, so a later undo still works.
    undo = bus.dispatch(TypedCommand(command_id="u1", operation="system.undo", input={}))
    assert undo.status == "applied"
    assert bus.project.timeline.markers == []
