"""CP5.2 adversarial transaction and scientific boundary regressions."""

import asyncio
import functools
import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nexus_ai_agent.creative.storyboard_compiler import cinematography_solver as cinema
from nexus_ai_agent.creative.studio import PlanStep, PlanTransaction
from nexus_ai_agent.creative.studio import bus as studio
from nexus_ai_agent.creative.veritas import se3_object_permanence as veritas

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_plan_transaction.py")))
make_project = HELPERS["make_project"]
make_command = HELPERS["make_command"]
registry = HELPERS["_plan_test_registry"]


def test_undo_revoked_permission_is_fail_closed(monkeypatch):
    bus = studio.CommandBus(make_project(), registry=registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "undo-auth"))
    plan = PlanTransaction(steps=(step,))
    plan.commit(bus)
    before = (bus.project, bus.history, plan.state)
    monkeypatch.setattr(
        bus.registry,
        "check_permission",
        lambda *a, **k: SimpleNamespace(allowed=False, reason="revoked"),
    )
    with pytest.raises(studio.ExecutionPolicyError, match="revoked"):
        plan.rollback_committed_plan()
    assert (bus.project, bus.history, plan.state) == before


def test_step_command_transport_is_deeply_frozen_against_toctou():
    command = make_command("test.plan_step", "later").model_dump(mode="json")
    bus = studio.CommandBus(make_project(), registry=registry([]))
    later = PlanStep("test.plan_step", command=command)

    def mutate():
        command["input"]["injected"] = ["not accepted by schema"]
        command["operation"] = "missing.operation"
        return True

    tx = PlanTransaction(
        steps=(PlanStep("mutate", execute=mutate, compensate=lambda _: None), later)
    )
    tx.commit(bus)
    assert tx.state is studio.PlanTransactionState.COMMITTED
    assert isinstance(later.command, str)
    assert json.loads(later.command)["input"] == {}


@pytest.mark.parametrize("value", [False, np.bool_(False)])
def test_false_result_is_saved_for_emergency_compensation(value):
    observed = []
    tx = PlanTransaction(
        steps=(
            PlanStep(
                "failure",
                execute=lambda: value,
                compensate=observed.append,
                rollback_on_self_error=True,
            ),
        )
    )
    with pytest.raises(studio.PlanTransactionError):
        tx.commit()
    assert observed[0] is value
    assert tx.state is studio.PlanTransactionState.ROLLED_BACK


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_forward_control_exceptions_propagate_after_lifo_cleanup(error):
    log = []
    bus = studio.CommandBus(make_project())

    def interrupt():
        raise error()

    tx = PlanTransaction(
        steps=(
            PlanStep("first", execute=lambda: 1, compensate=lambda _: log.append(0)),
            PlanStep("interrupt", execute=interrupt, compensate=lambda _: None),
        )
    )
    with pytest.raises(error):
        tx.commit(bus)
    assert log == [0]
    assert not bus._executing
    assert tx.state is studio.PlanTransactionState.ROLLED_BACK


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_compensation_control_exceptions_do_not_abandon_other_steps(error):
    log = []

    def interrupt(_):
        log.append(1)
        raise error()

    tx = PlanTransaction(
        steps=(
            PlanStep("zero", execute=lambda: 0, compensate=lambda _: log.append(0)),
            PlanStep("one", execute=lambda: 1, compensate=interrupt),
            PlanStep("fail", execute=lambda: False, compensate=lambda _: None),
        )
    )
    with pytest.raises(error):
        tx.commit()
    assert log == [1, 0]
    assert tx.state is studio.PlanTransactionState.ROLLBACK_FAILED


