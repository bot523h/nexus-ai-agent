"""Sixty typed fail-closed probes for Chronos media and spine boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import (
    ChronosSpineAttempt,
    ChronosSpineExecutionError,
    ChronosVerificationRefusalError,
    acoustic_space_continuity,
    build_default_chronos_handlers,
    compute_chronos_recipe_identity,
    dispatch_chronos_spine_operation,
)
from nexus_ai_agent.creative.chronos import (
    real_media_continuity as real_media,
)
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import AcousticRoomSpec
from nexus_ai_agent.creative.chronos.chronos_port import ChronosCommand, ChronosPort
from nexus_ai_agent.creative.chronos.contracts import KernelError
from nexus_ai_agent.creative.chronos.kinetic_match_cut_engine import (
    estimate_subpixel_phase_translation,
    optimize_match_on_action_cut,
    rectify_rolling_shutter_frame,
)
from nexus_ai_agent.creative.chronos.multicam_epipolar_matching import (
    audit_and_project_epipolar_correspondences,
    compensate_focus_breathing_frame,
    compute_fundamental_matrix_from_rig,
    compute_physical_circle_of_confusion_mm,
    harmonize_multicam_sensor_color,
    sampson_epipolar_distance_squared,
)
from nexus_ai_agent.creative.chronos.multicamera_mastering import (
    MultiCameraShotInput,
    execute_multicamera_mastering,
    execute_multicamera_mastering_manifest,
)
from nexus_ai_agent.creative.chronos.nexus_spine_adapter import REAL_MEDIA_OPERATION


def _shot(index: int, **overrides: object) -> MultiCameraShotInput:
    rng = np.random.default_rng(4400 + index)
    frames = np.full((1, 8, 8, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    chart = rng.uniform(0.05, 0.4, size=(8, 3))
    k = np.array([[800.0, 0.0, 4.0], [0.0, 800.0, 4.0], [0.0, 0.0, 1.0]])
    landmarks = np.zeros((1, 4, 3), dtype=np.float64)
    velocities = np.zeros((1, 4, 2), dtype=np.float64)
    defaults: dict[str, object] = {
        "episode_index": 1,
        "shot_index": index,
        "camera_a_frames_linear_rgb": frames,
        "camera_b_frames_linear_rgb": frames.copy(),
        "intrinsics_a": k,
        "intrinsics_b": k,
        "rotation_ab": np.eye(3),
        "translation_ab": np.array([0.2, 0.0, 0.0]),
        "points_a_px": np.array([[1.0, 1.0], [2.0, 3.0]]),
        "points_b_px": np.array([[1.0, 1.1], [2.0, 2.9]]),
        "source_sensor_chart_rgb": chart,
        "reference_chart_rgb": chart.copy(),
        "focal_length_mm": 35.0,
        "reference_focus_distance_mm": 2500.0,
        "focus_distance_mm": 2000.0,
        "principal_point_px": (3.5, 3.5),
        "camera_velocity_px_per_frame": (0.0, 0.0),
        "readout_fraction": 0.5,
        "frame_rate_fps": 24.0,
        "landmarks_3d": landmarks,
        "screen_velocities_px_s": velocities,
        "camera_position_m": np.array([3.0 + index, 5.0, 2.0]),
        "camera_to_world_rotation": np.eye(3),
        "dialogue_pcm_mono": np.ones(100, dtype=np.float64) * 0.01,
    }
    defaults.update(overrides)
    return MultiCameraShotInput(**defaults)  # type: ignore[arg-type]


def test_phase5_typed_refusal_probe_ledger_is_60_of_60(tmp_path: Path) -> None:
    room = AcousticRoomSpec("ledger-room", (12.0, 10.0, 4.0), (0.25,) * 6)
    intrinsics = np.eye(3)
    F = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    points = np.array([[1.0, 2.0]])
    chart = np.array([[0.2, 0.1, 0.3], [0.4, 0.2, 0.1], [0.1, 0.4, 0.2], [0.3, 0.3, 0.4]])
    frame = np.full((4, 4, 3), 0.2)
    poses = np.zeros((1, 1, 3))
    screen = np.zeros((1, 1, 2))
    shot1, shot2 = _shot(1), _shot(2)

    def run_engine(shots: tuple[MultiCameraShotInput, ...], **kwargs: object) -> object:
        args: dict[str, object] = {
            "room": room,
            "speaker_position_m": np.array([3.0, 5.0, 2.0]),
            "workspace_root": tmp_path / "probe-workspace",
        }
        args.update(kwargs)
        return execute_multicamera_mastering(shots, **args)  # type: ignore[arg-type]

    probes: list[tuple[str, Callable[[], object], type[Exception]]] = [
        (
            "rig invalid intrinsics",
            lambda: compute_fundamental_matrix_from_rig(
                np.zeros((3, 3)), intrinsics, np.eye(3), np.ones(3)
            ),
            ValueError,
        ),
        (
            "rig zero baseline",
            lambda: compute_fundamental_matrix_from_rig(
                intrinsics, intrinsics, np.eye(3), np.zeros(3)
            ),
            ValueError,
        ),
        (
            "rig improper rotation",
            lambda: compute_fundamental_matrix_from_rig(
                intrinsics, intrinsics, np.diag([1.0, 1.0, -1.0]), np.ones(3)
            ),
            ValueError,
        ),
        (
            "Sampson shape mismatch",
            lambda: sampson_epipolar_distance_squared(points, np.zeros((2, 2)), F),
            ValueError,
        ),
        (
            "Sampson zero matrix",
            lambda: sampson_epipolar_distance_squared(points, points, np.zeros((3, 3))),
            ValueError,
        ),
        (
            "Sampson singular gradient",
            lambda: sampson_epipolar_distance_squared(
                np.zeros((1, 2)), np.zeros((1, 2)), np.diag([1.0, 0.0, 0.0])
            ),
            ValueError,
        ),
        (
            "projection invalid tolerance",
            lambda: audit_and_project_epipolar_correspondences(
                points, points, F, tolerance_px2=0.0
            ),
            ValueError,
        ),
        (
            "projection non-convergence",
            lambda: audit_and_project_epipolar_correspondences(
                np.array([[1.0, 2.0]]),
                np.array([[3.0, 4.0]]),
                np.array([[0.2, 0.1, 0.4], [-0.3, 0.4, -0.2], [0.5, 0.7, 0.1]]),
                tolerance_px2=1e-30,
                max_iterations=1,
            ),
            ValueError,
        ),
        (
            "focus distance inside focal length",
            lambda: compensate_focus_breathing_frame(
                frame,
                focal_length_mm=50.0,
                reference_focus_distance_mm=1000.0,
                focus_distance_mm=40.0,
                principal_point_px=(1.5, 1.5),
            ),
            ValueError,
        ),
        (
            "focus principal point outside image",
            lambda: compensate_focus_breathing_frame(
                frame,
                focal_length_mm=50.0,
                reference_focus_distance_mm=1000.0,
                focus_distance_mm=1200.0,
                principal_point_px=(5.0, 1.0),
            ),
            ValueError,
        ),
        (
            "CoC subject inside focal length",
            lambda: compute_physical_circle_of_confusion_mm(40.0, 1000.0, 50.0, 2.8),
            ValueError,
        ),
        (
            "sensor chart rank deficient",
            lambda: harmonize_multicam_sensor_color(
                np.ones((6, 3)) * 0.2, np.ones((6, 3)) * 0.3, frame
            ),
            ValueError,
        ),
        (
            "sensor ridge non-positive",
            lambda: harmonize_multicam_sensor_color(chart, chart, frame, ridge=0.0),
            ValueError,
        ),
        (
            "sensor frame out of range",
            lambda: harmonize_multicam_sensor_color(chart, chart, np.full((4, 4, 3), 1.5)),
            ValueError,
        ),
        (
            "room invalid dimensions",
            lambda: acoustic_space_continuity.compute_room_acoustic_invariants(
                AcousticRoomSpec("bad", (1.0, 0.0, 2.0), (0.2,) * 6)
            ),
            ValueError,
        ),
        (
            "room invalid absorption",
            lambda: acoustic_space_continuity.compute_room_acoustic_invariants(
                AcousticRoomSpec("bad", (1.0, 2.0, 3.0), (0.0,) * 6)
            ),
            ValueError,
        ),
        (
            "RIR source outside room",
            lambda: acoustic_space_continuity.synthesize_binaural_room_impulse_response(
                room, np.array([0.0, 5.0, 2.0]), np.array([4.0, 5.0, 2.0]), np.eye(3)
            ),
            ValueError,
        ),
        (
            "RIR invalid orientation",
            lambda: acoustic_space_continuity.synthesize_binaural_room_impulse_response(
                room,
                np.array([3.0, 5.0, 2.0]),
                np.array([4.0, 5.0, 2.0]),
                np.diag([1.0, 1.0, -1.0]),
            ),
            ValueError,
        ),
        (
            "RIR non-positive rate",
            lambda: acoustic_space_continuity.synthesize_binaural_room_impulse_response(
                room,
                np.array([3.0, 5.0, 2.0]),
                np.array([4.0, 5.0, 2.0]),
                np.eye(3),
                sample_rate_hz=0,
            ),
            ValueError,
        ),
        (
            "RIR coincident source",
            lambda: acoustic_space_continuity.synthesize_binaural_room_impulse_response(
                room, np.array([4.0, 5.0, 2.0]), np.array([4.0, 5.0, 2.0]), np.eye(3)
            ),
            ValueError,
        ),
        (
            "RIR ear crosses wall",
            lambda: acoustic_space_continuity.synthesize_binaural_room_impulse_response(
                room, np.array([3.0, 5.0, 2.0]), np.array([0.05, 5.0, 2.0]), np.eye(3)
            ),
            ValueError,
        ),
        (
            "convolution non-mono input",
            lambda: acoustic_space_continuity.convolve_shot_dialogue_with_room_acoustics(
                np.ones((8, 2)), np.zeros((4, 2))
            ),
            ValueError,
        ),
        (
            "convolution invalid RIR channels",
            lambda: acoustic_space_continuity.convolve_shot_dialogue_with_room_acoustics(
                np.ones(8), np.zeros((4, 1))
            ),
            ValueError,
        ),
        (
            "continuity duplicate IDs",
            lambda: acoustic_space_continuity.audit_cross_shot_acoustic_continuity(
                room,
                np.array([3.0, 5.0, 2.0]),
                (
                    ("dup", np.array([4.0, 5.0, 2.0]), np.eye(3)),
                    ("dup", np.array([5.0, 5.0, 2.0]), np.eye(3)),
                ),
            ),
            ValueError,
        ),
        (
            "continuity only one shot",
            lambda: acoustic_space_continuity.audit_cross_shot_acoustic_continuity(
                room, np.array([3.0, 5.0, 2.0]), (("one", np.array([4.0, 5.0, 2.0]), np.eye(3)),)
            ),
            ValueError,
        ),
        (
            "kinetic empty trajectory",
            lambda: optimize_match_on_action_cut(
                np.zeros((0, 1, 3)), np.zeros((0, 1, 2)), poses, screen
            ),
            ValueError,
        ),
        (
            "kinetic velocity shape mismatch",
            lambda: optimize_match_on_action_cut(poses, np.zeros((1, 1, 1)), poses, screen),
            ValueError,
        ),
        (
            "kinetic non-finite velocity",
            lambda: optimize_match_on_action_cut(poses, np.full((1, 1, 2), np.nan), poses, screen),
            ValueError,
        ),
        (
            "kinetic landmark count mismatch",
            lambda: optimize_match_on_action_cut(
                poses, screen, np.zeros((1, 2, 3)), np.zeros((1, 2, 2))
            ),
            ValueError,
        ),
        (
            "kinetic negative weight",
            lambda: optimize_match_on_action_cut(poses, screen, poses, screen, pose_weight=-1.0),
            ValueError,
        ),
        (
            "kinetic all weights zero",
            lambda: optimize_match_on_action_cut(
                poses, screen, poses, screen, pose_weight=0.0, velocity_weight=0.0, speed_weight=0.0
            ),
            ValueError,
        ),
        (
            "kinetic invalid bridge duration",
            lambda: optimize_match_on_action_cut(
                poses, screen, poses, screen, bridge_duration_s=0.0
            ),
            ValueError,
        ),
        (
            "kinetic too few Hermite samples",
            lambda: optimize_match_on_action_cut(
                poses, screen, poses, screen, bridge_sample_count=1
            ),
            ValueError,
        ),
        (
            "kinetic zero frame rate",
            lambda: optimize_match_on_action_cut(
                poses, screen, poses, screen, source_frame_rate_fps=0.0
            ),
            ValueError,
        ),
        (
            "rolling invalid readout",
            lambda: rectify_rolling_shutter_frame(
                frame, (0.0, 0.0), readout_fraction=1.1, frame_period_s=0.1
            ),
            ValueError,
        ),
        (
            "rolling invalid period",
            lambda: rectify_rolling_shutter_frame(
                frame, (0.0, 0.0), readout_fraction=0.5, frame_period_s=0.0
            ),
            ValueError,
        ),
        (
            "phase shape mismatch",
            lambda: estimate_subpixel_phase_translation(frame, np.zeros((5, 4, 3))),
            ValueError,
        ),
        ("mastering single shot", lambda: run_engine((shot1,)), ValueError),
        (
            "mastering non-48-kHz persistence",
            lambda: run_engine((shot1, shot2), sample_rate_hz=44_100),
            ValueError,
        ),
        (
            "mastering camera shape mismatch",
            lambda: run_engine(
                (shot1, replace_shot(shot2, camera_b_frames_linear_rgb=np.zeros((1, 7, 8, 3)))),
            ),
            ValueError,
        ),
        (
            "mastering camera frame out of range",
            lambda: run_engine(
                (replace_shot(shot1, camera_b_frames_linear_rgb=np.full((1, 8, 8, 3), 2.0)), shot2)
            ),
            ValueError,
        ),
        (
            "mastering non-positive shot index",
            lambda: run_engine((replace_shot(shot1, shot_index=0), shot2)),
            ValueError,
        ),
        (
            "mastering malformed landmarks",
            lambda: run_engine(
                (
                    replace_shot(
                        shot1,
                        landmarks_3d=np.zeros((1, 0, 3)),
                        screen_velocities_px_s=np.zeros((1, 0, 2)),
                    ),
                    shot2,
                )
            ),
            ValueError,
        ),
        (
            "mastering non-mono dialogue",
            lambda: run_engine((replace_shot(shot1, dialogue_pcm_mono=np.ones((100, 2))), shot2)),
            ValueError,
        ),
        (
            "mastering malformed room",
            lambda: execute_multicamera_mastering(
                (shot1, shot2),
                room=AcousticRoomSpec("bad", (12.0, 10.0, 4.0), (0.0,) * 6),
                speaker_position_m=np.array([3.0, 5.0, 2.0]),
                workspace_root=tmp_path / "bad-room",
            ),
            ValueError,
        ),
        ("mastering misordered shot grid", lambda: run_engine((shot2, shot1)), ValueError),
        (
            "manifest missing fields",
            lambda: execute_multicamera_mastering_manifest(
                _write_json(tmp_path / "missing-fields.json", {"shots": []}),
                tmp_path / "missing-fields-workspace",
            ),
            ValueError,
        ),
        (
            "manifest wrong top-level type",
            lambda: execute_multicamera_mastering_manifest(
                _write_json(tmp_path / "wrong-type.json", []), tmp_path / "wrong-type-workspace"
            ),
            ValueError,
        ),
        (
            "Studio bridge rejects unknown engine",
            lambda: ChronosPort(build_default_chronos_handlers()).execute(
                ChronosCommand(
                    "episode.run_verified", ("unused.json",), {"continuity_engine": "unknown"}
                )
            ),
            KernelError,
        ),
        (
            "stereo PCM rejects three channels",
            lambda: real_media._align_stereo_pcm_to_sample_count(np.ones((8, 3)), 8),
            KernelError,
        ),
    ]
    spine_anchor = real_media.CharacterIdentityAnchor(
        "probe-anchor",
        np.array(
            [
                [-0.06, -0.05, 0.0],
                [0.06, -0.05, 0.0],
                [-0.05, 0.05, 0.0],
                [0.05, 0.05, 0.0],
                [-0.025, 0.0, 0.015],
                [0.025, 0.0, -0.015],
            ]
        ),
    )
    spine_attempt = ChronosSpineAttempt(
        "probe-project",
        "sha256:parent",
        "probe-attempt",
        1,
        REAL_MEDIA_OPERATION,
        {"sample_rate_hz": 48_000, "frame_size": [8, 8]},
        real_media_shots=(
            real_media.RealMediaShot(
                1,
                1,
                np.full((1, 8, 8, 3), 0.25),
                spine_anchor.cranial_landmarks_m,
                np.zeros(2002),
            ),
        ),
        show_bible_anchor=spine_anchor,
    )
    probes.extend(
        [
            (
                "spine rejects empty project identity",
                lambda: compute_chronos_recipe_identity(replace(spine_attempt, project_id="")),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects project path traversal",
                lambda: compute_chronos_recipe_identity(
                    replace(spine_attempt, project_id="../escape")
                ),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects empty parent revision",
                lambda: compute_chronos_recipe_identity(
                    replace(spine_attempt, parent_revision_id=" ")
                ),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects attempt path traversal",
                lambda: compute_chronos_recipe_identity(
                    replace(spine_attempt, attempt_id="attempt/../../escape")
                ),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects zero fencing token",
                lambda: compute_chronos_recipe_identity(replace(spine_attempt, fencing_token=0)),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects boolean fencing token alias",
                lambda: compute_chronos_recipe_identity(replace(spine_attempt, fencing_token=True)),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects unsupported recipe operation",
                lambda: compute_chronos_recipe_identity(
                    replace(spine_attempt, operation_name="unknown")
                ),
                ChronosSpineExecutionError,
            ),
            (
                "spine rejects non-finite recipe parameters",
                lambda: compute_chronos_recipe_identity(
                    replace(spine_attempt, parameters={"temperature": float("nan")})
                ),
                ChronosSpineExecutionError,
            ),
            (
                "spine refuses stale active fence before render",
                lambda: dispatch_chronos_spine_operation(
                    "chronos.staged_spine_master",
                    tmp_path / "stale-fence-probe",
                    spine_attempt,
                    active_fencing_token=lambda project_id, attempt_id: 2,
                ),
                ChronosVerificationRefusalError,
            ),
            (
                "spine bridge rejects unregistered operation",
                lambda: dispatch_chronos_spine_operation(
                    "chronos.unregistered",
                    tmp_path / "unknown-operation-probe",
                    spine_attempt,
                    active_fencing_token=lambda project_id, attempt_id: 1,
                ),
                ValueError,
            ),
        ]
    )
    assert len(probes) == 60
    killed: list[str] = []
    for label, call, exception_type in probes:
        try:
            call()
        except exception_type:
            killed.append(label)
        else:
            pytest.fail(f"probe did not raise {exception_type.__name__}: {label}")
    assert len(killed) == 60
    assert len(set(killed)) == 60


def replace_shot(shot: MultiCameraShotInput, **changes: object) -> MultiCameraShotInput:
    return replace(shot, **changes)  # type: ignore[arg-type]


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
