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
from typing import Any, cast

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
    CommandExecutionError,
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
    # The log is an opaque observation sink, not a mutable callback parameter.
    # Shared mutable behavioral state is deliberately rejected by preflight.
    from types import SimpleNamespace

    sink = SimpleNamespace(append=log.append)

    def execute() -> Any:
        sink.append(("exec", index))
        if fail:
            raise ValueError(f"step {index} boom")
        if unsuccessful:
            return False
        return f"result-{index}"

    def compensate(result: Any) -> None:
        sink.append(("comp", index))
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
        assert tx.rollback_records == ()

    def test_step_records_carry_sha256_hashes(self) -> None:
        log: list[tuple[str, int]] = []
        tx = PlanTransaction(
            plan_id="plan-hash",
            steps=(_recording_step(0, log), _recording_step(1, log)),
        )
        tx.commit()
        import hashlib
        import json

        from nexus_ai_agent.creative.studio.bus import _callable_evidence

        for index, record in enumerate(tx.step_records):
            step = tx.steps[index]
            expected_input = {
                "operation": f"op.step{index}",
                "step_index": index,
                "command": None,
                "execute": _callable_evidence(step.execute),
                "compensate": _callable_evidence(step.compensate),
                "rollback_on_self_error": False,
                "external_effects": False,
            }
            canonical_input = json.dumps(
                expected_input,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            assert record.input_hash == hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()
            canonical = json.dumps(
                f"result-{index}",
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            assert record.output_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
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
        assert tx.rollback_records == ()

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


def test_strict_compensation_preflight() -> None:
    with pytest.raises(ValueError, match="require compensation"):
        PlanTransaction(steps=(PlanStep("custom", execute=lambda: None),))


def test_operation_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        PlanStep("wrong", command=make_command("test.plan_step", "mismatch"))


def test_failed_plan_restores_bus_snapshot() -> None:
    executed: list[str] = []
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


def test_committed_plan_cache_and_undo() -> None:
    executed: list[str] = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "cached"))
    plan = PlanTransaction(plan_id="cache", steps=(step,))
    before = bus.project
    plan.commit(bus)
    assert PlanTransaction(plan_id="cache", steps=(step,)).commit(bus) is plan
    assert executed == ["step"]
    committed_revision = bus.state_revision
    plan.rollback_committed_plan()
    assert bus.state_hash == before.state_hash
    assert bus.state_revision == committed_revision + 1
    assert bus.history[-1].operation == "plan.rollback_committed"


def test_callback_cannot_dispatch_reentrantly() -> None:
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


def test_compensation_output_hash_is_actual_result() -> None:
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


def test_rollback_committed_plan_detects_aba_revision_drift_after_intervening_do_and_undo() -> None:
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


def test_plan_replay_ignores_transient_command_id_and_trace_id_differences() -> None:
    executed: list[str] = []
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
def test_empty_or_whitespace_plan_id_is_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="plan_id must be a non-empty string"):
        PlanTransaction(plan_id=bad, steps=())


def test_committed_plan_cache_enforces_bounded_lru_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.creative.studio import bus as module

    monkeypatch.setattr(module, "MAX_COMMITTED_PLANS", 2)
    bus = CommandBus(state=make_project(), registry=_plan_test_registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "lru"))
    a = PlanTransaction(plan_id="a", steps=(step,)).commit(bus)
    PlanTransaction(plan_id="b", steps=(step,)).commit(bus)
    assert PlanTransaction(plan_id="a", steps=(step,)).commit(bus) is a
    PlanTransaction(plan_id="c", steps=(step,)).commit(bus)
    assert list(bus._plan_cache) == ["a", "c"]


def test_failing_step_emergency_compensation_runs_first() -> None:
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