def test_unserializable_and_surrogate_evidence_never_prevents_compensation():
    class Hostile:
        def __str__(self):
            raise ValueError("cannot serialize")

    log = []
    tx = PlanTransaction(
        steps=(
            PlanStep("zero", execute=lambda: "\ud800", compensate=lambda _: log.append(0)),
            PlanStep("hostile", execute=Hostile, compensate=lambda _: Hostile()),
            PlanStep("fail", execute=lambda: False, compensate=lambda _: None),
        )
    )
    with pytest.raises(studio.PlanTransactionError):
        tx.commit()
    assert log == [0]
    assert len(tx.rollback_records) == 2
    expected = hashlib.sha256(
        json.dumps("\ud800", ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert tx.step_records[0].output_hash == expected


def test_newer_callback_only_plan_fences_older_undo():
    bus = studio.CommandBus(make_project())
    step = PlanStep("callback", execute=lambda: True, compensate=lambda _: None)
    first = PlanTransaction(steps=(step,))
    first.commit(bus)
    PlanTransaction(steps=(step,)).commit(bus)
    assert bus.state_revision == 0
    with pytest.raises(studio.PlanTransactionError, match="drift"):
        first.rollback_committed_plan()


def test_audit_ledger_is_an_immutable_snapshot():
    tx = PlanTransaction(
        steps=(PlanStep("callback", execute=lambda: True, compensate=lambda _: None),)
    )
    tx.commit()
    records = tx.step_records
    assert isinstance(records, tuple)
    assert isinstance(tx.rollback_records, tuple)
    with pytest.raises(AttributeError):
        tx.step_records.append(None)
    with pytest.raises(AttributeError):
        tx.step_records = ()
    assert tx.step_records == records


def test_callback_partial_kwdefaults_and_state_affect_fingerprint():
    def function(value, *, amount=1):
        return value + amount

    def tx(fn):
        return PlanTransaction(steps=(PlanStep("op", execute=fn, compensate=lambda _: None),))

    one = tx(functools.partial(function, 1, amount=2))
    two = tx(functools.partial(function, 1, amount=3))
    assert one._step_input_hash(0, one.steps[0]) != two._step_input_hash(0, two.steps[0])
    normal = tx(function)
    before = normal._step_input_hash(0, normal.steps[0])
    function.__kwdefaults__["amount"] = 4
    assert normal._step_input_hash(0, normal.steps[0]) != before

    class Callback:
        def __init__(self, value):
            self.value = value

        def __call__(self):
            return self.value

    for factory in (lambda obj: obj, lambda obj: obj.__call__):
        a, b = tx(factory(Callback(1))), tx(factory(Callback(2)))
        assert a._step_input_hash(0, a.steps[0]) != b._step_input_hash(0, b.steps[0])


GLOBAL_CALLBACK_PARAMETER = 1


def _global_callback():
    return GLOBAL_CALLBACK_PARAMETER


def test_referenced_globals_and_opaque_state_fail_closed(monkeypatch):
    step = PlanStep("globals", execute=_global_callback, compensate=lambda _: None)
    bus = studio.CommandBus(make_project())
    PlanTransaction(plan_id="globals", steps=(step,)).commit(bus)
    monkeypatch.setitem(_global_callback.__globals__, "GLOBAL_CALLBACK_PARAMETER", 2)
    with pytest.raises(ValueError, match="conflict"):
        PlanTransaction(plan_id="globals", steps=(step,)).commit(bus)
    opaque = object()
    step = PlanStep("opaque", execute=lambda: opaque, compensate=lambda _: None)
    PlanTransaction(plan_id="opaque", steps=(step,)).commit(bus)
    with pytest.raises(studio.PlanTransactionError, match="opaque"):
        PlanTransaction(plan_id="opaque", steps=(step,)).commit(bus)


def test_external_effects_require_callable_compensation():
    for kwargs in ({"external_effects": True}, {"compensate": 123}):
        with pytest.raises(ValueError):
            PlanStep("external", execute=lambda: None, **kwargs)


def test_snapshot_byte_budget_and_immutable_history_sharing(monkeypatch):
    bus = studio.CommandBus(make_project(), registry=registry([]))
    command = PlanStep("test.plan_step", command=make_command("test.plan_step", "history"))
    PlanTransaction(steps=(command,)).commit(bus)
    step = PlanStep("callback", execute=lambda: True, compensate=lambda _: None)
    first, second = PlanTransaction(steps=(step,)), PlanTransaction(steps=(step,))
    first.commit(bus)
    second.commit(bus)
    assert first._snapshot["_history"][0] is second._snapshot["_history"][0]
    bus.history[0].state_before.clear()
    assert bus.history[0].state_before
    monkeypatch.setattr(studio, "MAX_PLAN_SNAPSHOT_BYTES", 1)
    PlanTransaction(steps=(step,)).commit(bus)
    assert not bus._plan_cache
    assert len(bus._plan_summaries) == 4


@pytest.mark.parametrize(
    "bad", ["1", b"1", 1 + 2j, ["1", 2, 3], np.array([1 + 2j]), np.array(["1", "2", "3"])]
)
def test_geometric_arrays_reject_strings_and_complex_before_coercion(bad):
    with pytest.raises((TypeError, ValueError)):
        cinema.so3_exp(bad)
    with pytest.raises(veritas.ObjectPermanenceError):
        veritas.se3_exp(bad)


def test_pose_owns_immutable_input_and_veritas_errors_are_domain_specific():
    rotation = np.eye(3)
    position = np.zeros(3)
    pose = cinema.CameraPoseSE3.from_rt(rotation, position)
    rotation[0, 0] = 999
    position[0] = 999
    np.testing.assert_array_equal(pose.rotation_matrix, np.eye(3))
    np.testing.assert_array_equal(pose.translation_vector, np.zeros(3))
    with pytest.raises(TypeError):
        pose.rotation[0][0] = 999
    for bad in (np.eye(3), np.ones((4, 4)), np.full((4, 4), np.nan)):
        with pytest.raises(veritas.ObjectPermanenceError) as error:
            veritas.se3_log(bad)
        assert error.value.__cause__ is not None


def test_logical_payload_conflict_and_nonempty_trace_replay():
    from pydantic import BaseModel, ConfigDict

    from nexus_ai_agent.creative.studio.capabilities import (
        CapabilityRegistry,
        OperationOutcome,
        OperationSpec,
    )
    from nexus_ai_agent.creative.studio.models import PermissionLevel

    class Payload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        values: list[int]

    reg = CapabilityRegistry()
    reg.register_operation(
        "test",
        "payload",
        OperationSpec(
            operation_id="test.payload",
            description="payload",
            input_model=Payload,
            permission_level=PermissionLevel.IMMEDIATE,
            handler=lambda project, context: OperationOutcome(
                project, context.history, {"values": context.input_data["values"]}
            ),
        ),
    )
    bus = studio.CommandBus(make_project(), registry=reg)

    def command(values, trace):
        return make_command("test.payload", "payload", input={"values": values}).model_copy(
            update={"trace_id": trace}
        )

    step = PlanStep("test.payload", command=command([1, 2], "trace-a"))
    first = PlanTransaction(plan_id="logical-payload", steps=(step,))
    first.commit(bus)
    replay = PlanTransaction(
        plan_id="logical-payload",
        steps=(PlanStep("test.payload", command=command([1, 2], "trace-b")),),
    )
    assert replay.commit(bus) is first
    with pytest.raises(ValueError, match="conflict"):
        PlanTransaction(
            plan_id="logical-payload",
            steps=(PlanStep("test.payload", command=command([3], "trace-c")),),
        ).commit(bus)


def _shot():
    from nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser import (
        parse_bilingual_script,
    )

    scene = parse_bilingual_script("INT. ROOM - NIGHT\nREZA\nOne.\n\nSARA\nTwo.").scenes[0]
    return cinema.solve_scene_cinematography(scene).shots[0]


def test_strict_geometry_shapes_domains_and_metadata():
    from dataclasses import replace

    shot = _shot()
    for field, value in (
        ("shot_index", True),
        ("shot_index", -1),
        ("shot_kind", "SINGLE"),
        ("intentional_axis_jump", 1),
        ("axis_side", 2),
        ("subject_position", (1.0, 2.0)),
    ):
        with pytest.raises((ValueError, TypeError)):
            replace(shot, **{field: value}).validate_strict()
    for field, value in (
        ("original_position", (1.0, 2.0)),
        ("enforced_axis_side", 5),
        ("was_axis_reflected", 1),
    ):
        with pytest.raises((ValueError, TypeError)):
            replace(shot.start_axis_trace, **{field: value}).validate_strict()
    bad_uv = replace(shot.framing, target_uv=(1.0, 2.0, 3.0))
    with pytest.raises(ValueError, match="shape"):
        replace(shot, framing=bad_uv).validate_strict()
    for field, value in (
        ("horizontal_fov_deg", 0.0),
        ("horizontal_fov_deg", 180.0),
        ("vertical_fov_deg", 180.0),
        ("dof_near_m", 0.0),
        ("dof_near_m", shot.lens.focus_distance_m + 1.0),
        ("dof_far_m", shot.lens.focus_distance_m - 0.1),
    ):
        with pytest.raises(ValueError):
            replace(shot.lens, **{field: value})


def test_wave2_facade_exports():
    from nexus_ai_agent.creative import storyboard_compiler as facade

    assert facade.se3_exp is cinema.se3_exp
    assert facade.se3_log is cinema.se3_log
    assert facade.verify_minimum_jerk_calculus_invariants()["verified"]


def test_external_effect_failure_always_compensates_partial_effects():
    effects = []

    def execute():
        effects.append("partial")
        raise ValueError("failed after external write")

    tx = PlanTransaction(
        steps=(
            PlanStep(
                "external",
                execute=execute,
                external_effects=True,
                compensate=lambda _: effects.clear(),
            ),
        )
    )
    with pytest.raises(studio.PlanTransactionError):
        tx.commit()
    assert effects == []
    assert tx.state is studio.PlanTransactionState.ROLLED_BACK


def test_callback_evidence_handles_nonfinite_bytes_cycles_and_recursion():
    assert studio._stable_value(1.5, frozenset()) == 1.5
    assert not studio._replay_safe(studio._stable_value(float("nan"), frozenset()))
    assert studio._stable_value(b"owned", frozenset()) == {"bytes": b"owned".hex()}
    cycle = []
    cycle.append(cycle)
    assert not studio._replay_safe(studio._stable_value(cycle, frozenset()))

    def recursive():
        return recursive()

    assert not studio._replay_safe(studio._callable_evidence(recursive))


def test_metadata_rejects_non_integer_index_not_only_boolean():
    from dataclasses import replace

    with pytest.raises(TypeError, match="int"):
        replace(_shot(), shot_index=1.5).validate_strict()


def test_opaque_recursive_callback_and_evicted_snapshot_accounting():
    def callback():
        return None

    assert not studio._replay_safe(studio._callable_evidence(callback, frozenset({id(callback)})))
    assert (
        studio._snapshot_cache_bytes({"evicted": ("fingerprint", SimpleNamespace(_snapshot=None))})
        == 0
    )
