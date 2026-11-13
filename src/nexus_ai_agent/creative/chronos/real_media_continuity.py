"""Real pixel, cranial-landmark, PCM, and disk-artifact season continuity.

This CPU-only proof engine applies explicit deterministic drift to source media,
restores it against a Show-Bible anchor, writes contained PNG/WAV artifacts, and
optionally muxes each shot with FFmpeg. It is engineering evidence, not a claim
of perceptual or biometric validation on production footage.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

from . import color_science, geometry, identity_lock, merkle_ledger
from .contracts import KernelError

FloatArray = NDArray[np.float64]

_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)


@dataclass(frozen=True, slots=True)
class CharacterIdentityAnchor:
    """Canonical 3D landmarks and a calibrated linear-RGB Show-Bible patch."""

    character_id: str
    cranial_landmarks_m: FloatArray
    reference_patch_linear_rgb: tuple[float, float, float] = (0.24, 0.34, 0.42)
    reference_illuminant_kelvin: float = 6500.0
    left_landmark_index: int = 0
    right_landmark_index: int = 1
    calibration_roi: tuple[int, int, int, int] = (0, 4, 0, 4)


@dataclass(frozen=True, slots=True)
class RealMediaShot:
    """Optional captured input buffers; omitted components are synthesized reproducibly."""

    episode_index: int
    shot_index: int
    frames_linear_rgb: FloatArray | None = None
    cranial_landmarks_m: FloatArray | None = None
    audio_pcm_float: FloatArray | None = None


@dataclass(frozen=True, slots=True)
class RealMediaShotReceipt:
    episode_index: int
    shot_index: int
    frame_count: int
    audio_samples: int
    input_audio_samples: int
    audio_trimmed_samples: int
    audio_padded_samples: int
    sample_start: int
    sample_end: int
    unanchored_scale_drift: float
    restored_scale_drift: float
    downweighted_landmark_count: int
    unanchored_delta_e00_by_frame: tuple[float, ...]
    restored_delta_e00_by_frame: tuple[float, ...]
    frame_paths: tuple[str, ...]
    frame_sha256s: tuple[str, ...]
    audio_path: str
    audio_sha256: str
    mux_path: str | None
    mux_sha256: str | None
    inclusion_bundle: dict[str, Any]
    restored_frames_linear_rgb: FloatArray
    restored_landmarks_m: FloatArray


@dataclass(frozen=True, slots=True)
class RealMediaSeasonReceipt:
    episode_count: int
    shot_count: int
    frame_count: int
    sample_rate_hz: int
    sample_count: int
    exact_sample_horizon: int
    anchored_sample_clock_drift: int
    naive_sample_clock_drift: int
    naive_audio_sample_count: int
    muxed_artifact_count: int
    merkle_root_sha256: str
    manifest_path: str
    shots: tuple[RealMediaShotReceipt, ...]


def canonical_sample_horizon(
    frame_count: int,
    *,
    sample_rate_hz: int = 48_000,
    frame_rate_num: int = 24_000,
    frame_rate_den: int = 1001,
) -> int:
    """Floor the exact rational sample horizon; never accumulate float seconds."""
    if frame_count < 0 or min(sample_rate_hz, frame_rate_num, frame_rate_den) <= 0:
        raise KernelError("sample-clock inputs must be non-negative frames and positive rates")
    return frame_count * sample_rate_hz * frame_rate_den // frame_rate_num


def bresenham_sample_counts(
    frame_counts: tuple[int, ...] | list[int],
    *,
    sample_rate_hz: int = 48_000,
    frame_rate_num: int = 24_000,
    frame_rate_den: int = 1001,
) -> tuple[int, ...]:
    """Distribute rational samples over shots with an exact carried remainder."""
    if not frame_counts or any(count <= 0 for count in frame_counts):
        raise KernelError("Bresenham sample clock requires positive frame counts")
    if min(sample_rate_hz, frame_rate_num, frame_rate_den) <= 0:
        raise KernelError("sample-clock rates must be positive")
    numerator_per_frame = sample_rate_hz * frame_rate_den
    remainder = 0
    result: list[int] = []
    for frame_count in frame_counts:
        samples, remainder = divmod(remainder + frame_count * numerator_per_frame, frame_rate_num)
        result.append(samples)
    if sum(result) != canonical_sample_horizon(
        sum(frame_counts),
        sample_rate_hz=sample_rate_hz,
        frame_rate_num=frame_rate_num,
        frame_rate_den=frame_rate_den,
    ):
        raise KernelError("Bresenham accumulator violated the exact sample horizon")
    return tuple(result)


def align_pcm_to_sample_count(
    audio_pcm_float: FloatArray, expected_samples: int
) -> tuple[FloatArray, int, int]:
    """Return finite mono float PCM at the exact integer sample count by trim/pad."""
    audio = np.asarray(audio_pcm_float, dtype=np.float64).reshape(-1)
    if expected_samples < 0 or np.any(~np.isfinite(audio)):
        raise KernelError("audio alignment requires finite PCM and a non-negative target")
    output = np.zeros(expected_samples, dtype=np.float64)
    copied = min(len(audio), expected_samples)
    output[:copied] = audio[:copied]
    return output, max(0, len(audio) - expected_samples), max(0, expected_samples - len(audio))


def apply_illuminant_exposure_drift(
    frames_linear_rgb: FloatArray,
    *,
    source_illuminant_kelvin: float,
    drifted_illuminant_kelvin: float,
    exposure_multiplier: float,
) -> FloatArray:
    """Apply Bradford illuminant shift and exposure drift to an F×H×W×3 tensor."""
    frames = _validate_frames(frames_linear_rgb)
    if (
        not 2000 <= source_illuminant_kelvin <= 15_000
        or not 2000 <= drifted_illuminant_kelvin <= 15_000
        or not np.isfinite(exposure_multiplier)
        or exposure_multiplier <= 0
    ):
        raise KernelError("illuminant drift requires supported kelvin and positive exposure")
    source_white = np.asarray(color_science.cct_kelvin_to_xyz(source_illuminant_kelvin))
    drifted_white = np.asarray(color_science.cct_kelvin_to_xyz(drifted_illuminant_kelvin))
    cat = color_science.bradford_adaptation(source_white, drifted_white)
    xyz = frames @ _RGB_TO_XYZ.T
    drifted_xyz = xyz @ cat.T
    return np.asarray((drifted_xyz @ _XYZ_TO_RGB.T) * exposure_multiplier, dtype=np.float64)


def _remove_known_color_drift(
    drifted_frames_linear_rgb: FloatArray,
    *,
    drifted_illuminant_kelvin: float,
    reference_illuminant_kelvin: float,
    exposure_multiplier: float,
) -> FloatArray:
    """Inverse-map known illuminant/exposure drift to reference-white XYZ."""
    frames = _validate_frames(drifted_frames_linear_rgb)
    if exposure_multiplier <= 0 or not np.isfinite(exposure_multiplier):
        raise KernelError("exposure correction requires a finite positive multiplier")
    drifted_white = np.asarray(color_science.cct_kelvin_to_xyz(drifted_illuminant_kelvin))
    reference_white = np.asarray(color_science.cct_kelvin_to_xyz(reference_illuminant_kelvin))
    inverse_cat = color_science.bradford_adaptation(drifted_white, reference_white)
    xyz = frames @ _RGB_TO_XYZ.T
    neutral_xyz = (xyz @ inverse_cat.T) / exposure_multiplier
    return np.asarray(neutral_xyz, dtype=np.float64)


def restore_anchored_color_frames(
    drifted_frames_linear_rgb: FloatArray,
    *,
    drifted_illuminant_kelvin: float,
    reference_illuminant_kelvin: float,
    exposure_multiplier: float,
    reference_patch_linear_rgb: tuple[float, float, float],
    calibration_roi: tuple[int, int, int, int],
) -> FloatArray:
    """Undo known drift, then Bradford-close each frame's patch to the Show Bible."""
    frames = _validate_frames(drifted_frames_linear_rgb)
    y0, y1, x0, x1 = _validate_roi(calibration_roi, frames.shape[1:3])
    reference_patch = np.asarray(reference_patch_linear_rgb, dtype=np.float64)
    if reference_patch.shape != (3,) or np.any(~np.isfinite(reference_patch)):
        raise KernelError("Show-Bible patch must be a finite linear-RGB triple")
    neutral_xyz = _remove_known_color_drift(
        frames,
        drifted_illuminant_kelvin=drifted_illuminant_kelvin,
        reference_illuminant_kelvin=reference_illuminant_kelvin,
        exposure_multiplier=exposure_multiplier,
    )
    neutral_rgb = neutral_xyz @ _XYZ_TO_RGB.T

    reference_xyz = _RGB_TO_XYZ @ reference_patch
    reference_cone = color_science.BRADFORD_MATRIX @ reference_xyz
    measured_patch = neutral_rgb[:, y0:y1, x0:x1].mean(axis=(1, 2))
    measured_xyz = measured_patch @ _RGB_TO_XYZ.T
    measured_cone = measured_xyz @ color_science.BRADFORD_MATRIX.T
    if np.any(np.abs(measured_cone) < 1e-12):
        raise KernelError("Show-Bible pixel patch has singular Bradford cone response")
    correction_ratio = reference_cone[None, :] / measured_cone
    all_cone = neutral_xyz @ color_science.BRADFORD_MATRIX.T
    corrected_xyz = (all_cone * correction_ratio[:, None, None, :]) @ np.linalg.inv(
        color_science.BRADFORD_MATRIX
    ).T
    corrected_rgb = corrected_xyz @ _XYZ_TO_RGB.T
    if np.any(~np.isfinite(corrected_rgb)):
        raise KernelError("Show-Bible color correction produced non-finite pixels")
    return np.asarray(corrected_rgb, dtype=np.float64)


