"""Deterministic bounded DAG tests; no external effects or identity mocks."""

from dataclasses import replace

import pytest

from nexus_ai_agent.creative.studio.plan_ir import PlanIR, PlanNode


def node(key: str, *parents: str) -> PlanNode:
    return PlanNode(key, "example.operation", "a" * 64, parents)


def test_order_and_hash_are_independent_of_input_order() -> None:
    nodes = (node("c", "b", "a"), node("b"), node("a"))
    plan = PlanIR("project", "plan", nodes)
    other = PlanIR("project", "plan", (nodes[2], nodes[1], node("c", "a", "b")))
    assert plan.plan_hash == other.plan_hash
    assert plan.canonical_bytes() == other.canonical_bytes()
    assert tuple(item.node_id for item in plan.topological_nodes()) == ("a", "b", "c")
    assert replace(plan, project_id="other").plan_hash != plan.plan_hash
    assert replace(plan, plan_id="other").plan_hash != plan.plan_hash
    assert (
        replace(plan, nodes=(replace(nodes[0], command_digest="b" * 64), *nodes[1:])).plan_hash
        != plan.plan_hash
    )


@pytest.mark.parametrize(
    "nodes,match",
    [
        ((node("a", "a"),), "cycle"),
        ((node("a", "b"), node("b", "a")), "cycle"),
        ((node("a", "missing"),), "unknown dependency"),
        ((node("a"), node("a")), "duplicate node"),
        ((), "node count"),
        (tuple(node(str(i)) for i in range(257)), "node count"),
        (tuple(node(str(i), *([str(i - 1)] if i else [])) for i in range(65)), "depth"),
        (tuple(node(str(i), *(str(j) for j in range(i))) for i in range(92)), "edge"),
    ],
)
def test_invalid_graphs(nodes: tuple[PlanNode, ...], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        PlanIR("project", "plan", nodes)


def test_maximum_depth_is_allowed() -> None:
    plan = PlanIR(
        "project", "plan", tuple(node(str(i), *([str(i - 1)] if i else [])) for i in range(64))
    )
    assert len(plan.topological_nodes()) == 64


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "g" * 64])
def test_invalid_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        PlanNode("a", "op", digest)


def test_duplicate_dependencies_and_version() -> None:
    with pytest.raises(ValueError, match="duplicate dependency"):
        node("a", "b", "b")
    with pytest.raises(ValueError, match="version"):
        PlanIR("project", "plan", (node("a"),), version=2)
    with pytest.raises(ValueError, match="version"):
        PlanIR("project", "plan", (node("a"),), version=True)


def test_compile_owns_commands_and_rejects_changed_bindings() -> None:
    from nexus_ai_agent.creative.studio.bus import _fingerprint
    from nexus_ai_agent.creative.studio.models import TargetRef, TypedCommand
    from nexus_ai_agent.creative.studio.plan_execution import compile_plan

    command = TypedCommand(
        command_id="one", operation="example.play", target=TargetRef(project_id="project")
    )
    ir = PlanIR("project", "plan", (PlanNode("a", command.operation, _fingerprint(command)),))
    compiled = compile_plan(ir, {"a": command})
    assert compiled.plan_id == "plan"
    assert compiled.steps[0].command == command.model_dump_json()
    with pytest.raises(ValueError, match="exactly cover"):
        compile_plan(ir, {})
    with pytest.raises(ValueError, match="differs"):
        compile_plan(ir, {"a": command.model_copy(update={"operation": "other.operation"})})
    with pytest.raises(ValueError, match="project differs"):
        compile_plan(replace(ir, project_id="other"), {"a": command})


def test_dag_structure_is_bound_into_bus_approval_hash() -> None:
    from nexus_ai_agent.creative.studio.bus import PlanTransactionError, _fingerprint
    from nexus_ai_agent.creative.studio.models import TargetRef, TypedCommand
    from nexus_ai_agent.creative.studio.plan_execution import compile_plan

    command = TypedCommand(
        command_id="one", operation="example.play", target=TargetRef(project_id="project")
    )
    a = PlanNode("a", command.operation, _fingerprint(command))
    b = replace(a, node_id="b")
    parallel = compile_plan(PlanIR("project", "plan", (a, b)), {"a": command, "b": command})
    serial = compile_plan(
        PlanIR("project", "plan", (a, replace(b, dependencies=("a",)))),
        {"a": command, "b": command},
    )
    assert parallel.steps == serial.steps
    assert parallel._step_input_hash(0, parallel.steps[0]) != serial._step_input_hash(
        0, serial.steps[0]
    )
    serial.admitted_ir_hash = "invalid"
    with pytest.raises(PlanTransactionError, match="DAG hash"):
        serial._step_input_hash(0, serial.steps[0])
