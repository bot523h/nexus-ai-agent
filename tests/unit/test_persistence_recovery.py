"""Tests for DurableStore SQLite persistence and crash recovery."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus_ai_agent.creative.studio import (
    CommandBus,
    PlanTransaction,
    Project,
)
from nexus_ai_agent.creative.studio.persistence import DurableStore
from tests.unit.test_plan_transaction import make_command, make_project


def test_durable_store_project_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_studio.db"
        store = DurableStore(db_path)

        project = make_project("p_durable_01")
        bus = CommandBus(state=project)

        # Execute commands and persist
        cmd1 = make_command("timeline.mark", "c1", input={"at": "اینجا", "label": "Durable Mark 1"})
        bus.dispatch(cmd1)

        store.save_project_state(bus.project, bus.history)

        # Recover in fresh store instance simulating process restart
        fresh_store = DurableStore(db_path)
        recovered_proj = fresh_store.load_project_state("p_durable_01")
        recovered_txs = fresh_store.load_history("p_durable_01")

        assert recovered_proj is not None
        assert recovered_proj.state_revision == 1
        assert len(recovered_proj.timeline.markers) == 1
        assert recovered_proj.timeline.markers[0].label == "Durable Mark 1"
        assert len(recovered_txs) == 1
        assert recovered_txs[0].command_id == "c1"

        store.close()
        fresh_store.close()


def test_durable_store_plan_transaction_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_plan_durable.db"
        store = DurableStore(db_path)

        project = make_project("p_durable_02")
        bus = CommandBus(state=project)

        cmd1 = make_command("timeline.mark", "c1", input={"at": "اینجا", "label": "Plan Mark 1"})
        cmd2 = make_command("timeline.mark", "c2", input={"at": "اینجا", "label": "Plan Mark 2"})
        plan = PlanTransaction(plan_id="durable_plan_100", commands=(cmd1, cmd2))

        bus.dispatch_plan(plan)
        store.save_project_state(bus.project, bus.history)

        # Simulate restart
        fresh_store = DurableStore(db_path)
        recovered_bus = CommandBus(state=fresh_store.load_project_state("p_durable_02"))
        recovered_bus._history = fresh_store.load_history("p_durable_02")

        assert recovered_bus.state_revision == 2
        assert len(recovered_bus.project.timeline.markers) == 2
        assert all(tx.plan_id == "durable_plan_100" for tx in recovered_bus.history)

        # Verify system.undo(plan_id=...) on recovered bus
        undo_cmd = make_command("system.undo", "c_undo", input={"plan_id": "durable_plan_100"})
        recovered_bus.dispatch(undo_cmd)

        assert recovered_bus.project.timeline.markers == []

        store.close()
        fresh_store.close()
