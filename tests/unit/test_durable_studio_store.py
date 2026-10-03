"""Contract and regression tests for DurableStudioStore and CommandBus crash recovery."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    Clip,
    CommandBus,
    IdempotencyConflictError,
    LeaseExpiredError,
    MediaRef,
    Playhead,
    Project,
    ReferenceResolutionError,
    StaleFencingTokenError,
    TargetRef,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)
from nexus_ai_agent.creative.studio.models import PlanTransaction
from nexus_ai_agent.creative.studio.persistence import DurableStudioStore
from nexus_ai_agent.creative.studio.testing import default_authorizer

TIMEBASE = TimeBase(numerator=30, denominator=1)
DIGEST = "sha256:" + "ab" * 32


def make_project(project_id: str = "project_persist_01") -> Project:
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
    project = new_project(project_id, "Persistence Test Project", timeline)
    return Project.model_validate(
        {
            **project.model_dump(mode="json"),
            "assets": [
                {
                    "asset_id": "asset_01",
                    "media_kind": "video",
                    "content_sha256": DIGEST,
                    "duration_us": 10_000_000,
                    "parent_asset_ids": [],
                    "provenance": {"origin": "test"},
                }
            ],
        }
    )


def make_actor(actor_id: str = "alice") -> ActorIdentity:
    return ActorIdentity(kind="user", actor_id=actor_id)


def make_command(
    operation: str = "timeline.mark",
    command_id: str = "cmd_01",
    *,
    input: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> TypedCommand:
    return TypedCommand.model_validate(
        {
            "command_id": command_id,
            "operation": operation,
            "schema_version": 1,
            "actor": {"kind": "user", "actor_id": "alice"},
            "input": input if input is not None else {"at": "اینجا", "label": "Test Marker"},
            "idempotency_key": idempotency_key,
        }
    )


class TestDurableStudioStoreContract:
    def test_command_bus_persists_state_and_recovers_after_simulated_crash(self) -> None:
        """CommandBus with DurableStudioStore recovers exact state/history after process crash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")

            # Phase 1: Initialize store & bus, perform dispatches
            store1 = DurableStudioStore(db_path)
            project1 = make_project("proj_crash_test")
            bus1 = CommandBus(state=project1, store=store1, authorizer=default_authorizer())

            res1 = bus1.dispatch(make_command("timeline.mark", "cmd_1", idempotency_key="key_1"))
            assert res1.state_revision == 1

            plan_cmd = make_command(
                "timeline.mark", "cmd_2", input={"at": "اینجا", "label": "Plan Mark"}
            )
            plan = PlanTransaction(
                plan_id="plan_crash_1",
                actor=make_actor("alice"),
                target=TargetRef(project_id="proj_crash_test"),
                commands=(plan_cmd,),
                idempotency_key="key_plan_1",
            )
            bus1.dispatch_plan(plan)
            assert bus1.state_revision == 2
            expected_hash = bus1.state_hash
            expected_markers = bus1.project.timeline.markers
            store1.close()

            # Phase 2: Simulate process crash/restart by creating new store & bus from DB
            store2 = DurableStudioStore(db_path)
            recovered_data = store2.load_project("proj_crash_test")
            assert recovered_data is not None
            recovered_project, recovered_history = recovered_data

            bus2 = CommandBus(
                state=recovered_project,
                history=recovered_history,
                store=store2,
                authorizer=default_authorizer(),
            )
            assert bus2.state_revision == 2
            assert bus2.state_hash == expected_hash
            assert bus2.project.timeline.markers == expected_markers
            assert len(bus2.history) == 2

            # Idempotency replay works across restarts
            replay_res = bus2.dispatch(
                make_command("timeline.mark", "cmd_retry", idempotency_key="key_1")
            )
            assert replay_res.transaction_id == res1.transaction_id
            assert bus2.state_revision == 2
            store2.close()

    def test_replay_transactions_rebuilds_exact_project_state_hash(self) -> None:
        """DurableStudioStore can replay transaction log from revision 0 to verify hash chain."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")
            store = DurableStudioStore(db_path)
            project = make_project("proj_replay_test")
            bus = CommandBus(state=project, store=store, authorizer=default_authorizer())

            bus.dispatch(
                make_command("timeline.mark", "cmd_1", input={"at": "اینجا", "label": "M1"})
            )
            bus.dispatch(
                make_command("timeline.mark", "cmd_2", input={"at": "اینجا", "label": "M2"})
            )
            expected_hash = bus.state_hash

            replayed_project = store.replay_project("proj_replay_test")
            assert replayed_project.state_hash == expected_hash
            assert replayed_project.state_revision == 2
            store.close()

    def test_multi_project_isolation_in_durable_store(self) -> None:
        """DurableStudioStore isolates projects cleanly in SQLite database."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")
            store = DurableStudioStore(db_path)

            p1 = make_project("proj_A")
            p2 = make_project("proj_B")

            bus1 = CommandBus(state=p1, store=store, authorizer=default_authorizer())
            bus2 = CommandBus(state=p2, store=store, authorizer=default_authorizer())

            bus1.dispatch(
                make_command("timeline.mark", "cmd_a", input={"at": "اینجا", "label": "Mark A"})
            )
            bus2.dispatch(
                make_command("timeline.mark", "cmd_b", input={"at": "اینجا", "label": "Mark B"})
            )

            data1 = store.load_project("proj_A")
            data2 = store.load_project("proj_B")

            assert data1 is not None and data2 is not None
            assert data1[0].timeline.markers[0].label == "Mark A"
            assert data2[0].timeline.markers[0].label == "Mark B"
            store.close()

    def test_durable_idempotency_conflict_and_undo_eviction_across_restarts(self) -> None:
        """Durable idempotency detects payload conflict and evicts undone keys across restarts."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")
            store1 = DurableStudioStore(db_path)
            bus1 = CommandBus(
                state=make_project("proj_idem"), store=store1, authorizer=default_authorizer()
            )
            res1 = bus1.dispatch(
                make_command(
                    "timeline.mark",
                    "cmd_1",
                    input={"at": "اینجا", "label": "First"},
                    idempotency_key="idem_shared",
                )
            )
            store1.close()

            # Restart 1: conflict on same key with different payload
            store2 = DurableStudioStore(db_path)
            proj2, hist2 = store2.load_project("proj_idem")  # type: ignore[misc]
            bus2 = CommandBus(
                state=proj2, history=hist2, store=store2, authorizer=default_authorizer()
            )
            with pytest.raises(IdempotencyConflictError):
                bus2.dispatch(
                    make_command(
                        "timeline.mark",
                        "cmd_conflict",
                        input={"at": "اینجا", "label": "Different"},
                        idempotency_key="idem_shared",
                    )
                )

            # Undo the transaction and verify eviction persists across restart 2
            bus2.dispatch(make_command("system.undo", "cmd_undo", input={}))
            store2.close()

            store3 = DurableStudioStore(db_path)
            proj3, hist3 = store3.load_project("proj_idem")  # type: ignore[misc]
            bus3 = CommandBus(
                state=proj3, history=hist3, store=store3, authorizer=default_authorizer()
            )
            res_reapply = bus3.dispatch(
                make_command(
                    "timeline.mark",
                    "cmd_reapply",
                    input={"at": "اینجا", "label": "First"},
                    idempotency_key="idem_shared",
                )
            )
            assert res_reapply.transaction_id != res1.transaction_id
            assert len(bus3.project.timeline.markers) == 1
            store3.close()

    def test_aborted_multi_step_plan_leaves_canonical_state_unchanged_and_records_abort(
        self,
    ) -> None:
        """Partial multi-step plan failure leaves zero state changes and logs an aborted record."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")
            store = DurableStudioStore(db_path)
            project = make_project("proj_abort")
            bus = CommandBus(state=project, store=store, authorizer=default_authorizer())
            initial_hash = bus.state_hash

            cmd_ok = make_command("timeline.mark", "c_ok", input={"at": "اینجا", "label": "Step 1"})
            cmd_fail = make_command(
                "timeline.mark", "c_fail", input={"at": "non_existent_marker", "label": "Step 2"}
            )
            plan = PlanTransaction(plan_id="plan_abort_01", commands=(cmd_ok, cmd_fail))

            with pytest.raises(ReferenceResolutionError):
                bus.dispatch_plan(plan)

            assert bus.state_revision == 0
            assert bus.state_hash == initial_hash
            loaded_proj = store.load_project_state("proj_abort")
            assert loaded_proj is not None
            assert loaded_proj.state_revision == 0
            assert loaded_proj.state_hash == initial_hash
            assert store.load_history("proj_abort") == []

            aborts = store.list_aborted_transactions("proj_abort")
            assert len(aborts) == 1
            assert aborts[0]["plan_id"] == "plan_abort_01"
            assert aborts[0]["state_revision"] == 0
            assert aborts[0]["state_hash"] == initial_hash
            store.close()

    def test_worker_lease_monotonic_fencing_and_stale_worker_rejection(self) -> None:
        """Stale worker with superseded fencing token or expired lease is rejected fail-closed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "studio.sqlite")
            store = DurableStudioStore(db_path)

            lease1 = store.acquire_lease(
                "job_render_1", "attempt_1", "worker_A", ttl_seconds=30.0, now=1000.0
            )
            assert lease1.fencing_token == 1

            # Worker B takes over job_render_1 and receives strictly monotonic fencing_token == 2
            lease2 = store.acquire_lease(
                "job_render_1", "attempt_2", "worker_B", ttl_seconds=30.0, now=1010.0
            )
            assert lease2.fencing_token == 2

            # Stale Worker A (token 1) is rejected fail-closed when trying to commit side effect
            with pytest.raises(StaleFencingTokenError, match="Stale fencing token 1"):
                store.commit_fenced_side_effect(
                    "job_render_1",
                    "attempt_1",
                    lease1.fencing_token,
                    project_id="proj_A",
                    transaction_id="tx_1",
                    command_id="cmd_1",
                    artifact_id="art_stale",
                    result_payload={"status": "stale_write"},
                    now=1015.0,
                )

            # Worker B fails if its lease expires before commit
            with pytest.raises(LeaseExpiredError, match="expired or inactive"):
                store.commit_fenced_side_effect(
                    "job_render_1",
                    "attempt_2",
                    lease2.fencing_token,
                    project_id="proj_A",
                    transaction_id="tx_2",
                    command_id="cmd_1",
                    artifact_id="art_expired",
                    result_payload={"status": "late_write"},
                    now=1050.0,
                )

            # Worker B succeeds while lease is valid (now=1020.0 < 1040.0)
            committed = store.commit_fenced_side_effect(
                "job_render_1",
                "attempt_2",
                lease2.fencing_token,
                project_id="proj_A",
                transaction_id="tx_2",
                command_id="cmd_1",
                artifact_id="art_valid",
                result_payload={"status": "ok"},
                now=1020.0,
            )
            assert committed.status == "completed"
            assert committed.fencing_token == 2

            receipts = store.list_execution_receipts("proj_A")
            assert len(receipts) == 1
            assert receipts[0]["fencing_token"] == 2
            assert receipts[0]["artifact_id"] == "art_valid"
            store.close()
