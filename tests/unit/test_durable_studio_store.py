"""Contract and regression tests for DurableStudioStore and CommandBus crash recovery."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    Clip,
    CommandBus,
    MediaRef,
    Playhead,
    Project,
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
            bus1 = CommandBus(state=project1, store=store1)

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

            bus2 = CommandBus(state=recovered_project, history=recovered_history, store=store2)
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
            bus = CommandBus(state=project, store=store)

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

            bus1 = CommandBus(state=p1, store=store)
            bus2 = CommandBus(state=p2, store=store)

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
