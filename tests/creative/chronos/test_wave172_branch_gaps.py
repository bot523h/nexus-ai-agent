from __future__ import annotations

import struct
import subprocess
import zlib
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import (
    acoustic_space_continuity,
    biomechanics_motion,
    color_science,
    geometry,
    identity_lock,
    real_media_continuity,
    scene_portrait_ops,
    temporal_optics,
    verifier,
    weather_and_grading,
)
from nexus_ai_agent.creative.chronos.contracts import KernelError, RationalTimestamp
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png_bytes(raw: bytes, *, color_type: int = 2) -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def test_acoustic_fractional_impulse_at_buffer_edge() -> None:
    target = np.zeros(1, dtype=np.float64)
    acoustic_space_continuity._add_linear_fractional_impulse(target, 0.5, 1.0)
    assert target == pytest.approx([0.5])


def test_verlet_recovers_collapsed_predicted_segment_from_history() -> None:
    stamps = [RationalTimestamp(0, Fraction(1, 24)), RationalTimestamp(1, Fraction(1, 24))]
    result = biomechanics_motion.simulate_secondary_verlet_chain(
        stamps,
        np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
        np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
        gravity_m_s2=(0.0, 0.0, 0.0),
    )
    assert result.max_segment_strain < 1e-14


def test_png_decoder_ancillary_and_refusal_branches(tmp_path: Path) -> None:
    png = _png_bytes(b"\x00\x20\x40\x60")
    ancillary = _png_chunk(b"tEXt", b"comment\x00test")
    path = tmp_path / "ancillary.png"
    path.write_bytes(png[:33] + ancillary + png[33:])
    assert np.array_equal(color_science.read_rgb8_png(path), np.array([[[32, 64, 96]]], np.uint8))

    unsupported = tmp_path / "unsupported.png"
    unsupported.write_bytes(_png_bytes(b"\x00\x20\x40\x60", color_type=6))
    with pytest.raises(KernelError, match="unsupported PNG encoding"):
        color_science.read_rgb8_png(unsupported)

    bad_filter = tmp_path / "filter.png"
    bad_filter.write_bytes(_png_bytes(b"\x01\x20\x40\x60"))
    with pytest.raises(KernelError, match="unsupported PNG filter"):
        color_science.read_rgb8_png(bad_filter)

    short_scanline = tmp_path / "short.png"
    short_scanline.write_bytes(_png_bytes(b"\x00\x20\x40"))
    with pytest.raises(KernelError, match="scanline length"):
        color_science.read_rgb8_png(short_scanline)

    with pytest.raises(KernelError, match="positive definite"):
        color_science._symmetric_power(np.diag([1.0, 1.0, -1.0]), 0.5)


def test_singular_pixel_ray_is_refused() -> None:
    intrinsic = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0]])
    with pytest.raises(KernelError, match="singular pixel ray"):
        geometry.backproject_character_height_m((1.0, 0.0), (0.0, 0.0), 1.0, intrinsic)


def test_identity_outlier_refit_and_valid_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = np.random.default_rng(2026).normal(size=(16, 3)) * 0.05
    baseline_fit = identity_lock.restore_cranial_landmarks_to_anchor(reference, reference)
    assert baseline_fit.downweighted_landmark_count == 0
    observed = reference.copy()
    observed[-1] += np.array([0.5, -0.4, 0.35])
    result = identity_lock.restore_cranial_landmarks_to_anchor(reference, observed)
    assert result.downweighted_landmark_count >= 1
    baseline = identity_lock.verify_cranial_baseline(
        reference, reference, left_index=0, right_index=1
    )
    assert baseline.within_tolerance
    with pytest.raises(KernelError, match="invalid cranial baseline"):
        identity_lock.verify_cranial_baseline(np.zeros(3), np.zeros(3), left_index=0, right_index=1)

    monkeypatch.setattr(
        np.linalg,
        "svd",
        lambda matrix: (np.eye(3), np.zeros(3), np.eye(3)),
    )
    with pytest.raises(KernelError, match="non-positive scale"):
        identity_lock.restore_cranial_landmarks_to_anchor(reference, reference)


def test_scene_portrait_loop_edges_and_valid_retouch() -> None:
    source = np.arange(7 * 7 * 3, dtype=np.float64).reshape(7, 7, 3) / 100.0
    interior = np.zeros((7, 7), dtype=bool)
    interior[3, 3] = True
    scene_portrait_ops.inpaint_masked_region_pde(source, interior, iterations=1, tolerance=1e-20)
    border = np.zeros((7, 7), dtype=bool)
    border[0, 0] = True
    scene_portrait_ops.inpaint_masked_region_pde(source, border, iterations=2)
    retouched = scene_portrait_ops.retouch_portrait_frequency_separation(source, interior)
    assert retouched.shape == source.shape


