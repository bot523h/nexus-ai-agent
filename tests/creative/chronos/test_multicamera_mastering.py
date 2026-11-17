"""Unified camera/acoustic/motion mastering through persisted Chronos receipts."""

from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import (
    build_default_chronos_handlers,
    merkle_ledger,
)
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import AcousticRoomSpec
from nexus_ai_agent.creative.chronos.chronos_port import ChronosCommand, ChronosPort
from nexus_ai_agent.creative.chronos.contracts import KernelError
from nexus_ai_agent.creative.chronos.multicamera_mastering import (
    MultiCameraShotInput,
    execute_multicamera_mastering,
    execute_multicamera_mastering_manifest,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _fixture_shot(shot_index: int, seed: int = 801) -> MultiCameraShotInput:
    rng = np.random.default_rng(seed + shot_index)
    reference_frames = np.full((2, 16, 16, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    sensor_matrix = np.array(
        [[0.92, 0.025, 0.01], [0.02, 0.91, 0.025], [0.015, 0.025, 0.90]],
        dtype=np.float64,
    )
    sensor_offset = np.array([0.008, 0.012, 0.01])
    source_frames = (reference_frames - sensor_offset) @ np.linalg.inv(sensor_matrix)
    source_chart = rng.uniform(0.05, 0.45, size=(32, 3))
    reference_chart = source_chart @ sensor_matrix + sensor_offset
    landmarks = np.array(
        [
            [-0.060, -0.050, 0.000],
            [0.060, -0.050, 0.000],
            [-0.050, 0.050, 0.000],
            [0.050, 0.050, 0.000],
            [-0.025, 0.000, 0.015],
            [0.025, 0.000, -0.015],
        ],
        dtype=np.float64,
    )
    trajectory = np.stack((landmarks, landmarks + np.array([0.001, 0.0, 0.0])))
    screen_velocity = np.full((2, 6, 2), (0.15, 0.0), dtype=np.float64)
    points_a = np.array([[2.0, 2.0], [5.0, 4.0], [9.0, 8.0], [12.0, 13.0]])
    points_b = points_a + np.array([[0.0, 0.4], [0.0, -0.3], [0.0, 0.2], [0.0, -0.1]])
    intrinsics = np.array(
        [[800.0, 0.0, 8.0], [0.0, 800.0, 8.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    audio = 0.025 * np.sin(2.0 * np.pi * 440.0 * np.arange(4004) / 48_000)
    return MultiCameraShotInput(
        episode_index=1,
        shot_index=shot_index,
        camera_a_frames_linear_rgb=reference_frames,
        camera_b_frames_linear_rgb=source_frames,
        intrinsics_a=intrinsics,
        intrinsics_b=intrinsics,
        rotation_ab=np.eye(3),
        translation_ab=np.array([0.2, 0.0, 0.0]),
        points_a_px=points_a,
        points_b_px=points_b,
        source_sensor_chart_rgb=source_chart,
        reference_chart_rgb=reference_chart,
        focal_length_mm=35.0,
        reference_focus_distance_mm=3000.0,
        focus_distance_mm=2500.0,
        principal_point_px=(7.5, 7.5),
        camera_velocity_px_per_frame=(0.2, -0.1),
        readout_fraction=0.6,
        frame_rate_fps=24.0,
        landmarks_3d=trajectory + (shot_index - 1) * np.array([0.002, 0.0, 0.0]),
        screen_velocities_px_s=screen_velocity,
        camera_position_m=np.array([3.0 + shot_index, 5.0, 2.0]),
        camera_to_world_rotation=np.eye(3),
        dialogue_pcm_mono=audio,
    )


def _manifest_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _manifest_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_manifest_value(item) for item in value]
    return value


def _manifest_payload(shots: tuple[MultiCameraShotInput, ...]) -> dict[str, Any]:
    room = {
        "room_id": "sound-stage",
        "dimensions_m": [12.0, 10.0, 4.0],
        "wall_absorption": [0.25] * 6,
        "air_attenuation_per_m": 0.0,
    }
    return {
        "room": room,
        "speaker_position_m": [3.0, 5.0, 2.0],
        "sample_rate_hz": 48_000,
        "max_reflection_order": 2,
        "shots": [
            _manifest_value({key: getattr(shot, key) for key in shot.__dataclass_fields__})
            for shot in shots
        ],
    }


def test_complete_mastering_bridge_persists_stereo_media_and_merkle_proofs(
    tmp_path: Path,
) -> None:
    shots = (_fixture_shot(1), _fixture_shot(2))
    manifest_path = tmp_path / "mastering-manifest.json"
    manifest_path.write_text(json.dumps(_manifest_payload(shots)), encoding="utf-8")
    output_path = tmp_path / "bridge-output" / "receipt.json"
    workspace = tmp_path / "persisted-master"
    command = ChronosCommand(
        "episode.run_verified",
        (str(manifest_path),),
        {
            "continuity_engine": "multicamera_mastering",
            "staging_dir": str(workspace),
            "output": str(output_path),
        },
    )
    port = ChronosPort(build_default_chronos_handlers())
    bridge_receipt = port.execute(command)
    summary = json.loads(output_path.read_text(encoding="utf-8"))
    assert bridge_receipt.operation == "episode.run_verified"
    assert bridge_receipt.sha256 == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert bridge_receipt.quality_claim == "MULTICAMERA_MASTERING_CONTINUITY"
    assert summary["continuity"] == "OK"
    assert summary["shot_count"] == 2
    assert summary["cut_count"] == 1
    assert summary["anchored_sample_clock_drift"] == 0
    assert summary["sample_count"] == summary["exact_sample_horizon"] == 8008
    assert summary["rt60_max_spread_seconds"] == 0.0
    assert summary["drr_monotonic_with_proximity"] is True
    assert len(summary["merkle_root_sha256"]) == 64
    assert summary["muxed_artifact_count"] in (0, 2)
    fs = WorkspaceFilesystem(workspace)
    persisted = execute_multicamera_mastering_manifest(manifest_path, tmp_path / "second-run")
    assert persisted.persisted_season.merkle_root_sha256 == summary["merkle_root_sha256"]
    assert len(persisted.shots) == 2
    for shot in persisted.shots:
        assert np.max(shot.epipolar_audit.sampson_distance_squared_after) < 1e-10
        assert shot.color_calibration.delta_e00_after_max < 1e-3
        assert len(shot.focus_receipts) == 2
        assert len(shot.rolling_shutter_receipts) == 2
        assert shot.room_audio.sample_drift == 0
        assert shot.persisted_shot.audio_channel_count == 2
        assert (
            shot.persisted_shot.audio_samples
            == shot.persisted_shot.sample_end - shot.persisted_shot.sample_start
        )
        with wave.open(str(fs.resolve(shot.persisted_shot.audio_path)), "rb") as reader:
            assert reader.getnchannels() == 2
            assert reader.getnframes() == shot.persisted_shot.audio_samples
            assert reader.getframerate() == 48_000
        assert merkle_ledger.verify_rfc6962_bundle(shot.persisted_shot.inclusion_bundle)
        assert shot.persisted_shot.inclusion_bundle["root_sha256"] == summary["merkle_root_sha256"]
        assert max(shot.persisted_shot.restored_delta_e00_by_frame) < 0.25


def test_direct_engine_receipt_summary_is_json_safe(tmp_path: Path) -> None:
    room_payload = _manifest_payload((_fixture_shot(1), _fixture_shot(2)))
    receipt = execute_multicamera_mastering_manifest(
        _write_manifest(tmp_path, room_payload), tmp_path / "direct-workspace"
    )
    encoded = json.dumps(receipt.summary(), sort_keys=True)
    assert "MULTICAMERA" not in encoded
    assert receipt.summary()["continuity"] == "OK"
    assert len(receipt.cut_receipts) == 1


def _write_manifest(root: Path, payload: dict[str, Any]) -> Path:
    path = root / "direct-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_mastering_refuses_invalid_grid_and_stereo_buffers(tmp_path: Path) -> None:
    shots = (_fixture_shot(1), _fixture_shot(2))
    room = AcousticRoomSpec("sound-stage", (12.0, 10.0, 4.0), (0.25, 0.25, 0.25, 0.25, 0.25, 0.25))
    with pytest.raises(ValueError):
        execute_multicamera_mastering(
            shots[:1],
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "one-shot",
        )
    malformed = replace(
        shots[0],
        camera_b_frames_linear_rgb=np.full_like(shots[0].camera_b_frames_linear_rgb, np.nan),
    )
    with pytest.raises(ValueError):
        execute_multicamera_mastering(
            (malformed, shots[1]),
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-frame",
        )
    with pytest.raises(ValueError):
        execute_multicamera_mastering(
            shots,
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-rate",
            sample_rate_hz=44_100,
        )
    with pytest.raises(ValueError):
        execute_multicamera_mastering(
            (replace(shots[0], shot_index=0), shots[1]),
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-index",
        )
    with pytest.raises(ValueError):
        execute_multicamera_mastering(
            (replace(shots[0], frame_rate_fps=0.0), shots[1]),
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-frame-rate",
        )
    smaller = replace(
        shots[0],
        camera_a_frames_linear_rgb=shots[0].camera_a_frames_linear_rgb[:, :12],
        camera_b_frames_linear_rgb=shots[0].camera_b_frames_linear_rgb[:, :12],
    )
    with pytest.raises(ValueError, match="dimensions"):
        execute_multicamera_mastering(
            (smaller, shots[1]),
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-dimensions",
        )
    with pytest.raises(ValueError, match="ordered"):
        execute_multicamera_mastering(
            tuple(reversed(shots)),
            room=room,
            speaker_position_m=np.array([3.0, 5.0, 2.0]),
            workspace_root=tmp_path / "bad-order",
        )
    split_episodes = (
        replace(shots[0], episode_index=1, shot_index=1),
        replace(shots[1], episode_index=2, shot_index=1),
    )
    split_receipt = execute_multicamera_mastering(
        split_episodes,
        room=room,
        speaker_position_m=np.array([3.0, 5.0, 2.0]),
        workspace_root=tmp_path / "two-episodes",
        ffmpeg_path="",
    )
    assert split_receipt.cut_receipts == ()


def test_mastering_manifest_refuses_bad_schema_and_bridge_selector(
    tmp_path: Path,
) -> None:
    invalid_manifest = tmp_path / "bad.json"
    invalid_manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        execute_multicamera_mastering_manifest(invalid_manifest, tmp_path / "bad")
    with pytest.raises(ValueError):
        execute_multicamera_mastering_manifest(tmp_path / "missing.json", tmp_path / "bad")
    malformed_payload = _manifest_payload((_fixture_shot(1), _fixture_shot(2)))
    malformed_payload["room"] = []
    with pytest.raises(ValueError, match="room spec"):
        execute_multicamera_mastering_manifest(
            _write_manifest(tmp_path, malformed_payload), tmp_path / "bad-room"
        )
    malformed_payload["room"] = {
        "room_id": "stage",
        "dimensions_m": [12.0, 10.0, 4.0],
        "wall_absorption": [0.25] * 6,
    }
    malformed_payload["shots"] = [None]
    with pytest.raises(ValueError, match="shot must be an object"):
        execute_multicamera_mastering_manifest(
            _write_manifest(tmp_path, malformed_payload), tmp_path / "bad-shot"
        )
    malformed_payload["shots"] = []
    malformed_payload["show_bible_anchor"] = "not-an-object"
    with pytest.raises(ValueError, match="anchor must be an object"):
        execute_multicamera_mastering_manifest(
            _write_manifest(tmp_path, malformed_payload), tmp_path / "bad-anchor"
        )
    with pytest.raises(ValueError, match="omitted"):
        execute_multicamera_mastering_manifest(
            _write_manifest(tmp_path, {"shots": []}), tmp_path / "missing-fields"
        )
    command = ChronosCommand(
        "episode.run_verified",
        (str(invalid_manifest),),
        {"continuity_engine": "unrecognized"},
    )
    with pytest.raises(KernelError):
        ChronosPort(build_default_chronos_handlers()).execute(command)