def test_rollback_cannot_interrupt_forward_execution() -> None:
    plan: PlanTransaction

    def execute() -> Any:
        with pytest.raises(PlanTransactionError):
            plan.rollback()
        return True

    plan = PlanTransaction(steps=(PlanStep("closure", execute=execute, compensate=lambda _: None),))
    plan.commit()
    assert plan.state is PlanTransactionState.COMMITTED


def test_attached_rollback_obeys_bus_execution_guard() -> None:
    bus = CommandBus(state=make_project())
    plan = PlanTransaction(
        steps=(PlanStep("closure", execute=lambda: 1, compensate=lambda _: None),)
    )
    plan._bus = bus
    bus._executing = True
    with pytest.raises(PlanTransactionError, match="Re-entrant"):
        plan.rollback()
    bus._executing = False
    assert plan.rollback().state is PlanTransactionState.ROLLED_BACK


def test_replay_revalidates_current_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    bus = CommandBus(state=make_project(), registry=_plan_test_registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "policy"))
    PlanTransaction(plan_id="policy", steps=(step,)).commit(bus)
    monkeypatch.setattr(
        bus.registry,
        "check_permission",
        lambda *a, **k: SimpleNamespace(allowed=False, reason="permission revoked"),
    )
    from nexus_ai_agent.creative.studio.models import ExecutionPolicyError

    with pytest.raises(ExecutionPolicyError, match="revoked"):
        PlanTransaction(plan_id="policy", steps=(step,)).commit(bus)


def test_replay_revalidates_current_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.studio.models import CapabilityError

    bus = CommandBus(state=make_project(), registry=_plan_test_registry([]))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "removed"))
    PlanTransaction(plan_id="removed", steps=(step,)).commit(bus)

    def removed(_: Any) -> Any:
        raise CapabilityError("operation removed")

    monkeypatch.setattr(bus.registry, "get_spec", removed)
    with pytest.raises(CapabilityError, match="removed"):
        PlanTransaction(plan_id="removed", steps=(step,)).commit(bus)


def test_step_fingerprint_binds_compensation_and_self_error_flag() -> None:
    def execute() -> Any:
        return 1

    def undo_one(_: Any) -> Any:
        return 1

    def undo_two(_: Any) -> Any:
        return 2

    tx = PlanTransaction(steps=(PlanStep("step", execute=execute, compensate=undo_one),))
    first = tx._step_input_hash(0, tx.steps[0])
    assert tx._step_input_hash(0, PlanStep("step", execute=execute, compensate=undo_two)) != first
    assert (
        tx._step_input_hash(
            0, PlanStep("step", execute=execute, compensate=undo_one, rollback_on_self_error=True)
        )
        != first
    )


def test_lru_eviction_preserves_lightweight_redelivery(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.studio import bus as module

    monkeypatch.setattr(module, "MAX_COMMITTED_PLANS", 1)
    monkeypatch.setattr(module, "MAX_PLAN_SUMMARIES", 2)
    executed: list[str] = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "evicted"))
    first = PlanTransaction(plan_id="first", steps=(step,)).commit(bus)
    PlanTransaction(plan_id="second", steps=(step,)).commit(bus)
    assert isinstance(first, PlanTransaction)
    assert first._snapshot is None
    revision = bus.state_revision
    replay = PlanTransaction(plan_id="first", steps=(step,)).commit(bus)
    assert isinstance(replay, module.PlanResultSummary)
    assert replay.state is PlanTransactionState.COMMITTED
    assert executed == ["step", "step"]
    assert bus.state_revision == revision
    PlanTransaction(plan_id="third", steps=(step,)).commit(bus)
    assert list(bus._plan_summaries) == ["first", "third"]
    assert len(bus._plan_cache) == 1