def test_temporal_singleton_flicker_variance_is_zero() -> None:
    assert temporal_optics._temporal_flicker_variance(np.array([0.2])) == 0.0


def test_verifier_persisted_structure_threshold_refusal(tmp_path: Path) -> None:
    rows, columns = np.mgrid[0:16, 0:16]
    source = np.stack((rows / 16, columns / 16, (rows + columns) / 32), axis=-1).astype(float)
    target = np.clip(source * 0.6 + 0.15, 0, 1)
    with pytest.raises(KernelError, match="structure lock"):
        verifier.verify_persisted_relighting(source, target, target, tmp_path / "relit.png", 2.0)


def test_weather_lut_finite_domain_and_flat_out_of_range_paths() -> None:
    samples = "".join("0 0 0\n" for _ in range(8))
    parsed = weather_and_grading.parse_cube_lut(
        "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n" + samples
    )
    assert parsed.size == 2
    flat = weather_and_grading.legalize_broadcast_rgb_ebu_r103(np.full((2, 2, 3), -0.5))
    assert flat.illegal_pixel_fraction_before == 1.0


def test_real_media_internal_error_and_normalization_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert real_media_continuity._synthesize_frames(1, 8, 8, (0.2, 0.3, 0.4)).shape == (
        1,
        8,
        8,
        3,
    )
    assert real_media_continuity._synthesize_frames(1, 4, 4, (0.2, 0.3, 0.4)).shape == (
        1,
        4,
        4,
        3,
    )
    with pytest.raises(KernelError, match="WAV persistence"):
        real_media_continuity._wav_pcm16_bytes(np.zeros((2, 3)), 48_000)

    root = tmp_path / "mux-failure"
    root.mkdir()
    fs = WorkspaceFilesystem(root)
    fs.ensure_directory("shot")
    frame = "shot/frame-00000.png"
    audio = "shot/audio.wav"
    fs.write_bytes(frame, b"png")
    fs.write_bytes(audio, b"wav")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="injected mux refusal"),
    )
    with pytest.raises(KernelError, match="FFmpeg mux failed"):
        real_media_continuity._mux_shot_if_available(
            fs,
            "ffmpeg",
            (frame,),
            audio,
            "shot/mux.nut",
            frame_count=1,
            frame_rate_num=24,
            frame_rate_den=1,
        )


def test_real_media_clock_and_landmark_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_clock = real_media_continuity.canonical_sample_horizon
    calls = 0

    def drifted_clock(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        value = original_clock(*args, **kwargs)
        return value if calls == 1 else value + 1

    monkeypatch.setattr(real_media_continuity, "canonical_sample_horizon", drifted_clock)
    with pytest.raises(KernelError, match="sample clock does not match"):
        real_media_continuity.execute_real_media_season_continuity(
            tmp_path / "clock-refusal",
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=1,
            frame_size=(8, 8),
            ffmpeg_path="",
        )


def test_bresenham_horizon_invariant_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    original_clock = real_media_continuity.canonical_sample_horizon
    monkeypatch.setattr(
        real_media_continuity,
        "canonical_sample_horizon",
        lambda *args, **kwargs: original_clock(*args, **kwargs) + 1,
    )
    with pytest.raises(KernelError, match="exact sample horizon"):
        real_media_continuity.bresenham_sample_counts([2])


def test_scene_linear_color_correction_refuses_nonfinite_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(np.linalg, "inv", lambda matrix: np.full_like(matrix, np.nan))
    with pytest.raises(KernelError, match="non-finite pixels"):
        real_media_continuity.restore_anchored_color_frames(
            np.full((1, 8, 8, 3), 0.25),
            drifted_illuminant_kelvin=7000,
            reference_illuminant_kelvin=6500,
            exposure_multiplier=1.0,
            reference_patch_linear_rgb=(0.24, 0.34, 0.42),
            calibration_roi=(0, 4, 0, 4),
        )


def test_real_media_source_landmark_shape_refusal(tmp_path: Path) -> None:
    anchor = real_media_continuity._default_anchor()
    shot = real_media_continuity.RealMediaShot(
        1,
        1,
        cranial_landmarks_m=np.zeros((3, 3), dtype=np.float64),
    )
    with pytest.raises(KernelError, match="cranial landmarks do not match"):
        real_media_continuity.execute_real_media_season_continuity(
            tmp_path / "bad-landmarks",
            shots=[shot],
            show_bible_anchor=anchor,
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=1,
            frame_size=(8, 8),
            ffmpeg_path="",
        )
