"""Unit tests for the task-125 op-gap operations (audio × 4, motion × 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nagar_helpers import authorized_bus, command_for
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    OPERATION_DEESS,
    OPERATION_EQ_VOICE,
    OPERATION_REMOVE_NOISE,
    OPERATION_TIME_STRETCH,
    EqVoiceInput,
)
from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
from nexus_ai_agent.creative.packs.motion.models import (
    MOTION_PACKAGE_ID,
    OPERATION_ADD_PARALLAX,
    OPERATION_STABILIZE,
)
from nexus_ai_agent.creative.packs.motion.operations import build_motion_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
    Timeline,
    new_project,
)

PACKS_ROOT = Path("src/nexus_ai_agent/creative/packs")


def _project_with_media() -> Project:
    audio_rec = AssetRecord(
        asset_id="voice_01",
        media_kind="audio",
        content_sha256="sha256:voicefeed",
        duration_us=8_000_000,
    )
    clip_rec = AssetRecord(
        asset_id="clip_01",
        media_kind="video",
        content_sha256="sha256:videofeed",
        duration_us=4_000_000,
    )
    timeline = Timeline(timeline_id="tl_opgap", duration_us=8_000_000)
    project = new_project("test_opgap", "Op-gap Test", timeline)
    return project.model_copy(update={"assets": [audio_rec, clip_rec]})


def _audio_bus() -> CommandBus:
    return authorized_bus(_project_with_media(), registry=build_audio_registry())


def _motion_bus() -> CommandBus:
    return authorized_bus(_project_with_media(), registry=build_motion_registry())


def _derived(bus: CommandBus, asset_id: str) -> AssetRecord:
    matches = [a for a in bus.project.assets if a.asset_id == asset_id]
    assert len(matches) == 1
    return matches[0]


def test_new_operation_specs_permission_levels() -> None:
    audio_registry = build_audio_registry()
    for operation_id in (
        OPERATION_REMOVE_NOISE,
        OPERATION_DEESS,
        OPERATION_EQ_VOICE,
        OPERATION_TIME_STRETCH,
    ):
        spec = audio_registry.get_spec(operation_id)
        assert spec.permission_level == PermissionLevel.REVERSIBLE
        assert AUDIO_PACKAGE_ID in spec.required_packs
        assert spec.deterministic is True

    motion_registry = build_motion_registry()
    for operation_id in (OPERATION_STABILIZE, OPERATION_ADD_PARALLAX):
        spec = motion_registry.get_spec(operation_id)
        assert spec.permission_level == PermissionLevel.REVERSIBLE
        assert MOTION_PACKAGE_ID in spec.required_packs
        assert spec.deterministic is True


def test_manifests_advertise_new_capabilities() -> None:
    audio_manifest = json.loads(
        (PACKS_ROOT / "audio" / "pack.manifest.json").read_text(encoding="utf-8")
    )
    for cap in (
        "audio.remove_noise",
        "audio.deess",
        "audio.eq_voice",
        "audio.time_stretch",
    ):
        assert cap in audio_manifest["capabilities"]

    motion_manifest = json.loads(
        (PACKS_ROOT / "motion" / "pack.manifest.json").read_text(encoding="utf-8")
    )
    assert "motion.stabilize" in motion_manifest["capabilities"]
    assert "motion.add_parallax" in motion_manifest["capabilities"]


def test_remove_noise_execution() -> None:
    bus = _audio_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_denoise_01",
            operation="audio.remove_noise",
            input={
                "audio_asset_id": "voice_01",
                "strength": 0.8,
                "output_asset_id": "voice_01_clean",
            },
        )
    )
    assert result.status == "applied"
    assert result.output["asset_id"] == "voice_01_clean"
    assert result.output["source_asset_id"] == "voice_01"

    derived = _derived(bus, "voice_01_clean")
    assert derived.media_kind == "audio"
    assert derived.duration_us == 8_000_000
    assert derived.parent_asset_ids == ("voice_01",)
    assert derived.provenance["processor"] == "nagar.audio.studio.denoise.v1"
    assert derived.content_sha256 == result.output["content_sha256"]


def test_remove_noise_is_deterministic() -> None:
    payload = {
        "audio_asset_id": "voice_01",
        "strength": 0.8,
        "output_asset_id": "voice_01_clean",
    }
    first = _audio_bus().dispatch(
        command_for("test_opgap", command_id="cmd_a", operation="audio.remove_noise", input=payload)
    )
    second = _audio_bus().dispatch(
        command_for("test_opgap", command_id="cmd_b", operation="audio.remove_noise", input=payload)
    )
    assert first.output["content_sha256"] == second.output["content_sha256"]


def test_deess_execution() -> None:
    bus = _audio_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_deess_01",
            operation="audio.deess",
            input={
                "audio_asset_id": "voice_01",
                "frequency_hz": 7000.0,
                "threshold_db": -18.0,
                "output_asset_id": "voice_01_deessed",
            },
        )
    )
    assert result.status == "applied"
    assert result.output["frequency_hz"] == 7000.0
    assert result.output["threshold_db"] == -18.0
    derived = _derived(bus, "voice_01_deessed")
    assert derived.media_kind == "audio"
    assert derived.provenance["processor"] == "nagar.audio.studio.deess.v1"


def test_eq_voice_applies_preset_plus_master_gain() -> None:
    bus = _audio_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_eq_01",
            operation="audio.eq_voice",
            input={
                "audio_asset_id": "voice_01",
                "preset": "broadcast",
                "gain_db": 1.0,
                "output_asset_id": "voice_01_eq",
            },
        )
    )
    assert result.status == "applied"
    # broadcast body_250hz=1.5 + master 1.0
    assert result.output["applied_bands"]["body_250hz"] == 2.5
    assert result.output["applied_bands"]["presence_3khz"] == 3.0
    assert result.output["cuts"] == ["high_pass_80hz"]
    derived = _derived(bus, "voice_01_eq")
    assert derived.provenance["processor"] == "nagar.audio.studio.eq.v1"


def test_eq_voice_rejects_unknown_preset() -> None:
    with pytest.raises(ValidationError, match="unknown voice EQ preset"):
        EqVoiceInput(audio_asset_id="voice_01", preset="rock-concert")


def test_time_stretch_scales_duration() -> None:
    bus = _audio_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_stretch_01",
            operation="audio.time_stretch",
            input={
                "audio_asset_id": "voice_01",
                "factor": 0.5,
                "preserve_pitch": False,
                "output_asset_id": "voice_01_fast",
            },
        )
    )
    assert result.status == "applied"
    assert result.output["new_duration_us"] == 4_000_000
    derived = _derived(bus, "voice_01_fast")
    assert derived.duration_us == 4_000_000
    assert derived.provenance["processor"] == "nagar.audio.studio.stretch.v1"


def test_audio_gap_ops_reject_unknown_and_wrong_kind_assets() -> None:
    for operation in (
        "audio.remove_noise",
        "audio.deess",
        "audio.eq_voice",
        "audio.time_stretch",
    ):
        with pytest.raises(CommandValidationError, match="unknown audio asset"):
            _audio_bus().dispatch(
                command_for(
                    "test_opgap",
                    command_id=f"cmd_{operation}_missing",
                    operation=operation,
                    input={"audio_asset_id": "ghost_track"},
                )
            )
        with pytest.raises(CommandValidationError, match="requires an audio asset"):
            _audio_bus().dispatch(
                command_for(
                    "test_opgap",
                    command_id=f"cmd_{operation}_kind",
                    operation=operation,
                    input={"audio_asset_id": "clip_01"},
                )
            )


def test_stabilize_execution() -> None:
    bus = _motion_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_stab_01",
            operation="motion.stabilize",
            input={
                "clip_asset_id": "clip_01",
                "strength": 0.9,
                "crop_mode": "static",
                "output_asset_id": "clip_01_stable",
            },
        )
    )
    assert result.status == "applied"
    assert result.output["crop_mode"] == "static"
    derived = _derived(bus, "clip_01_stable")
    assert derived.media_kind == "video"
    assert derived.duration_us == 4_000_000
    assert derived.provenance["processor"] == "nagar.motion.graphics.stabilize.v1"


def test_add_parallax_layer_plan() -> None:
    bus = _motion_bus()
    result = bus.dispatch(
        command_for(
            "test_opgap",
            command_id="cmd_par_01",
            operation="motion.add_parallax",
            input={
                "clip_asset_id": "clip_01",
                "depth_layers": 4,
                "intensity": 0.6,
                "direction": "vertical",
                "output_asset_id": "clip_01_parallax",
            },
        )
    )
    assert result.status == "applied"
    plan = result.output["layer_plan"]
    assert len(plan) == 4
    # foreground carries the full offset, background carries zero
    assert plan[0]["offset_factor"] == 0.6
    assert plan[-1]["offset_factor"] == 0.0
    assert all(layer["axis"] == "vertical" for layer in plan)
    derived = _derived(bus, "clip_01_parallax")
    assert derived.provenance["processor"] == "nagar.motion.graphics.parallax.v1"


def test_motion_gap_ops_reject_unknown_and_wrong_kind_assets() -> None:
    for operation in ("motion.stabilize", "motion.add_parallax"):
        key = "clip_asset_id"
        with pytest.raises(CommandValidationError, match="unknown video asset"):
            _motion_bus().dispatch(
                command_for(
                    "test_opgap",
                    command_id=f"cmd_{operation}_missing",
                    operation=operation,
                    input={key: "ghost_clip"},
                )
            )
        with pytest.raises(CommandValidationError, match="requires a video asset"):
            _motion_bus().dispatch(
                command_for(
                    "test_opgap",
                    command_id=f"cmd_{operation}_kind",
                    operation=operation,
                    input={key: "voice_01"},
                )
            )