def test_committed_undo_records_revision_without_reexecution() -> None:
    executed: list[str] = []
    bus = CommandBus(state=make_project(), registry=_plan_test_registry(executed))
    step = PlanStep("test.plan_step", command=make_command("test.plan_step", "undo-revision"))
    plan = PlanTransaction(plan_id="undo-revision", steps=(step,)).commit(bus)
    committed = bus.state_revision
    assert isinstance(plan, PlanTransaction)
    plan.rollback_committed_plan()
    assert bus.state_revision == committed + 1
    assert len(bus.history) == 2
    assert bus.history[-1].parent_revision == committed
    replay = PlanTransaction(plan_id="undo-revision", steps=(step,)).commit(bus)
    assert replay.state is PlanTransactionState.ROLLED_BACK
    assert executed == ["step"]


def test_plan_guard_error_paths_and_snapshot_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.studio import bus as module

    assert len(module._canonical_hash(float("nan"))) == 64
    with pytest.raises(PlanTransactionError, match="non-empty"):
        PlanStep("", execute=lambda: 1)
    step = PlanStep("closure", execute=lambda: 1, compensate=lambda _: None)
    plan = PlanTransaction(steps=(step,))
    plan._restore_snapshot()
    with pytest.raises(PlanTransactionError, match="requires a bus"):
        plan.rollback_committed_plan()
    bus = CommandBus(state=make_project())
    bus._executing = True
    with pytest.raises(CommandExecutionError, match="Re-entrant"):
        plan.commit(bus)
    bus._executing = False
    for state in (PlanTransactionState.EXECUTING, PlanTransactionState.ROLLING_BACK):
        plan.state = state
        with pytest.raises(PlanTransactionError):
            plan.commit()
    plan.state = PlanTransactionState.PENDING
    plan.commit(bus)
    with pytest.raises(PlanTransactionError, match="finished"):
        plan.commit(bus)
    bus._executing = True
    with pytest.raises(CommandExecutionError, match="Re-entrant"):
        plan.rollback_committed_plan()
    bus._executing = False
    plan._snapshot = None
    with pytest.raises(PlanTransactionError, match="evicted"):
        plan.rollback_committed_plan()
    plan.state = PlanTransactionState.PENDING
    with pytest.raises(PlanTransactionError, match="not committed"):
        plan.rollback_committed_plan()


def test_replay_closure_plan_and_no_bus_command_error() -> None:
    step = PlanStep("closure", execute=lambda: 1, compensate=lambda _: None)
    bus = CommandBus(state=make_project())
    plan = PlanTransaction(plan_id="closure-replay", steps=(step,))
    plan.commit(bus)
    assert PlanTransaction(plan_id="closure-replay", steps=(step,)).commit(bus) is plan
    command = PlanStep("test.plan_step", command=make_command("test.plan_step", "no-bus"))
    with pytest.raises(PlanTransactionError) as error:
        PlanTransaction(steps=(command,)).commit()
    assert "no bus" in str(error.value.original_error)


def test_committed_undo_compensation_failure_is_audited() -> None:
    def fail(_: Any) -> Any:
        raise ValueError("emergency")

    bus = CommandBus(state=make_project())
    plan = PlanTransaction(steps=(PlanStep("closure", execute=lambda: 1, compensate=fail),))
    plan.commit(bus)
    with pytest.raises(PlanRollbackError):
        plan.rollback_committed_plan()
    assert plan.state is PlanTransactionState.ROLLBACK_FAILED
    assert bus.state_revision == 1
    assert bus._plan_summaries[plan.plan_id][1].state is PlanTransactionState.ROLLBACK_FAILED


