"""Unit tests for ``nexus.motion.graphics`` pack operations, permissions, and contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.motion.models import (
    MOTION_PACKAGE_ID,
    AddTransitionInput,
    KeyframeTransformInput,
    TransformKeyframe,
)
from nexus_ai_agent.creative.packs.motion.operations import (
    build_motion_registry,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


def _setup_motion_bus() -> tuple[Project, CommandBus]:
    registry = build_motion_registry()

    clip_a = AssetRecord(
        asset_id="clip_scene_a",
        media_kind="video",
        content_sha256="sha256:scene_a_hash",
        duration_us=4_000_000,
    )
    clip_b = AssetRecord(
        asset_id="clip_scene_b",
        media_kind="video",
        content_sha256="sha256:scene_b_hash",
        duration_us=4_000_000,
    )

    timeline = Timeline(timeline_id="tl_motion", duration_us=8_000_000)
    project = new_project("p_motion_01", "Motion Graphics Project", timeline)
    project = project.model_copy(update={"assets": [clip_a, clip_b]})
    bus = CommandBus(project, registry=registry)
    return project, bus


def test_input_validations() -> None:
    # AddTransitionInput: valid duration and easing
    t_valid = AddTransitionInput(
        left_clip_id="clip_scene_a", right_clip_id="clip_scene_b", duration_us=600_000
    )
    assert t_valid.duration_us == 600_000
    with pytest.raises(ValidationError):
        AddTransitionInput(
            left_clip_id="clip_scene_a", right_clip_id="clip_scene_b", duration_us=100
        )

    # KeyframeTransformInput: keyframes must be monotonic
    kf1 = TransformKeyframe(time_offset_us=0, scale=1.0)
    kf2 = TransformKeyframe(time_offset_us=500_000, scale=1.2)
    kf_bad = TransformKeyframe(time_offset_us=200_000, scale=1.1)

    # Monotonic -> valid
    KeyframeTransformInput(clip_asset_id="clip_scene_a", keyframes=[kf1, kf2])
    # Non-monotonic -> raises ValidationError
    with pytest.raises(ValidationError, match="monotonically"):
        KeyframeTransformInput(clip_asset_id="clip_scene_a", keyframes=[kf1, kf2, kf_bad])


def test_operation_specs_permission_levels() -> None:
    registry = build_motion_registry()

    for op in (
        "motion.add_transition",
        "motion.keyframe_transform",
        "motion.add_glow",
        "motion.add_motion_blur",
        "motion.add_title",
    ):
        spec = registry.get_spec(op)
        assert spec.permission_level == PermissionLevel.REVERSIBLE
        assert MOTION_PACKAGE_ID in spec.required_packs


def test_add_transition_execution() -> None:
    project, bus = _setup_motion_bus()

    cmd = TypedCommand(
        command_id="cmd_trans_01",
        operation="motion.add_transition",
        input={
            "left_clip_id": "clip_scene_a",
            "right_clip_id": "clip_scene_b",
            "kind": "crossfade",
            "duration_us": 750_000,
            "easing": "smoothstep",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    trans_rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert trans_rec.media_kind == "video"
    assert trans_rec.duration_us == 750_000
    assert trans_rec.parent_asset_ids == ("clip_scene_a", "clip_scene_b")
    assert trans_rec.provenance["kind"] == "crossfade"


def test_keyframe_transform_execution() -> None:
    project, bus = _setup_motion_bus()

    kf1 = TransformKeyframe(time_offset_us=0, scale=1.0, position_x=0.0).model_dump()
    kf2 = TransformKeyframe(time_offset_us=1_000_000, scale=1.15, position_x=0.2).model_dump()

    cmd = TypedCommand(
        command_id="cmd_kf_01",
        operation="motion.keyframe_transform",
        input={
            "clip_asset_id": "clip_scene_a",
            "keyframes": [kf1, kf2],
            "easing": "ease_in_out",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert rec.provenance["keyframe_count"] == 2
    assert rec.parent_asset_ids == ("clip_scene_a",)


def test_add_glow_execution() -> None:
    project, bus = _setup_motion_bus()

    cmd = TypedCommand(
        command_id="cmd_glow_01",
        operation="motion.add_glow",
        input={
            "clip_asset_id": "clip_scene_a",
            "radius_px": 25.0,
            "intensity": 1.2,
            "threshold": 0.6,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert rec.provenance["radius_px"] == 25.0
    assert rec.provenance["intensity"] == 1.2


def test_add_motion_blur_execution() -> None:
    project, bus = _setup_motion_bus()

    cmd = TypedCommand(
        command_id="cmd_blur_01",
        operation="motion.add_motion_blur",
        input={
            "clip_asset_id": "clip_scene_a",
            "shutter_angle_deg": 270.0,
            "samples": 12,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert rec.provenance["shutter_angle_deg"] == 270.0
    assert rec.provenance["samples"] == 12


def test_add_title_execution() -> None:
    project, bus = _setup_motion_bus()

    cmd = TypedCommand(
        command_id="cmd_title_01",
        operation="motion.add_title",
        input={
            "text": "استودیوی خلاق نگار",
            "animation_style": "kinetic_pop",
            "font_name": "Vazirmatn",
            "font_size": 72,
            "duration_us": 4_000_000,
            "position": "lower_third",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert rec.provenance["text"] == "استودیوی خلاق نگار"
    assert rec.provenance["animation_style"] == "kinetic_pop"
    assert rec.provenance["font_name"] == "Vazirmatn"
    assert rec.duration_us == 4_000_000


def test_reversible_undo() -> None:
    project, bus = _setup_motion_bus()

    cmd = TypedCommand(
        command_id="cmd_trans_undo",
        operation="motion.add_transition",
        input={
            "left_clip_id": "clip_scene_a",
            "right_clip_id": "clip_scene_b",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    trans_id = res.output["asset_id"]
    assert trans_id in [a.asset_id for a in bus.project.assets]

    undo_cmd = TypedCommand(command_id="cmd_undo", operation="system.undo", input={})
    undo_res = bus.dispatch(undo_cmd)
    assert undo_res.status == "applied"
    assert trans_id not in [a.asset_id for a in bus.project.assets]
