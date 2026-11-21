"""PlanTransaction: atomic multi-step execution with LIFO compensating rollback.

Proves the transactional contract of ``PlanTransaction`` on ``CommandBus``:

1. Multi-step success commits with a full step ledger and SHA-256 hashes.
2. A mid-plan failure stops forward progress immediately (later steps never run)
   and compensates every prior step in exact reverse (LIFO) order.
3. A compensation failure does not abandon the remaining compensations; all
   rollback errors are collected and the transaction lands in ROLLBACK_FAILED.
4. Re-committing or re-rolling-back a finished transaction raises explicitly.
5. Bus integration: ``dispatch_plan`` runs real typed commands through the full
   validate -> authorize -> apply pipeline.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.bus import (
    CommandBus,
    PlanRollbackError,
    PlanStep,
    PlanTransaction,
    PlanTransactionError,
    PlanTransactionState,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    UndoCommandInput,
)
from nexus_ai_agent.creative.studio.models import (
    Clip,
    CommandValidationError,
    MediaRef,
    PermissionLevel,
    Playhead,
    Project,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)

TIMEBASE = TimeBase(numerator=30, denominator=1)
DIGEST = "sha256:" + "ab" * 32


def make_project(project_id: str = "project_01") -> Project:
    media = MediaRef(
        asset_id="asset_01",
        content_sha256=DIGEST,
        media_kind="video",
        duration_us=10_000_000,
        timebase=TIMEBASE,
    )
    clip = Clip(
        clip_id="clip_01",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=10_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=10_000_000),
    )
    track = Track(track_id="video_01", name="Video 1", kind="video", clips=[clip])
    timeline = Timeline(
        timeline_id="tl_01",
        duration_us=12_000_000,
        tracks=[track],
        playhead=Playhead(timecode_us=2_500_000, frame_number=75, timebase=TIMEBASE),
    )
    return new_project(project_id, "Contract Demo", timeline)


def make_command(
    operation: str,
    command_id: str,
    *,
    input: dict[str, Any] | None = None,  # noqa: A002
) -> TypedCommand:
    return TypedCommand.model_validate(
        {
            "command_id": command_id,
            "operation": operation,
            "schema_version": 1,
            "input": input if input is not None else {},
            "confirmed": False,
        }
    )


def _recording_step(
    index: int,
    log: list[tuple[str, int]],
    *,
    fail: bool = False,
    unsuccessful: bool = False,
    compensate_fails: bool = False,
    with_compensate: bool = True,
) -> PlanStep:
    def execute() -> Any:
        log.append(("exec", index))
        if fail:
            raise ValueError(f"step {index} boom")
        if unsuccessful:
            return False
        return f"result-{index}"

    def compensate(result: Any) -> None:
        log.append(("comp", index))
        if compensate_fails:
            raise RuntimeError(f"compensation {index} boom")

    return PlanStep(
        operation=f"op.step{index}",
        execute=execute,
        compensate=compensate if with_compensate else None,
    )


class TestPlanTransactionSuccess:
    def test_multi_step_success_commits_with_full_ledger(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-ok",
            steps=(_recording_step(0, log), _recording_step(1, log), _recording_step(2, log)),
        )
        returned = tx.commit()
        assert returned is tx
        assert tx.state is PlanTransactionState.COMMITTED
        assert log == [("exec", 0), ("exec", 1), ("exec", 2)]
        assert [r.step_index for r in tx.step_records] == [0, 1, 2]
        assert all(r.status == "succeeded" for r in tx.step_records)
        assert tx.rollback_records == []

    def test_step_records_carry_sha256_hashes(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-hash",
            steps=(_recording_step(0, log), _recording_step(1, log)),
        )
        tx.commit()
        for record in tx.step_records:
            assert len(record.input_hash) == 64
            assert len(record.output_hash) == 64
            int(record.input_hash, 16)
            int(record.output_hash, 16)
        # Distinct steps hash distinct inputs.
        assert tx.step_records[0].input_hash != tx.step_records[1].input_hash

    def test_single_step_plan_commits(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(plan_id="plan-single", steps=(_recording_step(0, log),))
        tx.commit()
        assert tx.state is PlanTransactionState.COMMITTED
        assert len(tx.step_records) == 1


class TestPlanTransactionFailureAndLifoRollback:
    def test_mid_step_failure_rolls_back_in_exact_lifo_order(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-lifo",
            steps=(
                _recording_step(0, log),
                _recording_step(1, log),
                _recording_step(2, log, fail=True),
            ),
        )
        with pytest.raises(PlanTransactionError):
            tx.commit()
        assert tx.state is PlanTransactionState.ROLLED_BACK
        assert log == [
            ("exec", 0),
            ("exec", 1),
            ("exec", 2),
            ("comp", 1),
            ("comp", 0),
        ]
        assert [r.step_index for r in tx.rollback_records] == [1, 0]
        assert all(r.status == "succeeded" for r in tx.rollback_records)

    def test_failure_stops_later_steps_from_executing(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-stop",
            steps=(
                _recording_step(0, log),
                _recording_step(1, log, fail=True),
                _recording_step(2, log),
                _recording_step(3, log),
            ),
        )
        with pytest.raises(PlanTransactionError):
            tx.commit()
        executed = [index for kind, index in log if kind == "exec"]
        assert executed == [0, 1]
        assert tx.state is PlanTransactionState.ROLLED_BACK

    def test_unsuccessful_result_triggers_rollback(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-false",
            steps=(_recording_step(0, log), _recording_step(1, log, unsuccessful=True)),
        )
        with pytest.raises(PlanTransactionError) as excinfo:
            tx.commit()
        assert excinfo.value.failed_step_index == 1
        assert tx.state is PlanTransactionState.ROLLED_BACK
        assert log == [("exec", 0), ("exec", 1), ("comp", 0)]

    def test_first_step_failure_rolls_back_nothing(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-first-fail",
            steps=(_recording_step(0, log, fail=True), _recording_step(1, log)),
        )
        with pytest.raises(PlanTransactionError):
            tx.commit()
        assert tx.state is PlanTransactionState.ROLLED_BACK
        assert log == [("exec", 0)]
        assert tx.rollback_records == []

    def test_step_without_compensate_is_skipped(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-skip",
            require_compensation=False,
            steps=(
                _recording_step(0, log, with_compensate=False),
                _recording_step(1, log),
                _recording_step(2, log, fail=True),
            ),
        )
        with pytest.raises(PlanTransactionError):
            tx.commit()
        assert tx.state is PlanTransactionState.ROLLBACK_FAILED
        assert log == [("exec", 0), ("exec", 1), ("exec", 2), ("comp", 1)]
        assert [r.step_index for r in tx.rollback_records] == [1]


class TestPlanTransactionRollbackFailure:
    def test_compensation_error_lands_in_rollback_failed(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-rb-fail",
            steps=(
                _recording_step(0, log),
                _recording_step(1, log, compensate_fails=True),
                _recording_step(2, log, fail=True),
            ),
        )
        with pytest.raises(PlanRollbackError) as excinfo:
            tx.commit()
        assert tx.state is PlanTransactionState.ROLLBACK_FAILED
        assert len(excinfo.value.rollback_errors) == 1
        assert [r.status for r in tx.rollback_records] == ["failed", "succeeded"]

    def test_rollback_continues_past_a_failing_compensation(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-rb-continue",
            steps=(
                _recording_step(0, log),
                _recording_step(1, log, compensate_fails=True),
                _recording_step(2, log, fail=True),
            ),
        )
        with pytest.raises(PlanRollbackError):
            tx.commit()
        # Step 0's compensation still ran after step 1's failed.
        assert log == [
            ("exec", 0),
            ("exec", 1),
            ("exec", 2),
            ("comp", 1),
            ("comp", 0),
        ]
        assert tx.state is PlanTransactionState.ROLLBACK_FAILED

    def test_multiple_compensation_errors_are_all_collected(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-rb-multi",
            steps=(
                _recording_step(0, log, compensate_fails=True),
                _recording_step(1, log, compensate_fails=True),
                _recording_step(2, log, fail=True),
            ),
        )
        with pytest.raises(PlanRollbackError) as excinfo:
            tx.commit()
        assert len(excinfo.value.rollback_errors) == 2
        assert tx.state is PlanTransactionState.ROLLBACK_FAILED


class TestPlanTransactionIdempotencyGuard:
    def test_recommit_finished_transaction_raises(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(plan_id="plan-guard", steps=(_recording_step(0, log),))
        tx.commit()
        assert tx.state is PlanTransactionState.COMMITTED
        with pytest.raises(PlanTransactionError):
            tx.commit()

    def test_rerollback_finished_transaction_raises(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(plan_id="plan-guard-rb", steps=(_recording_step(0, log),))
        tx.commit()
        with pytest.raises(PlanTransactionError):
            tx.rollback()

    def test_recommit_after_rollback_raises(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-guard-after-rb",
            steps=(_recording_step(0, log, fail=True),),
        )
        with pytest.raises(PlanTransactionError):
            tx.commit()
        assert tx.state is PlanTransactionState.ROLLED_BACK
        with pytest.raises(PlanTransactionError):
            tx.commit()

    def test_manual_rollback_of_pending_transaction(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(plan_id="plan-manual", steps=(_recording_step(0, log),))
        returned = tx.rollback()
        assert returned is tx
        assert tx.state is PlanTransactionState.ROLLED_BACK
        assert log == []


class TestPlanTransactionValidation:
    def test_empty_plan_is_rejected(self) -> None:
        with pytest.raises(PlanTransactionError):
            PlanTransaction(plan_id="plan-empty", steps=())

    def test_step_requires_exactly_one_of_command_or_execute(self) -> None:
        with pytest.raises(PlanTransactionError):
            PlanStep(operation="op.bad")
        with pytest.raises(PlanTransactionError):
            PlanStep(operation="op.bad", command={}, execute=lambda: None)

    def test_step_records_are_immutable(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(plan_id="plan-frozen", steps=(_recording_step(0, log),))
        tx.commit()
        with pytest.raises(FrozenInstanceError):
            tx.step_records[0].status = "failed"  # type: ignore[misc]


def _plan_test_registry(executed: list[str]) -> CapabilityRegistry:
    """A registry with untracked test-only operations (no truth projection)."""

    def _step(project: Project, context: OperationContext) -> OperationOutcome:
        executed.append("step")
        return OperationOutcome(project, context.history, {"ok": True})

    def _fail(project: Project, context: OperationContext) -> OperationOutcome:
        executed.append("fail")
        raise ValueError("test.plan_fail boom")

    registry = CapabilityRegistry()
    registry.register_operation(
        "test",
        "plan",
        OperationSpec(
            operation_id="test.plan_step",
            description="test plan step",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=UndoCommandInput,
            handler=_step,
        ),
    )
    registry.register_operation(
        "test",
        "plan",
        OperationSpec(
            operation_id="test.plan_fail",
            description="test plan failure",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=UndoCommandInput,
            handler=_fail,
        ),
    )
    return registry


class TestPlanTransactionBusIntegration:
    def test_dispatch_plan_commits_real_commands(self) -> None:
        executed: list[str] = []
        bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
        before = bus.state_revision
        plan = PlanTransaction(
            plan_id="plan-bus-ok",
            steps=(
                PlanStep(
                    operation="test.plan_step",
                    command=make_command("test.plan_step", "cmd_step_1"),
                ),
                PlanStep(
                    operation="test.plan_step",
                    command=make_command("test.plan_step", "cmd_step_2"),
                ),
            ),
        )
        returned = bus.dispatch_plan(plan)
        assert returned is plan
        assert plan.state is PlanTransactionState.COMMITTED
        assert bus.state_revision == before + 2
        assert executed == ["step", "step"]
        assert len(plan.step_records) == 2
        assert all(r.status == "succeeded" for r in plan.step_records)

    def test_dispatch_plan_failure_rolls_back(self) -> None:
        executed: list[str] = []
        bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
        log: list[tuple[str, int]] = []
        plan = PlanTransaction(
            plan_id="plan-bus-fail",
            steps=(
                PlanStep(
                    operation="test.plan_step",
                    command=make_command("test.plan_step", "cmd_step_3"),
                    compensate=lambda _result: log.append(("comp", 0)),
                ),
                PlanStep(
                    operation="test.plan_fail",
                    command=make_command("test.plan_fail", "cmd_fail_1"),
                ),
            ),
        )
        with pytest.raises(PlanTransactionError):
            bus.dispatch_plan(plan)
        assert plan.state is PlanTransactionState.ROLLED_BACK
        assert executed == ["step", "fail"]
        assert log == [("comp", 0)]
        assert plan.step_records[-1].status == "failed"
        assert [r.step_index for r in plan.rollback_records] == [0]

    def test_dispatch_plan_rejects_non_plan(self) -> None:
        bus = CommandBus(state=make_project())
        with pytest.raises(CommandValidationError):
            bus.dispatch_plan({"plan_id": "x"})  # type: ignore[arg-type]


def test_strict_compensation_preflight():
    with pytest.raises(ValueError, match="require compensation"):
        PlanTransaction(steps=(PlanStep("custom", execute=lambda: None),))


def test_operation_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        PlanStep("wrong", command=make_command("test.plan_step", "mismatch"))


def test_failed_plan_restores_bus_snapshot():
    executed = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    before = (bus.project, bus.history, dict(bus._idempotency))
    plan = PlanTransaction(
        steps=(
            PlanStep("test.plan_step", command=make_command("test.plan_step", "snapshot1")),
            PlanStep("test.plan_fail", command=make_command("test.plan_fail", "snapshot2")),
        )
    )
    with pytest.raises(PlanTransactionError):
        plan.commit(bus)
    assert (bus.project, bus.history, bus._idempotency) == before
    assert not bus._executing


def test_committed_plan_cache_and_undo():
    executed = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "cached"))
    plan = PlanTransaction(plan_id="cache", steps=(step,))
    before = bus.project
    plan.commit(bus)
    assert PlanTransaction(plan_id="cache", steps=(step,)).commit(bus) is plan
    assert executed == ["step"]
    plan.rollback_committed_plan()
    assert bus.project == before
    assert bus.history == ()


def test_callback_cannot_dispatch_reentrantly():
    bus = CommandBus(state=make_project())
    plan = PlanTransaction(
        steps=(
            PlanStep(
                "custom",
                execute=lambda: bus.dispatch_plan(
                    PlanTransaction(
                        steps=(PlanStep("inner", execute=lambda: None, compensate=lambda _: None),)
                    )
                ),
                compensate=lambda _: None,
            ),
        )
    )
    with pytest.raises(PlanTransactionError) as error:
        plan.commit(bus)
    assert "Re-entrant" in str(error.value.original_error) or "nested" in str(
        error.value.original_error
    )


def test_compensation_output_hash_is_actual_result():
    from nexus_ai_agent.creative.studio.bus import _canonical_hash

    plan = PlanTransaction(
        steps=(
            PlanStep("one", execute=lambda: 1, compensate=lambda _: {"undone": 1}),
            PlanStep("two", execute=lambda: False, compensate=lambda _: None),
        )
    )
    with pytest.raises(PlanTransactionError):
        plan.commit()
    assert plan.rollback_records[0].output_hash == _canonical_hash({"undone": 1})


def test_rollback_committed_plan_detects_aba_revision_drift_after_intervening_do_and_undo():
    bus = CommandBus(state=make_project(), registry=_plan_test_registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "aba1"))
    plan = PlanTransaction(steps=(step,))
    plan.commit(bus)
    # Same-content operations advance the audit history/revision (ABA).
    bus.dispatch(make_command("test.plan_step", "aba2"))
    assert bus.state_hash == plan._committed_hash
    before = (bus.project, bus.history)
    with pytest.raises(PlanTransactionError, match="ABA or revision drift"):
        plan.rollback_committed_plan()
    assert (bus.project, bus.history) == before


def test_plan_replay_ignores_transient_command_id_and_trace_id_differences():
    executed = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    first = make_command("test.plan_step", "transport1")
    second = make_command("test.plan_step", "transport2")
    plan = PlanTransaction(plan_id="logical", steps=(PlanStep(first.operation, command=first),))
    plan.commit(bus)
    replay = PlanTransaction(plan_id="logical", steps=(PlanStep(second.operation, command=second),))
    assert replay.commit(bus) is plan
    assert executed == ["step"]
    changed = make_command("test.plan_fail", "transport3")
    conflict = PlanTransaction(
        plan_id="logical", steps=(PlanStep(changed.operation, command=changed),)
    )
    with pytest.raises(ValueError, match="input conflict"):
        conflict.commit(bus)


@pytest.mark.parametrize("bad", ["", " ", "\n\t", None, 12])
def test_empty_or_whitespace_plan_id_is_rejected(bad):
    with pytest.raises(ValueError, match="plan_id must be a non-empty string"):
        PlanTransaction(plan_id=bad, steps=())


def test_committed_plan_cache_enforces_bounded_lru_capacity(monkeypatch):
    from nexus_ai_agent.creative.studio import bus as module

    monkeypatch.setattr(module, "MAX_COMMITTED_PLANS", 2)
    bus = CommandBus(state=make_project(), registry=_plan_test_registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "lru"))
    a = PlanTransaction(plan_id="a", steps=(step,)).commit(bus)
    PlanTransaction(plan_id="b", steps=(step,)).commit(bus)
    assert PlanTransaction(plan_id="a", steps=(step,)).commit(bus) is a
    PlanTransaction(plan_id="c", steps=(step,)).commit(bus)
    assert list(bus._plan_cache) == ["a", "c"]


def test_failing_step_emergency_compensation_runs_first():
    log = []
    plan = PlanTransaction(
        steps=(
            PlanStep("ok", execute=lambda: 1, compensate=lambda _: log.append("ok")),
            PlanStep(
                "fail",
                execute=lambda: False,
                compensate=lambda _: log.append("fail"),
                rollback_on_self_error=True,
            ),
        )
    )
    with pytest.raises(PlanTransactionError):
        plan.commit()
    assert log == ["fail", "ok"]