def measure_patch_delta_e00_by_frame(
    frames_linear_rgb: FloatArray,
    reference_patch_linear_rgb: tuple[float, float, float],
    calibration_roi: tuple[int, int, int, int],
) -> tuple[float, ...]:
    """Measure worst pixel-wise CIEDE2000 error in the calibrated ROI per frame."""
    frames = _validate_frames(frames_linear_rgb)
    y0, y1, x0, x1 = _validate_roi(calibration_roi, frames.shape[1:3])
    reference = np.asarray(reference_patch_linear_rgb, dtype=np.float64)
    if reference.shape != (3,) or np.any(~np.isfinite(reference)):
        raise KernelError("reference patch must be a finite linear-RGB triple")
    reference_lab = color_science.rgb_to_lab(color_science.srgb_encode(reference))
    roi = np.clip(frames[:, y0:y1, x0:x1], 0.0, 1.0)
    lab = color_science.rgb_to_lab(color_science.srgb_encode(roi))
    delta = color_science.delta_e_2000(lab, reference_lab)
    return tuple(float(value) for value in np.max(delta, axis=(1, 2)))


def _validate_frames(frames_linear_rgb: FloatArray) -> FloatArray:
    frames = np.asarray(frames_linear_rgb, dtype=np.float64)
    if (
        frames.ndim != 4
        or frames.shape[-1] != 3
        or min(frames.shape[:3]) <= 0
        or np.any(~np.isfinite(frames))
    ):
        raise KernelError("linear RGB frames must be finite F×H×W×3 tensors")
    return frames


