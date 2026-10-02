"""Adversarial and behavioral suite for PlanTransaction staged execution on CommandBus.

Proves:
1. Multi-step plan execution is strictly atomic: failure at step N leaves zero
   partial mutations.
2. Plan execution returns PlanExecutionResult with sub-results and plan-bound
   transactions.
3. Targeted system.undo(plan_id=...) rewinds all transactions belonging to that plan
   when top-of-stack, and rejects non-top undos when subsequent transactions exist.
4. Plan-level idempotency reservation replays identical plan execution and
   rejects conflicting payloads.
5. Command-level idempotency keys inside a committed plan are propagated to the bus.
6. Concurrent plan dispatches execute with serializable isolation under the bus lock.
"""

from __future__ import annotations

import threading

import pytest

from nexus_ai_agent.creative.studio import (
    CommandBus,
    CommandExecutionError,
    CommandValidationError,
    IdempotencyConflictError,
    PlanExecutionResult,
    PlanTransaction,
    PreconditionError,
    Project,
)
from nexus_ai_agent.creative.studio.models import Preconditions
from nexus_ai_agent.creative.studio.testing import make_command, make_project


@pytest.fixture()
def project() -> Project:
    return make_project()


@pytest.fixture()
def bus(project: Project) -> CommandBus:
    return CommandBus(state=project)


