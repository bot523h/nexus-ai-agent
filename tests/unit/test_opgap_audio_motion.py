"""Unit tests for the task-125 op-gap operations (audio × 4, motion × 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    TypedCommand,
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
    return CommandBus(
        _project_with_media(), registry=build_audio_registry(), allow_experimental=True
    )


def _motion_bus() -> CommandBus:
    return CommandBus(
        _project_with_media(), registry=build_motion_registry(), allow_experimental=True
    )


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


class _DenoiseEngine:
    """In-memory engine double for the denoise contract (no media bytes).

    What the engine *measures* is pinned against real FFmpeg in
    ``tests/unit/test_omega_native_engine.py``; this double proves the handler
    forwards the request and records the measured evidence it was handed.
    """

    def precheck(self, request):  # noqa: ANN001, ANN202 - test double
        from nexus_ai_agent.application.ports.audio_engine import SourcePrecheck

        return SourcePrecheck(
            asset_id=request.asset_id,
            path=f"/media/{request.asset_id}.wav",
            exists=True,
            sha256="sha256:" + "1" * 64,
            readable=True,
        )

    def remove_noise(self, request):  # noqa: ANN001, ANN202
        from nexus_ai_agent.application.ports.audio_engine import ArtifactEvidence

        return ArtifactEvidence(
            asset_id=request.output_asset_id,
            output_path=f"/workspace/media/{request.output_asset_id}.flac",
            sha256="sha256:" + "4" * 64,
            size_bytes=2048,
            duration_us=8_000_000,
            sample_rate=48_000,
            channels=2,
            codec="flac",
            spectral_centroid_hz=980.0,
            rms_dbfs=-29.5,
            crest_factor_db=11.2,
            dynamic_range_db=6.4,
            acoustic_signature_sha256="sha256:" + "5" * 64,
            verified=True,
            passport_id="pp_denoise",
            reused_existing=False,
        )


def _audio_bus_with_engine():  # noqa: ANN202 - test helper
    return CommandBus(
        _project_with_media(),
        registry=build_audio_registry(),
        allow_experimental=True,
        audio_engine=_DenoiseEngine(),
    )


def test_remove_noise_execution() -> None:
    bus = _audio_bus_with_engine()
    result = bus.dispatch(
        TypedCommand(
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
    assert derived.provenance["processor"] == "nagar.audio.studio.denoise.v2"
    assert derived.provenance["artifact_verified"] is True
    assert derived.provenance["claims"].startswith("static broadband noise")
    assert derived.provenance["measured"]["acoustic_signature_sha256"] == "sha256:" + "5" * 64
    assert result.output["derivation"] == "rendered_artifact"
    assert derived.content_sha256 == result.output["content_sha256"]


def test_remove_noise_is_deterministic() -> None:
    payload = {
        "audio_asset_id": "voice_01",
        "strength": 0.8,
        "output_asset_id": "voice_01_clean",
    }
    first = _audio_bus_with_engine().dispatch(
        TypedCommand(command_id="cmd_a", operation="audio.remove_noise", input=payload)
    )
    second = _audio_bus_with_engine().dispatch(
        TypedCommand(command_id="cmd_b", operation="audio.remove_noise", input=payload)
    )
    assert first.output["content_sha256"] == second.output["content_sha256"]


def test_remove_noise_refuses_without_a_media_engine() -> None:
    """The old handler derived a hash from a seed string with no bytes behind it."""
    with pytest.raises(CommandValidationError, match="native audio engine"):
        _audio_bus().dispatch(
            TypedCommand(
                command_id="cmd_denoise_noengine",
                operation="audio.remove_noise",
                input={"audio_asset_id": "voice_01"},
            )
        )


def test_deess_execution() -> None:
    bus = _audio_bus()
    result = bus.dispatch(
        TypedCommand(
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
        TypedCommand(
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
        TypedCommand(
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
                TypedCommand(
                    command_id=f"cmd_{operation}_missing",
                    operation=operation,
                    input={"audio_asset_id": "ghost_track"},
                )
            )
        with pytest.raises(CommandValidationError, match="requires an audio asset"):
            _audio_bus().dispatch(
                TypedCommand(
                    command_id=f"cmd_{operation}_kind",
                    operation=operation,
                    input={"audio_asset_id": "clip_01"},
                )
            )


def test_stabilize_execution() -> None:
    bus = _motion_bus()
    result = bus.dispatch(
        TypedCommand(
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
        TypedCommand(
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
                TypedCommand(
                    command_id=f"cmd_{operation}_missing",
                    operation=operation,
                    input={key: "ghost_clip"},
                )
            )
        with pytest.raises(CommandValidationError, match="requires a video asset"):
            _motion_bus().dispatch(
                TypedCommand(
                    command_id=f"cmd_{operation}_kind",
                    operation=operation,
                    input={key: "voice_01"},
                )
            )