def _validate_roi(
    roi: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    y0, y1, x0, x1 = roi
    height, width = size
    if not (0 <= y0 < y1 <= height and 0 <= x0 < x1 <= width):
        raise KernelError("calibration ROI must be a non-empty in-frame rectangle")
    return roi


def _default_anchor() -> CharacterIdentityAnchor:
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
    return CharacterIdentityAnchor("mira", landmarks)


def _synthesize_frames(
    frame_count: int, height: int, width: int, patch: tuple[float, float, float]
) -> FloatArray:
    image = np.empty((frame_count, height, width, 3), dtype=np.float64)
    image[:] = np.asarray(patch, dtype=np.float64)
    rows, columns = np.mgrid[0:height, 0:width]
    texture = np.stack((0.0004 * columns, 0.0003 * rows, 0.0002 * (rows + columns)), axis=-1)
    if height > 4 and width > 4:
        image[:, 4:, 4:, :] += texture[4:, 4:, :]
    return image


def _synthesize_audio(start_sample: int, sample_count: int, rate: int) -> FloatArray:
    sample_index = np.arange(start_sample, start_sample + sample_count, dtype=np.float64)
    return np.asarray(0.12 * np.sin(2.0 * math.pi * 220.0 * sample_index / rate), dtype=np.float64)


def _wav_pcm16_bytes(samples: FloatArray, sample_rate_hz: int) -> bytes:
    audio = np.asarray(samples, dtype=np.float64).reshape(-1)
    pcm = np.rint(np.clip(audio, -1.0, 0.999969482421875) * 32768.0).astype("<i2")
    from io import BytesIO

    stream = BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate_hz)
        writer.writeframes(pcm.tobytes())
    return stream.getvalue()


