"""Harness integrity, strict oracle, and focused W4 security regression tests."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "w4_mutation_harness", ROOT / "scripts/w4_mutations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_registry_is_exactly_the_declared_unique_valid_ast_mutations(harness: ModuleType) -> None:
    registry = harness.REGISTRY
    expected = [f"W4-M{index:02}" for index in range(1, 31)]
    expected += [f"W3-R7-M{index:02}" for index in range(1, 9)]  # Wave 3-R7 additions
    assert [item.id for item in registry] == expected
    assert len({(item.source, item.scope, item.old, item.new) for item in registry}) == len(
        expected
    )
    for mutation in registry:
        original = (ROOT / mutation.source).read_bytes()
        assert harness.mutated_bytes(original, mutation) != original
        assert (ROOT / mutation.target_test.split("::")[0]).is_file()
        assert mutation.expected_failure_pattern


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5, -9])
def test_oracle_never_counts_infrastructure_or_wrong_target(harness: ModuleType, code: int) -> None:
    mutation = harness.REGISTRY[0]
    valid = f"AssertionError\nFAILED {mutation.target_test} - AssertionError"
    assert harness.classify(code, valid, mutation) == (
        {0: "SURVIVED", 1: "KILLED"}.get(code, "INVALID")
    )
    if code:
        for marker in (
            "SyntaxError",
            "IndentationError",
            "ImportError",
            "ModuleNotFoundError",
            "collected 0 items",
            "INTERNALERROR",
            "ERROR at setup",
            "ERROR at teardown",
        ):
            assert harness.classify(code, valid + marker, mutation) == "INVALID"
        assert (
            harness.classify(code, "AssertionError\nFAILED other.py::test_other", mutation)
            == "INVALID"
        )
        assert (
            harness.classify(code, f"FAILED {mutation.target_test} - unrelated", mutation)
            == "INVALID"
        )


def test_cache_purge_and_exception_restoration_are_byte_exact(
    tmp_path: Path, harness: ModuleType
) -> None:
    source = tmp_path / "sample.py"
    original = b"def answer():\n    return 1\n"
    source.write_bytes(original)
    mutation = harness.Mutation(
        "test", "sample.py", "answer", "return 1", "return 2", "test.py::test"
    )
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    cache.write_bytes(b"stale")
    optimized = cache.with_name(cache.name.replace(".pyc", ".opt-1.pyc"))
    optimized.write_bytes(b"stale optimized")
    harness._purge_bytecode_cache(source)
    assert not cache.exists() and not optimized.exists()
    digest = hashlib.sha256(original).hexdigest()
    with pytest.raises(RuntimeError, match="interrupted"):
        with harness.installed_mutant(source, mutation) as before:
            assert before == digest
            assert b"return 2" in source.read_bytes()
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(b"mutant cache")
            raise RuntimeError("interrupted")
    assert source.read_bytes() == original
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert not cache.exists()


def test_invalid_or_noop_mutation_is_rejected(harness: ModuleType) -> None:
    original = b"def answer():\n    return 1\n"
    mutation = harness.Mutation(
        "test", "sample.py", "answer", "return 1", "return 1", "test.py::test"
    )
    with pytest.raises(ValueError, match="AST"):
        harness.mutated_bytes(original, mutation)
    mutation = harness.Mutation(
        "test", "sample.py", "missing", "return 1", "return 2", "test.py::test"
    )
    with pytest.raises(ValueError, match="scope"):
        harness.mutated_bytes(original, mutation)


def test_store_lock_rejects_replaced_symlink(tmp_path: Path) -> None:
    from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore

    store = DurablePlanDedupeStore(tmp_path / "db")
    lock = Path(f"{store.path}.lock")
    lock.unlink(missing_ok=True)
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"untouched")
    lock.symlink_to(victim)
    with pytest.raises(OSError):
        store.reserve("p", "a" * 64, external_effects=False)
    assert victim.read_bytes() == b"untouched"


def test_compiler_rejects_same_operation_changed_digest() -> None:
    from nexus_ai_agent.creative.studio.bus import command_fingerprint
    from nexus_ai_agent.creative.studio.models import TargetRef, TypedCommand
    from nexus_ai_agent.creative.studio.plan_execution import compile_plan
    from nexus_ai_agent.creative.studio.plan_ir import PlanIR, PlanNode

    command = TypedCommand(
        command_id="c", operation="sample.echo", target=TargetRef(project_id="p")
    )
    ir = PlanIR("p", "plan", (PlanNode("n", command.operation, command_fingerprint(command)),))
    changed = command.model_copy(update={"confirmed": True})
    with pytest.raises(ValueError, match="differs"):
        compile_plan(ir, {"n": changed})


def test_idempotency_and_permission_gates() -> None:
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.capabilities import (
        CapabilityRegistry,
        OperationContext,
        OperationOutcome,
        OperationSpec,
        PlayCommandInput,
    )
    from nexus_ai_agent.creative.studio.models import (
        ExecutionPolicyError,
        IdempotencyConflictError,
        PermissionLevel,
        Project,
        Timeline,
        TypedCommand,
        new_project,
    )

    project = new_project("p", "test", Timeline(timeline_id="t", duration_us=0))
    registry = CapabilityRegistry()
    calls: list[str] = []

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        calls.append(context.command.operation)
        return OperationOutcome(state, context.history, {})

    for operation, level in (
        ("sample.echo", PermissionLevel.IMMEDIATE),
        ("sample.denied", PermissionLevel.DENIED),
    ):
        registry.register_operation(
            "sample", operation, OperationSpec(operation, "test", level, PlayCommandInput, handler)
        )
    bus = CommandBus(project, registry=registry)
    command = TypedCommand(command_id="c", operation="sample.echo", idempotency_key="once")
    result = bus.dispatch(command)
    assert bus.dispatch(command) == result
    with pytest.raises(IdempotencyConflictError):
        bus.dispatch(command.model_copy(update={"confirmed": True}))
    with pytest.raises(ExecutionPolicyError):
        bus.dispatch(TypedCommand(command_id="d", operation="sample.denied"))
    assert calls == ["sample.echo"]


def test_runtime_postcondition_checks_disk_not_claimed_digest(tmp_path: Path) -> None:
    from dataclasses import asdict

    from nexus_ai_agent.adapters.durable_studio_runtime import _artifact_verifier
    from nexus_ai_agent.adapters.fenced_filesystem_destination import (
        FencedFilesystemEffectDestination,
        FilesystemObservationVerifier,
    )
    from nexus_ai_agent.creative.studio.models import Timeline, TypedCommand, new_project

    writer = FencedFilesystemEffectDestination(tmp_path, project_id="p")
    reader = FilesystemObservationVerifier(tmp_path, project_id="p")
    observation = writer.apply(effect_key="a" * 64, fencing_epoch=1, payload=b"wrong content")
    result: Any = SimpleNamespace(
        output={
            "artifact": asdict(observation),
            "payload_digest": hashlib.sha256(b"expected").hexdigest(),
        }
    )
    command = TypedCommand(
        command_id="c", operation="artifact.publish", input={"content": "expected"}
    )
    project = new_project("p", "test", Timeline(timeline_id="t", duration_us=0))
    assert not _artifact_verifier(reader)(project, command, result, "a" * 64)


def test_graph_node_and_edge_limits() -> None:
    from nexus_ai_agent.creative.studio.plan_ir import MAX_NODES, PlanIR, PlanNode

    nodes = tuple(PlanNode(f"n{i}", "sample.echo", "a" * 64) for i in range(MAX_NODES + 1))
    with pytest.raises(ValueError, match="node count"):
        PlanIR("p", "plan", nodes)
    # Two layers avoid triggering the depth limit instead of the edge limit.
    roots = nodes[:65]
    leaves = tuple(
        PlanNode(f"leaf{i}", "sample.echo", "a" * 64, tuple(n.node_id for n in roots))
        for i in range(65)
    )
    with pytest.raises(ValueError, match="edge limit"):
        PlanIR("p", "plan", roots + leaves)