class TestPlanTransactionSuite:
    def test_plan_transaction_atomicity_on_failure(self, bus: CommandBus) -> None:
        """Scenario A: Step 1 succeeds, Step 2 fails -> ZERO state mutation, ZERO history."""
        cmd1 = make_command(
            "timeline.mark",
            "cmd_step1",
            input={"at": "اینجا", "label": "Step 1 Staged"},
        )
        bad_target = {
            "target": {
                "project_id": "project_01",
                "track_id": "nonexistent_track",
                "clip_id": "clip_01",
            }
        }
        cmd2 = make_command(
            "timeline.split_at_playhead",
            "cmd_step2",
            input={"at": "اینجا"},
            extra=bad_target,
        )

        plan = PlanTransaction(
            plan_id="plan_fail_test",
            commands=(cmd1, cmd2),
        )

        with pytest.raises(CommandValidationError):
            bus.dispatch_plan(plan)

        # Invariant: Central state remains revision 0, 0 markers, 0 history transactions
        assert bus.state_revision == 0
        assert bus.project.timeline.markers == []
        assert len(bus.history) == 0

    def test_plan_transaction_success_commits_atomically(self, bus: CommandBus) -> None:
        """Plan execution with multiple valid commands commits all steps atomically."""
        cmd1 = make_command("timeline.mark", "cmd_p1", input={"at": "اینجا", "label": "Mark 1"})
        cmd2 = make_command("timeline.mark", "cmd_p2", input={"at": "اینجا", "label": "Mark 2"})

        plan = PlanTransaction(plan_id="plan_success_01", commands=(cmd1, cmd2))

        result = bus.dispatch_plan(plan)

        assert isinstance(result, PlanExecutionResult)
        assert result.plan_id == "plan_success_01"
        assert len(result.results) == 2
        assert bus.state_revision == 2
        assert len(bus.project.timeline.markers) == 2
        assert [m.label for m in bus.project.timeline.markers] == ["Mark 1", "Mark 2"]
        assert all(tx.plan_id == "plan_success_01" for tx in bus.history)

    def test_multistep_plan_targeted_undo_and_non_top_rejection(self, bus: CommandBus) -> None:
        """Undo of a plan rewinds ALL steps of that plan if top-of-stack, else rejects."""
        # Execute Plan A (multi-step: A1, A2)
        cmd_a1 = make_command(
            "timeline.mark", "cmd_A1", input={"at": "اینجا", "label": "Plan A Mark 1"}
        )
        cmd_a2 = make_command(
            "timeline.mark", "cmd_A2", input={"at": "اینجا", "label": "Plan A Mark 2"}
        )
        plan_a = PlanTransaction(plan_id="plan_A", commands=(cmd_a1, cmd_a2))
        bus.dispatch_plan(plan_a)

        # Execute Plan B (single-step: B1)
        cmd_b1 = make_command(
            "timeline.mark", "cmd_B1", input={"at": "اینجا", "label": "Plan B Mark 1"}
        )
        plan_b = PlanTransaction(plan_id="plan_B", commands=(cmd_b1,))
        bus.dispatch_plan(plan_b)

        assert len(bus.project.timeline.markers) == 3

        # Non-top undo rejection: attempting to undo Plan A when Plan B is on top fails!
        undo_a_early = make_command("system.undo", "cmd_undo_A", input={"plan_id": "plan_A"})
        with pytest.raises(CommandExecutionError, match="subsequent transactions"):
            bus.dispatch(undo_a_early)

        # Top-of-stack undo: undo Plan B first
        undo_b = make_command("system.undo", "cmd_undo_B", input={"plan_id": "plan_B"})
        undo_b_res = bus.dispatch(undo_b)
        assert undo_b_res.output["undone_plan_id"] == "plan_B"
        assert [m.label for m in bus.project.timeline.markers] == [
            "Plan A Mark 1",
            "Plan A Mark 2",
        ]

        # Top-of-stack undo: now undo multi-step Plan A
        undo_a = make_command("system.undo", "cmd_undo_A_2", input={"plan_id": "plan_A"})
        undo_a_res = bus.dispatch(undo_a)
        assert undo_a_res.output["undone_plan_id"] == "plan_A"
        assert undo_a_res.output["undone_transaction_count"] == 2
        assert bus.project.timeline.markers == []

    def test_undo_plan_via_transaction_id(self, bus: CommandBus) -> None:
        """system.undo using PlanExecutionResult.transaction_id rewinds all steps of the plan."""
        cmd1 = make_command(
            "timeline.mark", "cmd_tx_1", input={"at": "اینجا", "label": "Tx Mark 1"}
        )
        cmd2 = make_command(
            "timeline.mark", "cmd_tx_2", input={"at": "اینجا", "label": "Tx Mark 2"}
        )
        plan = PlanTransaction(plan_id="plan_tx_id_test", commands=(cmd1, cmd2))

        plan_res = bus.dispatch_plan(plan)
        assert len(bus.project.timeline.markers) == 2

        # Undo using plan_res.transaction_id
        undo_cmd = make_command(
            "system.undo", "cmd_undo_tx", input={"transaction_id": plan_res.transaction_id}
        )
        undo_res = bus.dispatch(undo_cmd)
        assert undo_res.output["undone_plan_id"] == "plan_tx_id_test"
        assert undo_res.output["undone_transaction_count"] == 2
        assert bus.project.timeline.markers == []

    def test_plain_system_undo_rewinds_top_plan_atomically(self, bus: CommandBus) -> None:
        """Plain system.undo (no args) rewinds the entire top plan transaction, not just step 2."""
        cmd1 = make_command("timeline.mark", "cmd_p1", input={"at": "اینجا", "label": "P1 Mark 1"})
        cmd2 = make_command("timeline.mark", "cmd_p2", input={"at": "اینجا", "label": "P1 Mark 2"})
        plan = PlanTransaction(plan_id="plan_plain_undo", commands=(cmd1, cmd2))

        bus.dispatch_plan(plan)
        assert len(bus.project.timeline.markers) == 2

        # Plain system.undo
        undo_cmd = make_command("system.undo", "cmd_undo_plain")
        undo_res = bus.dispatch(undo_cmd)
        assert undo_res.output["undone_plan_id"] == "plan_plain_undo"
        assert undo_res.output["undone_transaction_count"] == 2
        assert bus.project.timeline.markers == []

    def test_command_idempotency_inside_plan_does_not_index_error(self, bus: CommandBus) -> None:
        """Command idempotency hit inside plan returns cached result without index crash."""
        cmd1 = make_command(
            "timeline.mark",
            "cmd_idem_1",
            input={"at": "اینجا", "label": "Idem Mark"},
            idempotency_key="inner-cmd-key",
        )
        # First plan executes cmd1 with idempotency key
        plan1 = PlanTransaction(plan_id="plan_idem_1", commands=(cmd1,))
        bus.dispatch_plan(plan1)

        # Second plan includes cmd1 with SAME idempotency key + cmd2
        cmd1_repeat = make_command(
            "timeline.mark",
            "cmd_idem_1_dup",
            input={"at": "اینجا", "label": "Idem Mark"},
            idempotency_key="inner-cmd-key",
        )
        cmd2 = make_command(
            "timeline.mark", "cmd_idem_2", input={"at": "اینجا", "label": "New Mark"}
        )
        plan2 = PlanTransaction(plan_id="plan_idem_2", commands=(cmd1_repeat, cmd2))

        res2 = bus.dispatch_plan(plan2)
        assert len(res2.results) == 2
        assert len(bus.project.timeline.markers) == 2

    def test_command_idempotency_propagation_from_plan(self, bus: CommandBus) -> None:
        """Command-level idempotency keys reserved during plan dispatch persist on bus."""
        cmd1 = make_command(
            "timeline.mark",
            "cmd_1",
            input={"at": "اینجا", "label": "M1"},
            idempotency_key="cmd-key-inside-plan",
        )
        plan = PlanTransaction(plan_id="plan_idemp_inner", commands=(cmd1,))

        bus.dispatch_plan(plan)

        # Re-dispatching the individual command with the same idempotency key replays
        replay_cmd = make_command(
            "timeline.mark",
            "cmd_1_retry",
            input={"at": "اینجا", "label": "M1"},
            idempotency_key="cmd-key-inside-plan",
        )
        replay_res = bus.dispatch(replay_cmd)
        assert len(bus.project.timeline.markers) == 1
        assert replay_res.state_revision == 1

    def test_plan_idempotency_replay_and_conflict(self, bus: CommandBus) -> None:
        """Plan-level idempotency reservation replays identical plan execution."""
        cmd1 = make_command("timeline.mark", "cmd_1", input={"at": "اینجا", "label": "M1"})
        plan1 = PlanTransaction(
            plan_id="plan_idemp",
            commands=(cmd1,),
            idempotency_key="key-plan-100",
        )

        first_res = bus.dispatch_plan(plan1)
        assert first_res.state_revision == 1

        # Replay same plan with same idempotency key
        second_res = bus.dispatch_plan(plan1)
        assert second_res.transaction_id == first_res.transaction_id
        assert bus.state_revision == 1
        assert len(bus.project.timeline.markers) == 1

        # Conflict: same idempotency key, different commands
        cmd2 = make_command("timeline.mark", "cmd_2", input={"at": "اینجا", "label": "DIFFERENT"})
        conflicting_plan = PlanTransaction(
            plan_id="plan_idemp",
            commands=(cmd2,),
            idempotency_key="key-plan-100",
        )
        with pytest.raises(IdempotencyConflictError, match="different payload"):
            bus.dispatch_plan(conflicting_plan)

    def test_plan_precondition_check(self, bus: CommandBus) -> None:
        """Plan with stale precondition is rejected before any command runs."""
        bus.dispatch(make_command("media.play", "cmd_init"))
        assert bus.state_revision == 1

        stale_plan = PlanTransaction(
            plan_id="stale_p",
            commands=(make_command("timeline.mark", "c1", input={"at": "اینجا", "label": "x"}),),
            preconditions=Preconditions(state_revision=0),
        )

        with pytest.raises(PreconditionError, match="stale state_revision"):
            bus.dispatch_plan(stale_plan)

        assert bus.state_revision == 1

    def test_concurrent_plan_dispatches_isolated(self, bus: CommandBus) -> None:
        """Multiple threads dispatching plans concurrently execute without corruption."""
        results: list[PlanExecutionResult] = []
        errors: list[BaseException] = []

        def _run_plan(idx: int) -> None:
            try:
                cmd = make_command(
                    "timeline.mark", f"c_{idx}", input={"at": "اینجا", "label": f"Thread_{idx}"}
                )
                plan = PlanTransaction(plan_id=f"plan_thread_{idx}", commands=(cmd,))
                res = bus.dispatch_plan(plan)
                results.append(res)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_run_plan, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 10
        assert bus.state_revision == 10
        assert len(bus.project.timeline.markers) == 10