def test_manual_pending_rollback_collects_compensation_errors() -> None:
    from nexus_ai_agent.creative.studio.bus import PlanStepRecord

    def fail(_: Any) -> Any:
        raise ValueError("undo")

    plan = PlanTransaction(steps=(PlanStep("closure", execute=lambda: 1, compensate=fail),))
    plan._step_records.append(PlanStepRecord(0, "closure", "succeeded", "a" * 64, "b" * 64))
    with pytest.raises(PlanRollbackError):
        plan.rollback()
    assert plan.state is PlanTransactionState.ROLLBACK_FAILED


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("change", ["execute", "compensate", "closure"])
def test_mid_plan_callback_toctou_refuses_changed_step_and_compensates_prior(
    attached: bool,
    change: str,
) -> None:
    events: list[str] = []
    state = {"value": 1}

    class Callback:
        def __init__(self) -> None:
            self.value = 1

        def __call__(self, *_: Any) -> int:
            events.append("later-ran")
            return self.value

    execute, compensate = Callback(), Callback()

    def later_closure() -> int:
        events.append("later-ran")
        return state["value"]

    def mutate() -> int:
        if change == "closure":
            state["value"] = 99
        else:
            (execute if change == "execute" else compensate).value = 99
        return 7

    def undo(value: Any) -> None:
        assert value == 7
        events.append("prior-compensated")

    # The second callback's output sink is opaque; its behavioral state is not.
    # The closure case intentionally also detects the mutable shared event list.
    later = PlanStep(
        "later",
        execute=later_closure if change == "closure" else execute,
        compensate=compensate,
        rollback_on_self_error=True,
    )
    plan = PlanTransaction(steps=(PlanStep("mutate", execute=mutate, compensate=undo), later))
    bus = CommandBus(make_project()) if attached else None
    with pytest.raises(PlanTransactionError) as error:
        plan.commit(bus)
    assert "TOCTOU" in str(error.value.original_error)
    assert events == ["prior-compensated"]
    assert plan.state is PlanTransactionState.ROLLED_BACK
    assert [record.step_index for record in plan.rollback_records] == [0]
    if bus is not None:
        assert not bus._executing
        assert bus.state_revision == 0


@pytest.mark.parametrize("declaration", ["spec", "handler"])
@pytest.mark.parametrize("compensated", [False, True])
def test_command_external_effect_declaration_enforced_in_preflight(
    declaration: str,
    compensated: bool,
) -> None:
    from dataclasses import replace

    executed: list[str] = []
    registry = _plan_test_registry(executed)
    spec = registry.get_spec("test.plan_step")
    if declaration == "spec":
        registry = CapabilityRegistry()
        registry.register_operation("test", "plan", replace(spec, external_effects=True))
    else:
        cast(Any, spec.handler).external_effects = True
    bus = CommandBus(make_project(), registry=registry)
    from types import SimpleNamespace

    events: list[str] = []
    sink = SimpleNamespace(append=events.append)
    step = PlanStep(
        "test.plan_step",
        command=make_command("test.plan_step", "external"),
        compensate=(lambda _: sink.append("compensated")) if compensated else None,
    )
    plan = PlanTransaction(
        steps=(
            PlanStep("prior", execute=lambda: sink.append("prior"), compensate=lambda _: None),
            step,
        )
    )
    if compensated:
        plan.commit(bus)
        plan.rollback_committed_plan()
        assert executed == ["step"]
        assert events == ["prior", "compensated"]
    else:
        with pytest.raises(PlanTransactionError, match="external effects require"):
            plan.commit(bus)
        assert executed == []
        assert events == []
        assert plan.state is PlanTransactionState.PENDING
        assert not bus._executing
        assert bus.history == ()


def test_inferred_external_effect_partial_failure_is_compensated() -> None:
    from dataclasses import replace

    executed: list[str] = []
    registry = _plan_test_registry(executed)
    spec = registry.get_spec("test.plan_fail")
    registry = CapabilityRegistry()
    registry.register_operation("test", "plan", replace(spec, external_effects=True))
    bus = CommandBus(make_project(), registry=registry)
    plan = PlanTransaction(
        steps=(
            PlanStep(
                "test.plan_fail",
                command=make_command("test.plan_fail", "partial"),
                compensate=lambda _: executed.clear(),
            ),
        )
    )
    with pytest.raises(PlanTransactionError):
        plan.commit(bus)
    assert executed == []
    assert plan.state is PlanTransactionState.ROLLED_BACK
    assert len(plan.rollback_records) == 1
