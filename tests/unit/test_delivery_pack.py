"""Unit tests for ``nexus.color.delivery`` pack operations, permissions, and contracts."""

from __future__ import annotations

import json
import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.delivery.models import (
    DELIVERY_PACKAGE_ID,
    AdjustExposureInput,
    ApplyLutInput,
    AutoBalanceInput,
    ExportOtioInput,
    MakeProxyInput,
    RenderMaster4KInput,
)
from nexus_ai_agent.creative.packs.delivery.operations import (
    build_delivery_registry,
    register_delivery_operations,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionDeniedError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


def _setup_delivery_bus() -> tuple[Project, CommandBus]:
    registry = build_delivery_registry()

    clip_1 = AssetRecord(
        asset_id="clip_master_01",
        media_kind="video",
        content_sha256="sha256:videoclip001",
        duration_us=5_000_000,
    )
    clip_2 = AssetRecord(
        asset_id="clip_master_02",
        media_kind="video",
        content_sha256="sha256:videoclip002",
        duration_us=5_000_000,
    )
    audio_1 = AssetRecord(
        asset_id="audio_voice_01",
        media_kind="audio",
        content_sha256="sha256:audiotrack001",
        duration_us=10_000_000,
    )

    timeline = Timeline(timeline_id="tl_delivery", duration_us=10_000_000)
    project = new_project("p_delivery_01", "Cinema Delivery Project", timeline)
    project = project.model_copy(update={"assets": [clip_1, clip_2, audio_1]})
    bus = CommandBus(project, registry=registry)
    return project, bus


def test_input_validations() -> None:
    # ApplyLutInput: intensity bound [0, 1]
    lut_valid = ApplyLutInput(clip_asset_id="clip_master_01", lut_name="cinematic_warm", intensity=0.8)
    assert lut_valid.intensity == 0.8
    with pytest.raises(ValidationError):
        ApplyLutInput(clip_asset_id="clip_master_01", lut_name="cinematic_warm", intensity=1.5)

    # AdjustExposureInput: EV range [-4, 4]
    exp_valid = AdjustExposureInput(clip_asset_id="clip_master_01", exposure_ev=1.5, temperature_k=5600)
    assert exp_valid.exposure_ev == 1.5
    with pytest.raises(ValidationError):
        AdjustExposureInput(clip_asset_id="clip_master_01", exposure_ev=5.0)

    # RenderMaster4KInput: dimensions must be even
    render_valid = RenderMaster4KInput(width=3840, height=2160, confirmed=True)
    assert render_valid.width == 3840
    with pytest.raises(ValidationError, match="even integers"):
        RenderMaster4KInput(width=3841, height=2160, confirmed=True)


def test_operation_specs_permission_levels() -> None:
    registry = build_delivery_registry()

    lut_spec = registry.get_spec("color.apply_lut")
    assert lut_spec.permission_level == PermissionLevel.REVERSIBLE
    assert DELIVERY_PACKAGE_ID in lut_spec.required_packs

    exp_spec = registry.get_spec("color.adjust_exposure")
    assert exp_spec.permission_level == PermissionLevel.REVERSIBLE

    proxy_spec = registry.get_spec("delivery.make_proxy_480p")
    assert proxy_spec.permission_level == PermissionLevel.IMMEDIATE

    otio_spec = registry.get_spec("delivery.export_otio")
    assert otio_spec.permission_level == PermissionLevel.REVERSIBLE

    master_spec = registry.get_spec("delivery.render_master_4k")
    assert master_spec.permission_level == PermissionLevel.CONFIRMATION


def test_apply_lut_execution() -> None:
    project, bus = _setup_delivery_bus()

    cmd = TypedCommand(
        command_id="cmd_lut_01",
        operation="color.apply_lut",
        input={
            "clip_asset_id": "clip_master_01",
            "lut_name": "fuji_eterna_warm",
            "intensity": 0.85,
            "color_space": "bt709",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    graded_rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert graded_rec.media_kind == "video"
    assert graded_rec.parent_asset_ids == ("clip_master_01",)
    assert graded_rec.provenance["lut_name"] == "fuji_eterna_warm"
    assert graded_rec.provenance["intensity"] == 0.85


def test_adjust_exposure_execution() -> None:
    project, bus = _setup_delivery_bus()

    cmd = TypedCommand(
        command_id="cmd_exp_01",
        operation="color.adjust_exposure",
        input={
            "clip_asset_id": "clip_master_01",
            "exposure_ev": 0.75,
            "contrast": 1.2,
            "temperature_k": 5600,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    derived_id = res.output["asset_id"]

    graded_rec = next(a for a in bus.project.assets if a.asset_id == derived_id)
    assert graded_rec.provenance["exposure_ev"] == 0.75
    assert graded_rec.provenance["temperature_k"] == 5600


def test_make_proxy_execution() -> None:
    project, bus = _setup_delivery_bus()

    cmd = TypedCommand(
        command_id="cmd_proxy_01",
        operation="delivery.make_proxy_480p",
        input={"video_asset_id": "clip_master_01", "resolution": "854x480"},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    proxy_id = res.output["asset_id"]

    proxy_rec = next(a for a in bus.project.assets if a.asset_id == proxy_id)
    assert proxy_rec.provenance["is_proxy"] is True
    assert proxy_rec.provenance["resolution"] == "854x480"


def test_match_shot_execution() -> None:
    project, bus = _setup_delivery_bus()

    cmd = TypedCommand(
        command_id="cmd_match_01",
        operation="color.match_shot",
        input={
            "source_clip_id": "clip_master_01",
            "reference_clip_id": "clip_master_02",
            "match_luminance": True,
            "match_chrominance": True,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    matched_id = res.output["asset_id"]

    matched_rec = next(a for a in bus.project.assets if a.asset_id == matched_id)
    assert matched_rec.parent_asset_ids == ("clip_master_01", "clip_master_02")
    assert matched_rec.provenance["match_luminance"] is True


def test_export_otio_execution() -> None:
    project, bus = _setup_delivery_bus()

    cmd = TypedCommand(
        command_id="cmd_otio_01",
        operation="delivery.export_otio",
        input={"timeline_id": "tl_delivery", "frame_rate": 24.0},
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert res.output["format"] == "otio"
    assert res.output["total_video_clips"] == 2
    assert res.output["total_audio_clips"] == 1

    # Validate standard OpenTimelineIO JSON structure
    otio_doc = json.loads(res.output["otio_json"])
    assert otio_doc["OTIO_SCHEMA"] == "Timeline.1"
    tracks = otio_doc["tracks"]["children"]
    assert len(tracks) == 2
    video_track = next(t for t in tracks if t["kind"] == "Video")
    assert len(video_track["children"]) == 2
    assert video_track["children"][0]["OTIO_SCHEMA"] == "Clip.1"

    # Verify asset record registered in project
    otio_asset = next(a for a in bus.project.assets if a.provenance.get("format") == "otio")
    assert otio_asset.provenance["format"] == "otio"
    assert otio_asset.provenance["frame_rate"] == 24.0


def test_render_master_4k_requires_confirmation() -> None:
    project, bus = _setup_delivery_bus()

    # Missing confirmation -> fails with PermissionDeniedError at Level C gate
    cmd_unconf = TypedCommand(
        command_id="cmd_master_unconf",
        operation="delivery.render_master_4k",
        confirmed=False,
        input={"width": 3840, "height": 2160, "codec": "hevc"},
    )
    with pytest.raises(PermissionDeniedError, match="level C"):
        bus.dispatch(cmd_unconf)

    # Confirmed -> succeeds
    cmd_conf = TypedCommand(
        command_id="cmd_master_ok",
        operation="delivery.render_master_4k",
        confirmed=True,
        input={
            "width": 3840,
            "height": 2160,
            "codec": "hevc",
            "color_space": "bt2020",
            "target_lufs": -14.0,
            "confirmed": True,
        },
    )
    res = bus.dispatch(cmd_conf)
    assert res.status == "applied"
    master_id = res.output["asset_id"]

    master_rec = next(a for a in bus.project.assets if a.asset_id == master_id)
    assert master_rec.provenance["is_master"] is True
    assert master_rec.provenance["width"] == 3840
    assert master_rec.provenance["height"] == 2160
    assert master_rec.provenance["codec"] == "hevc"
    assert master_rec.provenance["color_space"] == "bt2020"
