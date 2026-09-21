"""Unit tests for ``nexus.edit.timeline`` pack operations, permissions, and contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.edit.models import (
    EDIT_PACKAGE_ID,
    AttachBRollInput,
    FreezeFrameInput,
    InsertGapInput,
    RetimeToMusicInput,
    ReverseSegmentInput,
    RippleDeleteInput,
    SpeedRampInput,
    TrimInput,
)
from nexus_ai_agent.creative.packs.edit.operations import (
    build_edit_registry,
    register_edit_operations,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


def _setup_edit_bus() -> tuple[Project, CommandBus]:
    registry = build_edit_registry()

    clip_1 = AssetRecord(
        asset_id="clip_main_01",
        media_kind="video",
        content_sha256="sha256:mainvideo01",
        duration_us=10_000_000,
    )
    b_roll = AssetRecord(
        asset_id="clip_broll_01",
        media_kind="video",
        content_sha256="sha256:brollvideo01",
        duration_us=4_000_000,
    )
    audio = AssetRecord(
        asset_id="music_track_01",
        media_kind="audio",
        content_sha256="sha256:musicaudio01",
        duration_us=20_000_000,
    )

    timeline = Timeline(timeline_id="tl_edit", duration_us=15_000_000)
    project = new_project("p_edit_01", "Timeline Edit Project", timeline)
    project = project.model_copy(update={"assets": [clip_1, b_roll, audio]})
    bus = CommandBus(project, registry=registry)
    return project, bus


def test_input_validations() -> None:
    # TrimInput: out_point > in_point
    valid_trim = TrimInput(clip_asset_id="clip_main_01", in_point_us=1_000_000, out_point_us=4_000_000)
    assert valid_trim.in_point_us == 1_000_000
    with pytest.raises(ValidationError, match="out_point_us"):
        TrimInput(clip_asset_id="clip_main_01", in_point_us=4_000_000, out_point_us=2_000_000)

    # SpeedRampInput: speed_factor bounds
    valid_speed = SpeedRampInput(clip_asset_id="clip_main_01", speed_factor=2.0)
    assert valid_speed.speed_factor == 2.0
    with pytest.raises(ValidationError):
        SpeedRampInput(clip_asset_id="clip_main_01", speed_factor=0.01)


def test_operation_specs_permission_levels() -> None:
    registry = build_edit_registry()

    for op in (
        "timeline.trim",
        "timeline.ripple_delete",
        "timeline.insert_gap",
        "timeline.speed_ramp",
        "timeline.reverse_segment",
        "timeline.freeze_frame",
        "timeline.attach_b_roll",
        "timeline.retime_to_music",
    ):
        spec = registry.get_spec(op)
        assert spec.permission_level == PermissionLevel.REVERSIBLE
        assert EDIT_PACKAGE_ID in spec.required_packs


def test_trim_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_trim_01",
        operation="timeline.trim",
        input={
            "clip_asset_id": "clip_main_01",
            "in_point_us": 2_000_000,
            "out_point_us": 6_000_000,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    trimmed = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert trimmed.duration_us == 4_000_000
    assert trimmed.provenance["in_point_us"] == 2_000_000
    assert trimmed.provenance["out_point_us"] == 6_000_000


def test_ripple_delete_execution() -> None:
    project, bus = _setup_edit_bus()

    initial_duration = bus.project.timeline.duration_us
    cmd = TypedCommand(
        command_id="cmd_ripple_01",
        operation="timeline.ripple_delete",
        input={"track_id": "video_main", "start_us": 3_000_000, "duration_us": 2_000_000},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert bus.project.timeline.duration_us == initial_duration - 2_000_000


def test_insert_gap_execution() -> None:
    project, bus = _setup_edit_bus()

    initial_duration = bus.project.timeline.duration_us
    cmd = TypedCommand(
        command_id="cmd_gap_01",
        operation="timeline.insert_gap",
        input={"track_id": "video_main", "at_us": 5_000_000, "duration_us": 3_000_000},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert bus.project.timeline.duration_us == initial_duration + 3_000_000


def test_speed_ramp_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_speed_01",
        operation="timeline.speed_ramp",
        input={"clip_asset_id": "clip_main_01", "speed_factor": 2.0, "maintain_pitch": True},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    ramped_id = res.output["asset_id"]

    ramped = next(a for a in bus.project.assets if a.asset_id == ramped_id)
    assert ramped.duration_us == 5_000_000  # 10s / 2.0x = 5s
    assert ramped.provenance["speed_factor"] == 2.0


def test_reverse_segment_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_rev_01",
        operation="timeline.reverse_segment",
        input={"clip_asset_id": "clip_main_01"},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    rev_id = res.output["asset_id"]

    rev_rec = next(a for a in bus.project.assets if a.asset_id == rev_id)
    assert rev_rec.provenance["playback_rate"] == -1.0


def test_freeze_frame_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_freeze_01",
        operation="timeline.freeze_frame",
        input={"clip_asset_id": "clip_main_01", "freeze_at_us": 2_500_000, "duration_us": 4_000_000},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    freeze_id = res.output["asset_id"]

    freeze_rec = next(a for a in bus.project.assets if a.asset_id == freeze_id)
    assert freeze_rec.duration_us == 4_000_000
    assert freeze_rec.provenance["freeze_at_us"] == 2_500_000


def test_attach_b_roll_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_broll_01",
        operation="timeline.attach_b_roll",
        input={
            "main_clip_id": "clip_main_01",
            "b_roll_asset_id": "clip_broll_01",
            "start_offset_us": 1_000_000,
            "duration_us": 3_000_000,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    broll_id = res.output["asset_id"]

    broll_rec = next(a for a in bus.project.assets if a.asset_id == broll_id)
    assert broll_rec.parent_asset_ids == ("clip_main_01", "clip_broll_01")
    assert broll_rec.provenance["start_offset_us"] == 1_000_000


def test_retime_to_music_execution() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_retime_01",
        operation="timeline.retime_to_music",
        input={
            "clip_asset_ids": ["clip_main_01", "clip_broll_01"],
            "audio_asset_id": "music_track_01",
            "tempo_bpm": 120.0,
            "beats_per_cut": 4,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert res.output["total_cuts"] == 2
    cuts = res.output["cut_plan"]
    assert cuts[0]["start_us"] == 0
    assert cuts[0]["duration_us"] == 2_000_000  # 4 beats @ 120 bpm = 2.0s


def test_reversible_undo() -> None:
    project, bus = _setup_edit_bus()

    cmd = TypedCommand(
        command_id="cmd_trim_undo",
        operation="timeline.trim",
        input={"clip_asset_id": "clip_main_01", "in_point_us": 1_000_000, "out_point_us": 5_000_000},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    trimmed_id = res.output["asset_id"]
    assert trimmed_id in [a.asset_id for a in bus.project.assets]

    undo_cmd = TypedCommand(command_id="cmd_undo", operation="system.undo", input={})
    undo_res = bus.dispatch(undo_cmd)
    assert undo_res.status == "applied"
    assert trimmed_id not in [a.asset_id for a in bus.project.assets]
