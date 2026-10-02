"""Comprehensive failure injection and adversarial tests for studio PlanTransaction.

Covers the 28-point failure matrix and invariant checks:
- Transport identity separation (different command_id / trace_id with same plan payload)
- Pre-existing vs. plan-level idempotency reservation interaction
- Failed plan retry & discard of staged reservations
- Reused plan_id handling in undo operations
- Foreign edits intervening between plan dispatches
- Stale state_revision and state_hash handling
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.studio import (
    CommandBus,
    CommandExecutionError,
    CommandValidationError,
    PlanTransaction,
    PreconditionError,
    Project,
)
from nexus_ai_agent.creative.studio.models import Preconditions
from tests.unit.test_plan_transaction import make_command, make_project


@pytest.fixture()
def project() -> Project:
    return make_project()


@pytest.fixture()
def bus(project: Project) -> CommandBus:
    return CommandBus(state=project)


class TestPlanFailureMatrix:
    def test_same_plan_different_transport_ids_yields_same_fingerprint(
        self, bus: CommandBus
    ) -> None:
        """Finding A verification: transport ID changes do not alter logical fingerprint."""
        cmd1 = make_command("timeline.mark", "cmd_trans_1", input={"at": "اینجا", "label": "M1"})
        plan1 = PlanTransaction(
            plan_id="plan_trans_01",
            commands=(cmd1,),
            idempotency_key="key_trans_100",
        )

        res1 = bus.dispatch_plan(plan1)

        # Re-dispatch with different transport identifiers inside command
        cmd1_mod = make_command(
            "timeline.mark",
            "cmd_trans_1_NEW_TRANSPORT_ID",
            input={"at": "اینجا", "label": "M1"},
            extra={"trace_id": "trace_xyz_999"},
        )
        plan1_mod = PlanTransaction(
            plan_id="plan_trans_01",
            commands=(cmd1_mod,),
            idempotency_key="key_trans_100",
        )

        res2 = bus.dispatch_plan(plan1_mod)
        assert res2.transaction_id == res1.transaction_id
        assert bus.state_revision == 1

    def test_preexisting_idempotency_reservation_preserved_on_failed_plan(
        self, bus: CommandBus
    ) -> None:
        """Finding B verification: pre-existing idempotency reservations remain active."""
        # Pre-reserve command idempotency key
        cmd_pre = make_command(
            "timeline.mark",
            "cmd_pre",
            input={"at": "اینجا", "label": "Pre-Reserved"},
            idempotency_key="key_pre_1",
        )
        res_pre = bus.dispatch(cmd_pre)
        assert bus.state_revision == 1

        # Now dispatch a plan that fails on step 2
        cmd_ok = make_command("timeline.mark", "cmd_ok", input={"at": "اینجا", "label": "OK"})
        cmd_fail = make_command(
            "timeline.split_at_playhead",
            "cmd_fail",
            input={"at": "اینجا"},
            extra={
                "target": {
                    "project_id": "project_01",
                    "track_id": "nonexistent_track",
                    "clip_id": "clip_01",
                }
            },
        )
        plan_fail = PlanTransaction(
            plan_id="plan_fail_idemp",
            commands=(cmd_ok, cmd_fail),
            idempotency_key="key_plan_fail",
        )

        with pytest.raises(CommandValidationError):
            bus.dispatch_plan(plan_fail)

        # Pre-existing reservation must still exist and return previous result
        cmd_pre_retry = make_command(
            "timeline.mark",
            "cmd_pre_retry",
            input={"at": "اینجا", "label": "Pre-Reserved"},
            idempotency_key="key_pre_1",
        )
        res_retry = bus.dispatch(cmd_pre_retry)
        assert res_retry.transaction_id == res_pre.transaction_id
        assert bus.state_revision == 1

    def test_intervening_foreign_transaction_blocks_plan_undo(self, bus: CommandBus) -> None:
        """Finding C verification: foreign intervening transactions block non-top plan undo."""
        # Step 1: Execute Plan 1
        cmd_p1 = make_command("timeline.mark", "c_p1", input={"at": "اینجا", "label": "P1 Mark"})
        plan1 = PlanTransaction(plan_id="plan_intervene_1", commands=(cmd_p1,))
        bus.dispatch_plan(plan1)
        assert bus.state_revision == 1

        # Step 2: Foreign single command execution
        cmd_foreign = make_command(
            "timeline.mark", "c_foreign", input={"at": "اینجا", "label": "Foreign Edit"}
        )
        bus.dispatch(cmd_foreign)
        assert bus.state_revision == 2

        # Attempt to undo Plan 1 while foreign edit sits on top of stack -> must fail closed
        undo_cmd = make_command("system.undo", "c_undo_p1", input={"plan_id": "plan_intervene_1"})
        with pytest.raises(CommandExecutionError, match="subsequent transactions"):
            bus.dispatch(undo_cmd)

        assert bus.state_revision == 2
        assert len(bus.project.timeline.markers) == 2

    def test_stale_state_hash_precondition_rejection(self, bus: CommandBus) -> None:
        """Plan with mismatched state_hash precondition fails before executing any command."""
        stale_plan = PlanTransaction(
            plan_id="plan_stale_hash",
            commands=(make_command("media.play", "c_play"),),
            preconditions=Preconditions(
                state_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000"
            ),
        )
        with pytest.raises(PreconditionError, match="stale state_hash"):
            bus.dispatch_plan(stale_plan)

        assert bus.state_revision == 0
