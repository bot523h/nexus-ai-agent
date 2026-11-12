from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.contracts import KernelError
from nexus_ai_agent.creative.chronos.temporal_optics import (
    estimate_subpixel_translation_phase_corr,
    rectify_rolling_shutter_skew,
    stabilize_temporal_exposure_flicker,
)


def _fourier_shift(frame: np.ndarray, dx: float, dy: float) -> np.ndarray:
    height, width = frame.shape
    frequency_y = np.fft.fftfreq(height)[:, None]
    frequency_x = np.fft.fftfreq(width)[None, :]
    shift = np.exp(-2j * np.pi * (frequency_x * dx + frequency_y * dy))
    return np.fft.ifft2(np.fft.fft2(frame) * shift).real


def test_phase_correlation_subpixel_accuracy_hann_window() -> None:
    rng = np.random.default_rng(4)
    reference = rng.normal(size=(96, 112))
    dx, dy = 1.35, -0.7
    shifted = _fourier_shift(reference, dx, dy)
    receipt = estimate_subpixel_translation_phase_corr(reference, shifted)
    assert abs(receipt.translation_x_px - dx) < 0.05
    assert abs(receipt.translation_y_px - dy) < 0.05
    assert receipt.peak_to_sidelobe_ratio > 1.0

    rgb = np.repeat(reference[..., None], 3, axis=2)
    shifted_rgb = np.repeat(shifted[..., None], 3, axis=2)
    rgb_receipt = estimate_subpixel_translation_phase_corr(rgb, shifted_rgb)
    assert rgb_receipt.translation_x_px == pytest.approx(receipt.translation_x_px, abs=1e-8)
    flat = estimate_subpixel_translation_phase_corr(np.zeros((8, 8)), np.zeros((8, 8)))
    assert flat.translation_x_px == 0 and flat.translation_y_px == 0
    narrow = estimate_subpixel_translation_phase_corr(np.ones((3, 3)), np.ones((3, 3)))
    assert np.isfinite(narrow.peak_to_sidelobe_ratio)


def test_phase_correlation_invalid_inputs() -> None:
    image = np.ones((8, 8))
    with pytest.raises(KernelError, match="matching finite"):
        estimate_subpixel_translation_phase_corr(image, np.ones((8, 9)))
    with pytest.raises(KernelError, match="matching finite"):
        estimate_subpixel_translation_phase_corr(np.full((8, 8), np.nan), image)
    with pytest.raises(KernelError, match="matching finite"):
        estimate_subpixel_translation_phase_corr(image, image, epsilon=0)
    with pytest.raises(KernelError, match="matching finite"):
        estimate_subpixel_translation_phase_corr(np.ones((2, 8)), np.ones((2, 8)))
    with pytest.raises(KernelError, match="grayscale or RGB"):
        estimate_subpixel_translation_phase_corr(np.ones((8, 8, 2)), np.ones((8, 8, 2)))


def test_temporal_deflicker_rejects_alternating_exposure_and_preserves_fade() -> None:
    exposure = np.array([0.025 * frame + (0.28 if frame % 2 else -0.28) for frame in range(20)])
    luminance = np.exp(exposure)
    frames = np.broadcast_to(luminance[:, None, None, None], (20, 12, 10, 3)).copy()
    receipt = stabilize_temporal_exposure_flicker(frames, smoothing_radius=4)
    assert receipt.meets_variance_target
    assert receipt.temporal_flicker_variance_after < 0.20 * receipt.temporal_flicker_variance_before
    assert np.asarray(receipt.frames).shape == frames.shape
    before_slope = np.polyfit(np.arange(20), receipt.log_luminance_before, 1)[0]
    after_slope = np.polyfit(np.arange(20), receipt.log_luminance_after, 1)[0]
    assert after_slope == pytest.approx(before_slope, abs=0.01)
    assert len(receipt.gain_by_frame) == len(frames)

    steady = stabilize_temporal_exposure_flicker(np.ones((4, 3, 2, 1)))
    assert steady.temporal_flicker_variance_before == 0.0
    assert steady.meets_variance_target
    no_filter = stabilize_temporal_exposure_flicker(frames, smoothing_radius=0)
    assert no_filter.temporal_flicker_variance_after == pytest.approx(
        no_filter.temporal_flicker_variance_before
    )
    assert not no_filter.meets_variance_target