def _frame_png_bytes(frame_linear_rgb: FloatArray) -> bytes:
    display_rgb = color_science.srgb_encode(np.asarray(frame_linear_rgb, dtype=np.float64))
    rgb8 = np.rint(np.clip(display_rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    return color_science.encode_rgb8_png(rgb8)


def discover_ffmpeg_executable() -> str | None:
    """Resolve system FFmpeg, then the packaged imageio-ffmpeg binary if present."""
    executable = shutil.which("ffmpeg")
    if executable is not None:
        return executable
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError, FileNotFoundError):
        return None


def _mux_shot_if_available(
    fs: WorkspaceFilesystem,
    ffmpeg_binary: str | None,
    frame_paths: tuple[str, ...],
    audio_path: str,
    mux_path: str,
    *,
    frame_count: int,
    frame_rate_num: int,
    frame_rate_den: int,
) -> str | None:
    if ffmpeg_binary is None:
        return None
    output = fs.resolve(mux_path)
    first_frame = fs.resolve(frame_paths[0])
    audio = fs.resolve(audio_path)
    frame_pattern = str(first_frame.parent / "frame-%05d.png")
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-fflags",
        "+bitexact",
        "-framerate",
        f"{frame_rate_num}/{frame_rate_den}",
        "-i",
        frame_pattern,
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-frames:v",
        str(frame_count),
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-flags:v",
        "+bitexact",
        "-c:a",
        "pcm_s16le",
        "-flags:a",
        "+bitexact",
        "-map_metadata",
        "-1",
        "-f",
        "nut",
        str(output),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KernelError(f"FFmpeg mux failed: {exc}") from exc
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise KernelError(f"FFmpeg mux failed: {result.stderr.strip()}")
    digest, _ = fs.hash_file(mux_path)
    return digest


def execute_real_media_season_continuity(
    workspace_root: str | Path,
    *,
    shots: tuple[RealMediaShot, ...] | list[RealMediaShot] | None = None,
    show_bible_anchor: CharacterIdentityAnchor | None = None,
    episode_count: int = 4,
    shots_per_episode: int = 4,
    frame_count_per_shot: int = 12,
    frame_size: tuple[int, int] = (16, 16),
    sample_rate_hz: int = 48_000,
    frame_rate_num: int = 24_000,
    frame_rate_den: int = 1001,
    drift_kelvin_per_shot: float = 200.0,
    exposure_drift_per_shot: float = 0.12,
    scale_drift_per_shot: float = 0.0015,
    naive_frame_rate_fps: float = 23.98,
    ffmpeg_path: str | None = None,
) -> RealMediaSeasonReceipt:
    """Execute drift/control and anchored restoration over actual pixel/PCM buffers.

    When ``shots`` is omitted, reproducible multi-frame RGB, metric cranial
    landmarks, and float PCM buffers are synthesized. Supplied media tensors are
    accepted per ``RealMediaShot`` and are validated before processing. Each
    resulting RGB frame is written as PNG, PCM is written as 48 kHz WAV, and a
    bitexact FFV1/PCM NUT mux is made when FFmpeg is available (or explicitly given).
    """
    if (
        episode_count <= 0
        or shots_per_episode <= 0
        or frame_count_per_shot <= 0
        or min(frame_size) <= 4
        or sample_rate_hz != 48_000
        or min(frame_rate_num, frame_rate_den) <= 0
        or drift_kelvin_per_shot < 0
        or exposure_drift_per_shot < 0
        or scale_drift_per_shot < 0
        or naive_frame_rate_fps <= 0
    ):
        raise KernelError("invalid real-media season controls")
    anchor = _default_anchor() if show_bible_anchor is None else show_bible_anchor
    canonical_landmarks = np.asarray(anchor.cranial_landmarks_m, dtype=np.float64)
    reference_patch = np.asarray(anchor.reference_patch_linear_rgb, dtype=np.float64)
    y0, y1, x0, x1 = anchor.calibration_roi
    height, width = frame_size
    if (
        not anchor.character_id
        or canonical_landmarks.ndim != 2
        or canonical_landmarks.shape[1] != 3
        or len(canonical_landmarks) < 4
        or np.any(~np.isfinite(canonical_landmarks))
        or reference_patch.shape != (3,)
        or np.any(~np.isfinite(reference_patch))
        or not 2000 <= anchor.reference_illuminant_kelvin <= 15_000
        or not 0 <= anchor.left_landmark_index < len(canonical_landmarks)
        or not 0 <= anchor.right_landmark_index < len(canonical_landmarks)
        or anchor.left_landmark_index == anchor.right_landmark_index
        or not (0 <= y0 < y1 <= height and 0 <= x0 < x1 <= width)
    ):
        raise KernelError("invalid Show-Bible media anchor")

    total_shots = episode_count * shots_per_episode
    if shots is None:
        source_shots = [
            RealMediaShot(episode, shot)
            for episode in range(1, episode_count + 1)
            for shot in range(1, shots_per_episode + 1)
        ]
    else:
        source_shots = list(shots)
        expected_keys = [
            (episode, shot)
            for episode in range(1, episode_count + 1)
            for shot in range(1, shots_per_episode + 1)
        ]
        actual_keys = [(shot.episode_index, shot.shot_index) for shot in source_shots]
        if actual_keys != expected_keys:
            raise KernelError(
                "real-media shots must enumerate the complete ordered episode/shot grid"
            )

    raw_frames: list[FloatArray] = []
    frame_counts: list[int] = []
    for shot in source_shots:
        frames = (
            _synthesize_frames(
                frame_count_per_shot, height, width, anchor.reference_patch_linear_rgb
            )
            if shot.frames_linear_rgb is None
            else _validate_frames(shot.frames_linear_rgb)
        )
        if frames.shape[1:3] != frame_size or frames.shape[0] <= 0:
            raise KernelError("shot frame buffers must share the declared non-empty H×W")
        raw_frames.append(frames.copy())
        frame_counts.append(int(frames.shape[0]))
    sample_counts = bresenham_sample_counts(
        frame_counts,
        sample_rate_hz=sample_rate_hz,
        frame_rate_num=frame_rate_num,
        frame_rate_den=frame_rate_den,
    )
    sample_starts: list[int] = []
    sample_ends: list[int] = []
    sample_cursor = 0
    for count in sample_counts:
        sample_starts.append(sample_cursor)
        sample_cursor += count
        sample_ends.append(sample_cursor)
    total_frames = sum(frame_counts)
    exact_horizon = canonical_sample_horizon(
        total_frames,
        sample_rate_hz=sample_rate_hz,
        frame_rate_num=frame_rate_num,
        frame_rate_den=frame_rate_den,
    )
    if sample_cursor != exact_horizon:
        raise KernelError("restored media sample clock does not match the rational horizon")

    workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
    workspace_root.mkdir(parents=True, exist_ok=True)
    fs = WorkspaceFilesystem(workspace_root)
    ffmpeg_binary = ffmpeg_path or discover_ffmpeg_executable()
    if (
        ffmpeg_binary is not None
        and not Path(ffmpeg_binary).exists()
        and shutil.which(ffmpeg_binary) is None
    ):
        raise KernelError("requested FFmpeg executable was not found")

    shot_receipts: list[RealMediaShotReceipt] = []
    merkle_receipts: list[dict[str, object]] = []
    naive_audio_sample_count = 0
    muxed_count = 0
    global_shot_index = 0
    for source_shot, frames, shot_sample_count, sample_start, sample_end in zip(
        source_shots, raw_frames, sample_counts, sample_starts, sample_ends, strict=True
    ):
        frame_n = frame_counts[global_shot_index]
        if source_shot.audio_pcm_float is None:
            input_audio = _synthesize_audio(sample_start, shot_sample_count, sample_rate_hz)
        else:
            input_audio = np.asarray(source_shot.audio_pcm_float, dtype=np.float64).reshape(-1)
        aligned_audio, trimmed, padded = align_pcm_to_sample_count(input_audio, shot_sample_count)
        naive_count = round(frame_n * sample_rate_hz / naive_frame_rate_fps)
        naive_audio, _, _ = align_pcm_to_sample_count(input_audio, naive_count)
        naive_audio_sample_count += len(naive_audio)

        drift_step = global_shot_index + 1
        drift_kelvin = anchor.reference_illuminant_kelvin + drift_kelvin_per_shot * drift_step
        exposure = 1.0 + exposure_drift_per_shot * drift_step
        unanchored_frames = apply_illuminant_exposure_drift(
            frames,
            source_illuminant_kelvin=anchor.reference_illuminant_kelvin,
            drifted_illuminant_kelvin=drift_kelvin,
            exposure_multiplier=exposure,
        )
        restored_frames = restore_anchored_color_frames(
            unanchored_frames,
            drifted_illuminant_kelvin=drift_kelvin,
            reference_illuminant_kelvin=anchor.reference_illuminant_kelvin,
            exposure_multiplier=exposure,
            reference_patch_linear_rgb=anchor.reference_patch_linear_rgb,
            calibration_roi=anchor.calibration_roi,
        )
        unanchored_delta = measure_patch_delta_e00_by_frame(
            unanchored_frames, anchor.reference_patch_linear_rgb, anchor.calibration_roi
        )
        restored_delta = measure_patch_delta_e00_by_frame(
            restored_frames, anchor.reference_patch_linear_rgb, anchor.calibration_roi
        )

        source_landmarks = (
            canonical_landmarks
            if source_shot.cranial_landmarks_m is None
            else np.asarray(source_shot.cranial_landmarks_m, dtype=np.float64)
        )
        if source_landmarks.shape != canonical_landmarks.shape or np.any(
            ~np.isfinite(source_landmarks)
        ):
            raise KernelError("shot cranial landmarks do not match the Show-Bible anchor")
        axis = np.array([0.15, 0.25, 1.0], dtype=np.float64)
        rotation = geometry.rotation_matrix_from_axis_angle(axis, 0.001 * drift_step)
        scale = 1.0 + scale_drift_per_shot * drift_step
        translation = np.array([0.001 * drift_step, -0.0005 * drift_step, 0.0007 * drift_step])
        unanchored_landmarks = scale * source_landmarks @ rotation + translation
        outlier_index = len(unanchored_landmarks) - 1
        unanchored_landmarks[outlier_index] += np.array([0.08, -0.06, 0.05])
        identity_receipt = identity_lock.restore_cranial_landmarks_to_anchor(
            canonical_landmarks,
            unanchored_landmarks,
            left_index=anchor.left_landmark_index,
            right_index=anchor.right_landmark_index,
            huber_delta_m=0.005,
        )
        before_scale = identity_receipt.scale_drift_before
        after_scale = identity_receipt.scale_drift_after

        shot_rel = (
            f"media/episode-{source_shot.episode_index:04d}/shot-{source_shot.shot_index:04d}"
        )
        fs.ensure_directory(shot_rel)
        frame_paths: list[str] = []
        frame_hashes: list[str] = []
        for frame_index, frame in enumerate(restored_frames):
            frame_rel = f"{shot_rel}/frame-{frame_index:05d}.png"
            fs.write_bytes(frame_rel, _frame_png_bytes(frame))
            digest, _ = fs.hash_file(frame_rel)
            frame_paths.append(frame_rel)
            frame_hashes.append(digest)
        audio_rel = f"{shot_rel}/audio.wav"
        fs.write_bytes(audio_rel, _wav_pcm16_bytes(aligned_audio, sample_rate_hz))
        audio_digest, _ = fs.hash_file(audio_rel)
        mux_rel = f"{shot_rel}/mux.nut"
        mux_digest = _mux_shot_if_available(
            fs,
            ffmpeg_binary,
            tuple(frame_paths),
            audio_rel,
            mux_rel,
            frame_count=frame_n,
            frame_rate_num=frame_rate_num,
            frame_rate_den=frame_rate_den,
        )
        mux_path = mux_rel if mux_digest is not None else None
        muxed_count += int(mux_digest is not None)

        merkle_receipt: dict[str, object] = {
            "episode_index": source_shot.episode_index,
            "shot_index": source_shot.shot_index,
            "frame_count": frame_n,
            "audio_sample_count": shot_sample_count,
            "sample_start": sample_start,
            "sample_end": sample_end,
            "frame_sha256s": frame_hashes,
            "audio_sha256": audio_digest,
            "mux_sha256": mux_digest,
        }
        merkle_receipts.append(merkle_receipt)
        shot_receipts.append(
            RealMediaShotReceipt(
                source_shot.episode_index,
                source_shot.shot_index,
                frame_n,
                shot_sample_count,
                len(input_audio),
                trimmed,
                padded,
                sample_start,
                sample_end,
                before_scale,
                after_scale,
                identity_receipt.downweighted_landmark_count,
                unanchored_delta,
                restored_delta,
                tuple(frame_paths),
                tuple(frame_hashes),
                audio_rel,
                audio_digest,
                mux_path,
                mux_digest,
                {},
                restored_frames,
                identity_receipt.restored_landmarks_m,
            )
        )
        global_shot_index += 1

    root = merkle_ledger.merkle_root(merkle_receipts)
    for index, receipt in enumerate(merkle_receipts):
        proof = merkle_ledger.build_inclusion_proof(merkle_receipts, index)
        bundle = merkle_ledger.to_rfc6962_bundle(receipt, proof, root)
        if not merkle_ledger.verify_rfc6962_bundle(bundle):
            raise KernelError("on-disk shot artifact failed RFC 6962 inclusion verification")
        shot_receipts[index] = replace(shot_receipts[index], inclusion_bundle=bundle)
    root_hex = root.hex()
    naive_drift = abs(naive_audio_sample_count - exact_horizon)
    manifest = {
        "schema": "chronos-real-media-season-v1",
        "episode_count": episode_count,
        "shot_count": total_shots,
        "frame_count": total_frames,
        "sample_rate_hz": sample_rate_hz,
        "frame_rate": f"{frame_rate_num}/{frame_rate_den}",
        "sample_count": sample_cursor,
        "exact_sample_horizon": exact_horizon,
        "naive_audio_sample_count": naive_audio_sample_count,
        "naive_sample_clock_drift": naive_drift,
        "merkle_root_sha256": root_hex,
        "receipts": merkle_receipts,
        "inclusion_bundles": [shot.inclusion_bundle for shot in shot_receipts],
    }
    manifest_rel = "media/season-continuity-manifest.json"
    fs.write_text(manifest_rel, merkle_ledger.canonical_receipt_bytes(manifest).decode("utf-8"))
    persisted_manifest = json.loads(fs.read_text(manifest_rel))
    if persisted_manifest.get("merkle_root_sha256") != root_hex:
        raise KernelError("persisted season manifest root changed")
    verified_samples = sum(shot.audio_samples for shot in shot_receipts)
    if verified_samples != exact_horizon:
        raise KernelError("persisted PCM sample counts do not match the rational video horizon")
    return RealMediaSeasonReceipt(
        episode_count,
        total_shots,
        total_frames,
        sample_rate_hz,
        verified_samples,
        exact_horizon,
        verified_samples - exact_horizon,
        naive_drift,
        naive_audio_sample_count,
        muxed_count,
        root_hex,
        manifest_rel,
        tuple(shot_receipts),
    )
