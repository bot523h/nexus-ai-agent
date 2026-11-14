"""Phase-correlation, scanline rectification, and match-on-action cut coverage."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.kinetic_match_cut_engine import (
    _hermite_bridge,
    estimate_subpixel_phase_translation,
    optimize_match_on_action_cut,
    rectify_rolling_shutter_frame,
)


def test_phase_correlation_hann_window_wrapper() -> None:
    rng = np.random.default_rng(104)
    image = rng.normal(size=(48, 64))
    dx, dy = 1.35, -0.7
    fy = np.fft.fftfreq(image.shape[0])[:, None]
    fx = np.fft.fftfreq(image.shape[1])[None, :]
    shifted = np.fft.ifft2(np.fft.fft2(image) * np.exp(-2j * np.pi * (fx * dx + fy * dy))).real
    receipt = estimate_subpixel_phase_translation(image, shifted)
    assert receipt.translation_x_px == pytest.approx(dx, abs=0.05)
    assert receipt.translation_y_px == pytest.approx(dy, abs=0.05)
    assert receipt.peak_to_sidelobe_ratio > 1.0


def test_rolling_shutter_velocity_unit_conversion_and_readout() -> None:
    frame = np.zeros((20, 24), dtype=np.float64)
    frame[8, 8] = 1.0
    receipt = rectify_rolling_shutter_frame(
        frame,
        (4.0, 0.0),
        readout_fraction=0.5,
        frame_period_s=0.1,
    )
    assert receipt.rectified_frame.shape == frame.shape
    assert receipt.readout_fraction == 0.5
    assert receipt.max_warp_px == pytest.approx(1.0)
    assert np.all(np.isfinite(receipt.rectified_frame))


def test_global_match_action_cost_and_hermite_handoff() -> None:
    outgoing_pose = np.array([[[0.0, 0.0, 2.0]], [[4.0, 0.0, 2.0]], [[8.0, 0.0, 2.0]]])
    incoming_pose = np.array([[[10.0, 0.0, 2.0]], [[0.05, 0.0, 2.0]], [[5.0, 0.0, 2.0]]])
    outgoing_velocity = np.array([[[0.0, 0.0]], [[40.0, 0.0]], [[40.0, 0.0]]])
    incoming_velocity = np.array([[[10.0, 0.0]], [[0.5, 0.0]], [[40.0, 0.0]]])
    receipt = optimize_match_on_action_cut(
        outgoing_pose,
        outgoing_velocity,
        incoming_pose,
        incoming_velocity,
        pose_weight=1.0,
        velocity_weight=0.1,
        speed_weight=0.05,
        bridge_duration_s=0.2,
        bridge_sample_count=9,
        source_frame_rate_fps=10.0,
    )
    assert receipt.outgoing_frame_index == 0
    assert receipt.incoming_frame_index == 1
    assert receipt.selected_cost == pytest.approx(float(np.min(receipt.cost_matrix)))
    assert receipt.cost_reduction == pytest.approx(
        receipt.unaligned_cut_cost - receipt.selected_cost
    )
    assert receipt.bridge_duration_s == 0.2
    assert receipt.bridge_landmarks_3d.shape == (9, 1, 3)
    assert np.allclose(receipt.bridge_landmarks_3d[0], outgoing_pose[0])
    assert np.allclose(receipt.bridge_landmarks_3d[-1], incoming_pose[1])
    assert np.all(np.isfinite(receipt.bridge_velocities_3d_per_s))


def test_endpoint_velocity_fallbacks_and_single_frame_cut() -> None:
    positions = np.array([[[10.0, 0.0, 1.0]], [[0.0, 0.0, 1.0]]])
    velocities = np.zeros((2, 1, 2))
    incoming_positions = np.array([[[20.0, 0.0, 1.0]], [[0.0, 0.0, 1.0]]])
    last_to_last = optimize_match_on_action_cut(
        positions,
        velocities,
        incoming_positions,
        velocities,
        source_frame_rate_fps=10.0,
    )
    assert (last_to_last.outgoing_frame_index, last_to_last.incoming_frame_index) == (1, 1)

    one_pose = np.array([[[3.0, 2.0, 1.0]]])
    one_velocity = np.zeros((1, 1, 2))
    single = optimize_match_on_action_cut(one_pose, one_velocity, one_pose, one_velocity)
    assert np.allclose(single.bridge_velocities_3d_per_s, 0.0)
    assert np.allclose(single.bridge_landmarks_3d, one_pose)


def test_match_action_refuses_malformed_trajectories_and_controls() -> None:
    poses = np.zeros((2, 1, 3), dtype=np.float64)
    screen_velocity = np.zeros((2, 1, 2), dtype=np.float64)
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(poses, screen_velocity[:, :, :1], poses, screen_velocity)
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(
            poses,
            screen_velocity,
            np.zeros((2, 2, 3)),
            np.zeros((2, 2, 2)),
        )
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(poses, screen_velocity, poses[:, :0], screen_velocity[:, :0])
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(
            poses, screen_velocity, poses, screen_velocity, pose_weight=-1.0
        )
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(
            poses, screen_velocity, poses, screen_velocity, bridge_duration_s=0.0
        )
    with pytest.raises(ValueError):
        optimize_match_on_action_cut(
            poses,
            screen_velocity,
            poses,
            screen_velocity,
            pose_weight=0,
            velocity_weight=0,
            speed_weight=0,
        )
    enormous_pose = np.array([[[1e308, 0.0, 0.0]]])
    opposing_pose = np.array([[[-1e308, 0.0, 0.0]]])
    with pytest.raises(ValueError, match="overflowed"):
        optimize_match_on_action_cut(
            enormous_pose,
            np.zeros((1, 1, 2)),
            opposing_pose,
            np.zeros((1, 1, 2)),
        )
    with pytest.raises(ValueError):
        _hermite_bridge(
            np.zeros((1, 3)),
            np.zeros((1, 3)),
            np.zeros((2, 3)),
            np.zeros((1, 3)),
            duration_s=0.1,
            sample_count=4,
        )


def test_rolling_shutter_refuses_bad_controls() -> None:
    frame = np.ones((8, 9))
    with pytest.raises(ValueError):
        rectify_rolling_shutter_frame(frame, (1.0, 0.0), readout_fraction=1.1, frame_period_s=0.1)
    with pytest.raises(ValueError):
        rectify_rolling_shutter_frame(
            frame, (np.nan, 0.0), readout_fraction=0.5, frame_period_s=0.1
        )
    with pytest.raises(ValueError):
        rectify_rolling_shutter_frame(frame, (1.0, 0.0), readout_fraction=0.5, frame_period_s=0.0)
