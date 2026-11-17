"""Unified multi-camera mastering, physical continuity, and persisted proof pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from . import acoustic_space_continuity, kinetic_match_cut_engine, multicam_epipolar_matching
from .acoustic_space_continuity import (
    AcousticConvolutionReceipt,
    AcousticRoomSpec,
    BinauralRIRReceipt,
    CrossShotAcousticContinuityReceipt,
)
from .kinetic_match_cut_engine import (
    KineticCutReceipt,
    RollingShutterFrameReceipt,
)
from .multicam_epipolar_matching import (
    CorrespondenceAuditReceipt,
    FocusBreathingReceipt,
    MultiCameraColorReceipt,
)
from .real_media_continuity import (
    CharacterIdentityAnchor,
    RealMediaSeasonReceipt,
    RealMediaShot,
    RealMediaShotReceipt,
    align_pcm_to_sample_count,
    bresenham_sample_counts,
    execute_real_media_season_continuity,
)

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class MultiCameraShotInput:
    """Per-shot aligned camera buffers, calibration, motion, room, and dialogue inputs."""

    episode_index: int
    shot_index: int
    camera_a_frames_linear_rgb: FloatArray
    camera_b_frames_linear_rgb: FloatArray
    intrinsics_a: FloatArray
    intrinsics_b: FloatArray
    rotation_ab: FloatArray
    translation_ab: FloatArray
    points_a_px: FloatArray
    points_b_px: FloatArray
    source_sensor_chart_rgb: FloatArray
    reference_chart_rgb: FloatArray
    focal_length_mm: float
    reference_focus_distance_mm: float
    focus_distance_mm: float
    principal_point_px: tuple[float, float]
    camera_velocity_px_per_frame: tuple[float, float]
    readout_fraction: float
    frame_rate_fps: float
    landmarks_3d: FloatArray
    screen_velocities_px_s: FloatArray
    camera_position_m: FloatArray
    camera_to_world_rotation: FloatArray
    dialogue_pcm_mono: FloatArray


@dataclass(frozen=True, slots=True)
class MasteredMultiCameraShot:
    episode_index: int
    shot_index: int
    epipolar_audit: CorrespondenceAuditReceipt
    color_calibration: MultiCameraColorReceipt
    focus_receipts: tuple[FocusBreathingReceipt, ...]
    rolling_shutter_receipts: tuple[RollingShutterFrameReceipt, ...]
    phase_translations_xy_px: tuple[tuple[float, float], ...]
    room_rir: BinauralRIRReceipt
    room_audio: AcousticConvolutionReceipt
    persisted_shot: RealMediaShotReceipt


@dataclass(frozen=True, slots=True)
class MultiCameraMasteringReceipt:
    shot_count: int
    cut_receipts: tuple[KineticCutReceipt, ...]
    acoustic_continuity: CrossShotAcousticContinuityReceipt
    persisted_season: RealMediaSeasonReceipt
    shots: tuple[MasteredMultiCameraShot, ...]

    def summary(self) -> dict[str, Any]:
        """Return a compact JSON-safe bridge receipt without embedding pixel/audio arrays."""
        return {
            "schema": "chronos-multicamera-mastering-v1",
            "continuity": "OK",
            "shot_count": self.shot_count,
            "cut_count": len(self.cut_receipts),
            "merkle_root_sha256": self.persisted_season.merkle_root_sha256,
            "season_manifest_path": self.persisted_season.manifest_path,
            "sample_count": self.persisted_season.sample_count,
            "exact_sample_horizon": self.persisted_season.exact_sample_horizon,
            "anchored_sample_clock_drift": self.persisted_season.anchored_sample_clock_drift,
            "muxed_artifact_count": self.persisted_season.muxed_artifact_count,
            "rt60_seconds": self.acoustic_continuity.room_invariants.rt60_seconds,
            "rt60_max_spread_seconds": self.acoustic_continuity.rt60_max_spread_seconds,
            "drr_monotonic_with_proximity": self.acoustic_continuity.drr_monotonic_with_proximity,
            "cuts": [
                {
                    "outgoing_frame_index": cut.outgoing_frame_index,
                    "incoming_frame_index": cut.incoming_frame_index,
                    "selected_cost": cut.selected_cost,
                    "cost_reduction": cut.cost_reduction,
                    "bridge_duration_s": cut.bridge_duration_s,
                    "bridge_sample_count": len(cut.bridge_landmarks_3d),
                }
                for cut in self.cut_receipts
            ],
            "shots": [
                {
                    "episode_index": shot.episode_index,
                    "shot_index": shot.shot_index,
                    "max_sampson_distance_squared_after": float(
                        np.max(shot.epipolar_audit.sampson_distance_squared_after)
                    ),
                    "max_chart_delta_e00_after": shot.color_calibration.delta_e00_after_max,
                    "max_rolling_warp_px": max(
                        receipt.max_warp_px for receipt in shot.rolling_shutter_receipts
                    ),
                    "phase_translation_xy_px": [
                        list(value) for value in shot.phase_translations_xy_px
                    ],
                    "audio_samples": shot.persisted_shot.audio_samples,
                    "audio_channels": shot.persisted_shot.audio_channel_count,
                    "audio_path": shot.persisted_shot.audio_path,
                    "mux_path": shot.persisted_shot.mux_path,
                    "inclusion_bundle": shot.persisted_shot.inclusion_bundle,
                }
                for shot in self.shots
            ],
        }


def _validate_shot_buffers(shot: MultiCameraShotInput) -> tuple[FloatArray, FloatArray]:
    frames_a = np.asarray(shot.camera_a_frames_linear_rgb, dtype=np.float64)
    frames_b = np.asarray(shot.camera_b_frames_linear_rgb, dtype=np.float64)
    if (
        frames_a.ndim != 4
        or frames_b.shape != frames_a.shape
        or frames_a.shape[0] == 0
        or frames_a.shape[1] < 2
        or frames_a.shape[2] < 2
        or frames_a.shape[3] != 3
        or np.any(~np.isfinite(frames_a))
        or np.any(~np.isfinite(frames_b))
        or np.any((frames_a < 0.0) | (frames_a > 1.0))
        or np.any((frames_b < 0.0) | (frames_b > 1.0))
    ):
        raise ValueError("mastering requires matching finite F×H×W×3 [0,1] camera frame buffers")
    if (
        isinstance(shot.episode_index, bool)
        or not isinstance(shot.episode_index, (int, np.integer))
        or isinstance(shot.shot_index, bool)
        or not isinstance(shot.shot_index, (int, np.integer))
        or shot.episode_index <= 0
        or shot.shot_index <= 0
    ):
        raise ValueError("episode and shot indices must be positive one-based integers")
    landmarks = np.asarray(shot.landmarks_3d, dtype=np.float64)
    velocities = np.asarray(shot.screen_velocities_px_s, dtype=np.float64)
    dialogue = np.asarray(shot.dialogue_pcm_mono, dtype=np.float64)
    if (
        landmarks.ndim != 3
        or landmarks.shape[0] == 0
        or landmarks.shape[1] == 0
        or landmarks.shape[2] != 3
        or velocities.shape != (landmarks.shape[0], landmarks.shape[1], 2)
        or np.any(~np.isfinite(landmarks))
        or np.any(~np.isfinite(velocities))
        or dialogue.ndim != 1
        or dialogue.size == 0
        or np.any(~np.isfinite(dialogue))
    ):
        raise ValueError("mastering requires finite TxLx3 poses, TxLx2 velocities, and mono PCM")
    if (
        not np.isfinite(shot.frame_rate_fps)
        or shot.frame_rate_fps <= 0
        or not np.isfinite(shot.readout_fraction)
        or not 0 <= shot.readout_fraction <= 1
    ):
        raise ValueError("mastering frame rate must be positive and readout fraction in [0,1]")
    return frames_a, frames_b


def _translate_frame(frame: FloatArray, dx_px: float, dy_px: float) -> FloatArray:
    height, width = frame.shape[:2]
    rows, columns = np.mgrid[0:height, 0:width]
    sample_x = np.clip(columns + dx_px, 0.0, width - 1.0)
    sample_y = np.clip(rows + dy_px, 0.0, height - 1.0)
    x0, y0 = np.floor(sample_x).astype(np.intp), np.floor(sample_y).astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    wx, wy = (sample_x - x0)[..., None], (sample_y - y0)[..., None]
    upper = frame[y0, x0] * (1.0 - wx) + frame[y0, x1] * wx
    lower = frame[y1, x0] * (1.0 - wx) + frame[y1, x1] * wx
    return np.asarray(upper * (1.0 - wy) + lower * wy, dtype=np.float64)


def _luminance(rgb: FloatArray) -> FloatArray:
    return np.asarray(rgb[..., :3] @ np.array([0.2126, 0.7152, 0.0722]), dtype=np.float64)


def _master_shot_frames(
    shot: MultiCameraShotInput, frames_a: FloatArray, frames_b: FloatArray
) -> tuple[
    FloatArray,
    CorrespondenceAuditReceipt,
    MultiCameraColorReceipt,
    tuple[FocusBreathingReceipt, ...],
    tuple[RollingShutterFrameReceipt, ...],
    tuple[tuple[float, float], ...],
]:
    rig = multicam_epipolar_matching.compute_fundamental_matrix_from_rig(
        shot.intrinsics_a, shot.intrinsics_b, shot.rotation_ab, shot.translation_ab
    )
    epipolar = multicam_epipolar_matching.audit_and_project_epipolar_correspondences(
        shot.points_a_px, shot.points_b_px, rig.fundamental_matrix
    )
    color = multicam_epipolar_matching.harmonize_multicam_sensor_color(
        shot.source_sensor_chart_rgb,
        shot.reference_chart_rgb,
        frames_b,
    )
    harmonized = color.harmonized_rgb
    principal = shot.principal_point_px
    focus_receipts: list[FocusBreathingReceipt] = []
    rolling_receipts: list[RollingShutterFrameReceipt] = []
    phase_translations: list[tuple[float, float]] = []
    mastered_frames: list[FloatArray] = []
    period = 1.0 / shot.frame_rate_fps
    for reference_frame, source_frame in zip(frames_a, harmonized, strict=True):
        focus = multicam_epipolar_matching.compensate_focus_breathing_frame(
            source_frame,
            focal_length_mm=shot.focal_length_mm,
            reference_focus_distance_mm=shot.reference_focus_distance_mm,
            focus_distance_mm=shot.focus_distance_mm,
            principal_point_px=principal,
        )
        rolling = kinetic_match_cut_engine.rectify_rolling_shutter_frame(
            focus.frame,
            shot.camera_velocity_px_per_frame,
            readout_fraction=shot.readout_fraction,
            frame_period_s=period,
        )
        phase = kinetic_match_cut_engine.estimate_subpixel_phase_translation(
            _luminance(reference_frame), _luminance(rolling.rectified_frame)
        )
        aligned_source = _translate_frame(
            rolling.rectified_frame, phase.translation_x_px, phase.translation_y_px
        )
        mastered_frames.append(
            np.asarray(
                np.clip(0.5 * (reference_frame + aligned_source), 0.0, 1.0), dtype=np.float64
            )
        )
        focus_receipts.append(focus)
        rolling_receipts.append(rolling)
        phase_translations.append((phase.translation_x_px, phase.translation_y_px))
    return (
        np.asarray(mastered_frames, dtype=np.float64),
        epipolar,
        color,
        tuple(focus_receipts),
        tuple(rolling_receipts),
        tuple(phase_translations),
    )


def execute_multicamera_mastering(
    shots: tuple[MultiCameraShotInput, ...] | list[MultiCameraShotInput],
    *,
    room: AcousticRoomSpec,
    speaker_position_m: FloatArray,
    workspace_root: str | Path,
    show_bible_anchor: CharacterIdentityAnchor | None = None,
    sample_rate_hz: int = 48_000,
    max_reflection_order: int = 2,
    bridge_duration_s: float = 0.12,
    bridge_sample_count: int = 8,
    ffmpeg_path: str | None = None,
) -> MultiCameraMasteringReceipt:
    """Master camera pairs and cuts, then persist exact PCM/RGB and Merkle evidence."""
    ordered_shots = tuple(shots)
    if sample_rate_hz != 48_000:
        raise ValueError("the Wave 11–12 persistence bridge requires 48 kHz PCM")
    if len(ordered_shots) < 2:
        raise ValueError("multicamera mastering requires at least two ordered shots")
    frame_buffers = [_validate_shot_buffers(shot) for shot in ordered_shots]
    dimensions = frame_buffers[0][0].shape[1:3]
    if any(frames_a.shape[1:3] != dimensions for frames_a, _ in frame_buffers):
        raise ValueError("all mastering frames must use the same persisted image dimensions")
    episode_count = max(shot.episode_index for shot in ordered_shots)
    shot_indices = sorted({shot.shot_index for shot in ordered_shots})
    shots_per_episode = len(shot_indices)
    expected_keys = tuple(
        (episode, index)
        for episode in range(1, episode_count + 1)
        for index in range(1, shots_per_episode + 1)
    )
    actual_keys = tuple((shot.episode_index, shot.shot_index) for shot in ordered_shots)
    if actual_keys != expected_keys:
        raise ValueError("mastering shots must be a complete ordered one-based episode/shot grid")

    acoustic_shots = tuple(
        (
            f"episode-{shot.episode_index:04d}-shot-{shot.shot_index:04d}",
            shot.camera_position_m,
            shot.camera_to_world_rotation,
        )
        for shot in ordered_shots
    )
    acoustic_continuity = acoustic_space_continuity.audit_cross_shot_acoustic_continuity(
        room,
        speaker_position_m,
        acoustic_shots,
        sample_rate_hz=sample_rate_hz,
        max_order=max_reflection_order,
    )
    frame_counts = [int(frames_a.shape[0]) for frames_a, _ in frame_buffers]
    sample_counts = bresenham_sample_counts(frame_counts, sample_rate_hz=sample_rate_hz)

    mastered: list[dict[str, Any]] = []
    persisted_shots: list[RealMediaShot] = []
    for shot, (frames_a, frames_b), sample_count in zip(
        ordered_shots, frame_buffers, sample_counts, strict=True
    ):
        (
            mastered_frames,
            epipolar,
            color,
            focus_receipts,
            rolling_receipts,
            phase_translations,
        ) = _master_shot_frames(shot, frames_a, frames_b)
        rir = acoustic_space_continuity.synthesize_binaural_room_impulse_response(
            room,
            speaker_position_m,
            shot.camera_position_m,
            shot.camera_to_world_rotation,
            sample_rate_hz=sample_rate_hz,
            max_order=max_reflection_order,
        )
        dialogue, _, _ = align_pcm_to_sample_count(shot.dialogue_pcm_mono, sample_count)
        room_audio = acoustic_space_continuity.convolve_shot_dialogue_with_room_acoustics(
            dialogue, rir.stereo_rir, sample_rate_hz=sample_rate_hz
        )
        if room_audio.output_sample_count != sample_count or room_audio.sample_drift != 0:
            raise ValueError("acoustic render changed the exact rational PCM sample horizon")
        representative_landmarks = np.asarray(shot.landmarks_3d[-1], dtype=np.float64)
        persisted_shots.append(
            RealMediaShot(
                episode_index=shot.episode_index,
                shot_index=shot.shot_index,
                frames_linear_rgb=mastered_frames,
                cranial_landmarks_m=representative_landmarks,
                audio_pcm_stereo_float=room_audio.stereo_pcm,
            )
        )
        mastered.append(
            {
                "input": shot,
                "epipolar": epipolar,
                "color": color,
                "focus": focus_receipts,
                "rolling": rolling_receipts,
                "phase": phase_translations,
                "rir": rir,
                "audio": room_audio,
            }
        )

    cut_receipts: list[KineticCutReceipt] = []
    for outgoing, incoming in zip(ordered_shots, ordered_shots[1:], strict=False):
        if outgoing.episode_index != incoming.episode_index:
            continue
        cut_receipts.append(
            kinetic_match_cut_engine.optimize_match_on_action_cut(
                outgoing.landmarks_3d,
                outgoing.screen_velocities_px_s,
                incoming.landmarks_3d,
                incoming.screen_velocities_px_s,
                bridge_duration_s=bridge_duration_s,
                bridge_sample_count=bridge_sample_count,
                source_frame_rate_fps=outgoing.frame_rate_fps,
            )
        )

    persisted_season = execute_real_media_season_continuity(
        workspace_root,
        shots=persisted_shots,
        show_bible_anchor=show_bible_anchor,
        episode_count=episode_count,
        shots_per_episode=shots_per_episode,
        frame_count_per_shot=frame_counts[0],
        frame_size=(int(dimensions[0]), int(dimensions[1])),
        sample_rate_hz=sample_rate_hz,
        ffmpeg_path=ffmpeg_path,
    )
    receipts: list[MasteredMultiCameraShot] = []
    for result, persisted in zip(mastered, persisted_season.shots, strict=True):
        receipts.append(
            MasteredMultiCameraShot(
                result["input"].episode_index,
                result["input"].shot_index,
                result["epipolar"],
                result["color"],
                result["focus"],
                result["rolling"],
                result["phase"],
                result["rir"],
                result["audio"],
                persisted,
            )
        )
    return MultiCameraMasteringReceipt(
        len(receipts), tuple(cut_receipts), acoustic_continuity, persisted_season, tuple(receipts)
    )


def execute_multicamera_mastering_manifest(
    manifest_path: str | Path,
    workspace_root: str | Path,
) -> MultiCameraMasteringReceipt:
    """Load a JSON mastering manifest for the existing deterministic episode bridge."""
    try:
        path = Path(manifest_path).expanduser().resolve(strict=True)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("multicamera manifest must be readable UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("shots"), list):
        raise ValueError("multicamera manifest requires an object with an ordered shots list")
    try:
        raw_room = payload["room"]
        if not isinstance(raw_room, dict):
            raise ValueError("room spec must be an object")
        room = AcousticRoomSpec(
            str(raw_room["room_id"]),
            cast(tuple[float, float, float], tuple(float(v) for v in raw_room["dimensions_m"])),
            cast(
                tuple[float, float, float, float, float, float],
                tuple(float(v) for v in raw_room["wall_absorption"]),
            ),
            float(raw_room.get("air_attenuation_per_m", 0.0)),
        )
        parsed_shots: list[MultiCameraShotInput] = []
        for raw in payload["shots"]:
            if not isinstance(raw, dict):
                raise ValueError("each mastering shot must be an object")
            parsed_shots.append(
                MultiCameraShotInput(
                    int(raw["episode_index"]),
                    int(raw["shot_index"]),
                    np.asarray(raw["camera_a_frames_linear_rgb"], dtype=np.float64),
                    np.asarray(raw["camera_b_frames_linear_rgb"], dtype=np.float64),
                    np.asarray(raw["intrinsics_a"], dtype=np.float64),
                    np.asarray(raw["intrinsics_b"], dtype=np.float64),
                    np.asarray(raw["rotation_ab"], dtype=np.float64),
                    np.asarray(raw["translation_ab"], dtype=np.float64),
                    np.asarray(raw["points_a_px"], dtype=np.float64),
                    np.asarray(raw["points_b_px"], dtype=np.float64),
                    np.asarray(raw["source_sensor_chart_rgb"], dtype=np.float64),
                    np.asarray(raw["reference_chart_rgb"], dtype=np.float64),
                    float(raw["focal_length_mm"]),
                    float(raw["reference_focus_distance_mm"]),
                    float(raw["focus_distance_mm"]),
                    cast(tuple[float, float], tuple(float(v) for v in raw["principal_point_px"])),
                    cast(
                        tuple[float, float],
                        tuple(float(v) for v in raw["camera_velocity_px_per_frame"]),
                    ),
                    float(raw["readout_fraction"]),
                    float(raw["frame_rate_fps"]),
                    np.asarray(raw["landmarks_3d"], dtype=np.float64),
                    np.asarray(raw["screen_velocities_px_s"], dtype=np.float64),
                    np.asarray(raw["camera_position_m"], dtype=np.float64),
                    np.asarray(raw["camera_to_world_rotation"], dtype=np.float64),
                    np.asarray(raw["dialogue_pcm_mono"], dtype=np.float64),
                )
            )
        raw_anchor = payload.get("show_bible_anchor")
        anchor = None
        if raw_anchor is not None:
            if not isinstance(raw_anchor, dict):
                raise ValueError("Show-Bible anchor must be an object")
            anchor = CharacterIdentityAnchor(
                str(raw_anchor["character_id"]),
                np.asarray(raw_anchor["cranial_landmarks_m"], dtype=np.float64),
                cast(
                    tuple[float, float, float],
                    tuple(
                        float(v)
                        for v in raw_anchor.get("reference_patch_linear_rgb", (0.24, 0.34, 0.42))
                    ),
                ),
                float(raw_anchor.get("reference_illuminant_kelvin", 6500.0)),
                int(raw_anchor.get("left_landmark_index", 0)),
                (
                    int(raw_anchor["right_landmark_index"])
                    if "right_landmark_index" in raw_anchor
                    else 1
                ),
                cast(
                    tuple[int, int, int, int],
                    tuple(int(v) for v in raw_anchor.get("calibration_roi", (0, 4, 0, 4))),
                ),
            )
        return execute_multicamera_mastering(
            parsed_shots,
            room=room,
            speaker_position_m=np.asarray(payload["speaker_position_m"], dtype=np.float64),
            workspace_root=workspace_root,
            show_bible_anchor=anchor,
            sample_rate_hz=int(payload.get("sample_rate_hz", 48_000)),
            max_reflection_order=int(payload.get("max_reflection_order", 2)),
            bridge_duration_s=float(payload.get("bridge_duration_s", 0.12)),
            bridge_sample_count=int(payload.get("bridge_sample_count", 8)),
            ffmpeg_path=payload.get("ffmpeg_path"),
        )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("multicamera manifest omitted or mistyped a required field") from exc
