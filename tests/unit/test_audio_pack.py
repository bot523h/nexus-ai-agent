"""Unit tests for ``nexus.audio.studio`` pack operations, permissions, and contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    BeatGridRef,
    BeatMarker,
    DetectBeatsInput,
)
from nexus_ai_agent.creative.packs.audio.operations import (
    build_audio_registry,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
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


def _setup_audio_bus() -> tuple[Project, CommandBus]:
    registry = build_audio_registry()

    audio_rec = AssetRecord(
        asset_id="music_track_01",
        media_kind="audio",
        content_sha256="sha256:music1234567890",
        duration_us=10_000_000,
    )
    voice_rec = AssetRecord(
        asset_id="voice_track_01",
        media_kind="audio",
        content_sha256="sha256:voice1234567890",
        duration_us=8_000_000,
    )
    clip_1 = AssetRecord(
        asset_id="clip_01",
        media_kind="video",
        content_sha256="sha256:video01",
        duration_us=4_000_000,
    )
    clip_2 = AssetRecord(
        asset_id="clip_02",
        media_kind="video",
        content_sha256="sha256:video02",
        duration_us=4_000_000,
    )

    timeline = Timeline(timeline_id="tl_audio", duration_us=10_000_000)
    project = new_project("test_audio_proj", "Audio Studio Test", timeline)
    project = project.model_copy(update={"assets": [audio_rec, voice_rec, clip_1, clip_2]})
    bus = CommandBus(project, registry=registry, allow_experimental=True)
    return project, bus


def test_detect_beats_input_validation() -> None:
    # valid
    inp = DetectBeatsInput(audio_asset_id="music_track_01", min_bpm=80, max_bpm=160)
    assert inp.min_bpm == 80.0
    assert inp.max_bpm == 160.0

    # invalid: max_bpm <= min_bpm
    with pytest.raises(ValidationError, match="max_bpm"):
        DetectBeatsInput(audio_asset_id="music_track_01", min_bpm=120, max_bpm=100)


def test_beat_marker_and_grid_models() -> None:
    marker = BeatMarker(time_us=500_000, confidence=0.95, beat_number=1, is_downbeat=True)
    assert marker.time_us == 500_000
    assert marker.is_downbeat is True

    grid = BeatGridRef(
        audio_asset_id="music_track_01",
        tempo_bpm=120.0,
        beats=[marker],
        total_beats=1,
    )
    assert grid.tempo_bpm == 120.0
    assert len(grid.beats) == 1
    assert grid.total_beats == 1


def test_operation_specs_permission_levels() -> None:
    registry = build_audio_registry()

    detect_spec = registry.get_spec("audio.detect_beats")
    assert detect_spec.permission_level == PermissionLevel.IMMEDIATE
    assert AUDIO_PACKAGE_ID in detect_spec.required_packs

    norm_spec = registry.get_spec("audio.normalize_loudness")
    assert norm_spec.permission_level == PermissionLevel.CONFIRMATION
    assert AUDIO_PACKAGE_ID in norm_spec.required_packs

    duck_spec = registry.get_spec("audio.duck_music")
    assert duck_spec.permission_level == PermissionLevel.REVERSIBLE

    cut_spec = registry.get_spec("audio.beat_sync_cut")
    assert cut_spec.permission_level == PermissionLevel.REVERSIBLE


def test_detect_beats_execution() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_detect_01",
        operation="audio.detect_beats",
        input={"audio_asset_id": "music_track_01", "sensitivity": 0.8},
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["tempo_bpm"] == 120.0
    assert result.output["total_beats"] > 0
    grid = result.output["beat_grid"]
    assert len(grid["beats"]) == result.output["total_beats"]
    # Check downbeat cadence (beat 1, 5, 9...)
    assert grid["beats"][0]["is_downbeat"] is True
    assert grid["beats"][1]["is_downbeat"] is False


def test_detect_beats_rejects_missing_asset() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_detect_fail",
        operation="audio.detect_beats",
        input={"audio_asset_id": "non_existent_audio"},
    )
    with pytest.raises(CommandValidationError, match="unknown asset"):
        bus.dispatch(cmd)


def test_normalize_loudness_requires_level_c_confirmation() -> None:
    project, bus = _setup_audio_bus()

    # Unconfirmed command -> PermissionDeniedError at gate
    cmd_unconf = TypedCommand(
        command_id="cmd_norm_unconf",
        operation="audio.normalize_loudness",
        confirmed=False,
        input={"audio_asset_id": "music_track_01", "target_lufs": -16.0},
    )
    with pytest.raises(PermissionDeniedError, match="level C"):
        bus.dispatch(cmd_unconf)

    # Confirmed command -> succeeds and creates derived AssetRecord
    cmd_conf = TypedCommand(
        command_id="cmd_norm_ok",
        operation="audio.normalize_loudness",
        confirmed=True,
        input={
            "audio_asset_id": "music_track_01",
            "target_lufs": -14.0,
            "true_peak_db": -1.0,
            "confirmed": True,
        },
    )
    result = bus.dispatch(cmd_conf)
    assert result.status == "applied"
    norm_asset_id = result.output["asset_id"]

    # Verify project has new asset with provenance
    derived_asset = next(a for a in bus.project.assets if a.asset_id == norm_asset_id)
    assert derived_asset.media_kind == "audio"
    assert derived_asset.parent_asset_ids == ("music_track_01",)
    assert derived_asset.provenance["target_lufs"] == -14.0
    assert derived_asset.provenance["true_peak_db"] == -1.0


def test_duck_music_execution() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_duck_01",
        operation="audio.duck_music",
        input={
            "music_asset_id": "music_track_01",
            "voice_asset_id": "voice_track_01",
            "duck_attenuation_db": -14.0,
        },
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    duck_id = result.output["asset_id"]

    derived_asset = next(a for a in bus.project.assets if a.asset_id == duck_id)
    assert derived_asset.parent_asset_ids == ("music_track_01", "voice_track_01")
    assert derived_asset.provenance["duck_attenuation_db"] == -14.0


def test_beat_sync_cut_execution() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_cut_01",
        operation="audio.beat_sync_cut",
        input={
            "audio_asset_id": "music_track_01",
            "clip_asset_ids": ["clip_01", "clip_02"],
            "beats_per_cut": 4,
            "timeline_track_id": "video_main",
        },
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["total_cuts"] == 2
    cuts = result.output["cuts"]
    assert cuts[0]["clip_asset_id"] == "clip_01"
    assert cuts[0]["start_us"] == 0
    assert cuts[0]["duration_us"] == 2_000_000  # 4 beats @ 120 bpm = 2.0s = 2_000_000 us
    assert cuts[1]["clip_asset_id"] == "clip_02"
    assert cuts[1]["start_us"] == 2_000_000


def test_command_bus_idempotency() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_idem_01",
        idempotency_key="key-audio-12345",
        operation="audio.detect_beats",
        input={"audio_asset_id": "music_track_01"},
    )
    res1 = bus.dispatch(cmd)
    res2 = bus.dispatch(cmd)
    assert res1.output == res2.output
    assert res1.transaction_id == res2.transaction_id