def test_temporal_deflicker_invalid_inputs() -> None:
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.ones((8, 8, 3)))
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.full((3, 4, 4, 3), -1.0))
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.full((3, 4, 4, 3), np.nan))
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.ones((3, 4, 4, 2)))
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.ones((3, 4, 4, 3)), smoothing_radius=-1)
    with pytest.raises(KernelError, match="N x H x W"):
        stabilize_temporal_exposure_flicker(np.ones((3, 4, 4, 3)), epsilon=0)


def test_rolling_shutter_inverse_warp_subpixel_and_boundaries() -> None:
    height, width = 6, 10
    source = np.broadcast_to(np.arange(width, dtype=float), (height, width)).copy()
    receipt = rectify_rolling_shutter_skew(
        source,
        (10.0, 4.0),
        readout_duration_s=0.01,
        frame_period_s=0.02,
    )
    expected_top = np.clip(2 + 10 * 0.01 * (0 / height - 0.5), 0, width - 1)
    assert receipt.frame[0, 2] == pytest.approx(expected_top)
    assert receipt.max_warp_px == pytest.approx(0.05)
    assert receipt.readout_fraction == pytest.approx(0.5)
    assert not np.array_equal(receipt.frame, source)
    gray = rectify_rolling_shutter_skew(
        np.ones((4, 4, 1)), (0.0, 0.0), readout_duration_s=0.0, frame_period_s=1 / 24
    )
    assert gray.frame.shape == (4, 4, 1) and np.all(gray.frame == 1.0)
    rgba = rectify_rolling_shutter_skew(
        np.ones((3, 3, 4)), (1.0, 0.0), readout_duration_s=0.01, frame_period_s=0.02
    )
    assert rgba.frame.shape == (3, 3, 4)


def test_rolling_shutter_refusals() -> None:
    frame = np.ones((5, 5, 3))
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(frame, (1.0, 0.0), readout_duration_s=-0.1, frame_period_s=1)
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(frame, (1.0, 0.0), readout_duration_s=2, frame_period_s=1)
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(frame, (1.0, 0.0), readout_duration_s=1, frame_period_s=0)
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(frame, (np.nan, 0.0), readout_duration_s=0.1, frame_period_s=1)
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(
            np.ones((4, 4, 2)), (0.0, 0.0), readout_duration_s=0, frame_period_s=1
        )
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(
            np.ones((4, 4)), (0.0,), readout_duration_s=0, frame_period_s=1
        )
    with pytest.raises(KernelError, match="invalid rolling-shutter"):
        rectify_rolling_shutter_skew(
            np.full((4, 4), np.inf), (0.0, 0.0), readout_duration_s=0, frame_period_s=1
        )


def test_wave5_temporal_handlers_write_numpy_outputs(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    port = ChronosPort(build_default_chronos_handlers())
    height, width = 32, 40
    rng = np.random.default_rng(6)
    reference = rng.normal(size=(height, width))
    shifted = _fourier_shift(reference, 0.5, -0.25)
    ref_path, moved_path = tmp_path / "ref.npy", tmp_path / "moved.npy"
    np.save(ref_path, reference)
    np.save(moved_path, shifted)
    phase_output = tmp_path / "phase.txt"
    phase = port.execute(
        ChronosCommand(
            "temporal.phase_corr",
            (str(ref_path), str(moved_path)),
            {"output": str(phase_output)},
        )
    )
    assert phase_output.exists() and phase.metadata["dx_px"]

    frames_path = tmp_path / "flicker.npy"
    levels = np.array([1.0, 1.4, 0.8, 1.5, 0.9, 1.3])
    np.save(frames_path, np.broadcast_to(levels[:, None, None, None], (6, 5, 5, 3)).copy())
    fixed_path = tmp_path / "fixed.npy"
    fixed = port.execute(
        ChronosCommand("temporal.deflicker", (str(frames_path),), {"output": str(fixed_path)})
    )
    assert np.load(fixed_path, allow_pickle=False).shape == (6, 5, 5, 3)
    assert fixed.metadata["meets_target"] == "True"

    rolling_path = tmp_path / "rolling.npy"
    np.save(rolling_path, np.ones((4, 5, 3)))
    rectified_path = tmp_path / "rectified.npy"
    rectified = port.execute(
        ChronosCommand(
            "temporal.rectify_rolling",
            (str(rolling_path),),
            {
                "output": str(rectified_path),
                "velocity_x_px_per_second": "20",
                "velocity_y_px_per_second": "1",
                "readout_duration_s": "0.01",
                "frame_period_s": "0.02",
            },
        )
    )
    assert np.load(rectified_path, allow_pickle=False).shape == (4, 5, 3)
    assert float(rectified.metadata["readout_fraction"]) == 0.5
