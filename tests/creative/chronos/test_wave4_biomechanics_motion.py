from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.biomechanics_motion import (
    enforce_stance_foot_lock,
    render_shutter_motion_blur_linear,
    simulate_secondary_verlet_chain,
    solve_two_bone_or_chain_ik,
)
from nexus_ai_agent.creative.chronos.contracts import KernelError, RationalTimestamp


def _timestamps(count: int) -> list[RationalTimestamp]:
    return [RationalTimestamp(index, Fraction(1, 24)) for index in range(count)]


def test_ik_two_bone_chain_reach_limits_and_refusals() -> None:
    two_bone = solve_two_bone_or_chain_ik(np.zeros(3), (1.0, 1.0), np.array([1.0, 1.0, 0.0]))
    points = np.asarray(two_bone.joint_positions)
    assert two_bone.reachable and not two_bone.clamped
    assert two_bone.target_residual_m < 1e-7
    assert np.linalg.norm(np.diff(points, axis=0), axis=1) == pytest.approx([1.0, 1.0])

    chain = solve_two_bone_or_chain_ik(np.zeros(3), (1.0, 0.8, 0.6), np.array([1.5, 0.2, 0.0]))
    assert chain.reachable and chain.iterations < 200
    assert len(chain.joint_positions) == 4
    vertical = solve_two_bone_or_chain_ik(np.zeros(3), (1.0, 1.0), np.array([0.0, 0.0, 1.5]))
    assert vertical.target_residual_m < 1e-6

    bounded = solve_two_bone_or_chain_ik(
        np.zeros(3),
        (1.0, 1.0),
        np.array([0.0, 1.5, 0.0]),
        joint_angle_limits_rad=(-0.1, 0.1),
    )
    assert all(-0.1 <= angle <= 0.1 for angle in bounded.joint_angles_radians)
    assert bounded.target_residual_m > 0.1

    with pytest.raises(KernelError, match="exceeds total chain"):
        solve_two_bone_or_chain_ik(np.zeros(3), (1.0, 1.0), np.array([3.0, 0.0, 0.0]))
    clamped = solve_two_bone_or_chain_ik(
        np.zeros(3), (1.0, 1.0), np.array([3.0, 0.0, 0.0]), clamp_unreachable=True
    )
    assert clamped.clamped and not clamped.reachable
    assert clamped.target_residual_m == pytest.approx(1.0, abs=1e-8)
    with pytest.raises(KernelError):
        solve_two_bone_or_chain_ik(np.zeros(3), (1.0,), np.ones(3))
    with pytest.raises(KernelError, match="angle limits"):
        solve_two_bone_or_chain_ik(
            np.zeros(3), (1.0, 1.0), np.ones(3), joint_angle_limits_rad=((1, -1), (0, 1))
        )
    with pytest.raises(KernelError, match="finite 3D"):
        solve_two_bone_or_chain_ik(np.ones(2), (1.0, 1.0), np.ones(3))
    with pytest.raises(KernelError, match="solver controls"):
        solve_two_bone_or_chain_ik(np.zeros(3), (1.0, 1.0), np.ones(3), damping=0.0)


def test_foot_lock_zero_slip_and_validation() -> None:
    stamps = _timestamps(7)
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.1, 0.0, 0.02],
            [0.2, 0.0, 0.01],
            [0.35, 0.0, 0.0],
            [0.5, 0.0, 0.03],
            [0.6, 0.0, 0.01],
            [0.7, 0.0, 0.0],
        ]
    )
    stance = np.array([False, True, True, True, True, False, False])
    result = enforce_stance_foot_lock(stamps, positions, stance, transition_frames=2)
    locked = np.asarray(result.positions)
    assert result.max_foot_slip_mm_before == pytest.approx(150.0)
    assert result.max_foot_slip_mm_after == 0.0
    assert np.array_equal(locked[1:5, :2], np.repeat(positions[1:2, :2], 4, axis=0))
    assert locked[1, 2] == positions[1, 2]
    assert result.locked_frame_count == 4

    no_stance = enforce_stance_foot_lock(stamps, positions, np.zeros(7, bool))
    assert no_stance.max_foot_slip_mm_after == 0.0
    no_ramp = enforce_stance_foot_lock(stamps, positions, stance, transition_frames=0)
    assert np.array_equal(np.asarray(no_ramp.positions)[1:5, :2], locked[1:5, :2])
    with pytest.raises(KernelError, match="matching"):
        enforce_stance_foot_lock(stamps, positions[:-1], stance)
    with pytest.raises(KernelError, match="strictly increasing"):
        enforce_stance_foot_lock(
            [RationalTimestamp(0, Fraction(1, 24)), RationalTimestamp(0, Fraction(1, 24))],
            np.zeros((2, 3)),
            np.array([True, True]),
        )
    with pytest.raises(KernelError, match="share a time base"):
        enforce_stance_foot_lock(
            [RationalTimestamp(0, Fraction(1, 24)), RationalTimestamp(1, Fraction(1, 30))],
            np.zeros((2, 3)),
            np.array([True, True]),
        )
    with pytest.raises(KernelError, match="matching"):
        enforce_stance_foot_lock(stamps, positions, stance, transition_frames=-1)


def test_verlet_chain_bounded_strain_root_coupling_and_fail_closed() -> None:
    stamps = _timestamps(6)
    initial = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 2.0]])
    roots = np.column_stack((np.linspace(0, 0.2, 6), np.zeros(6), np.zeros(6)))
    result = simulate_secondary_verlet_chain(
        stamps, roots, initial, wind_m_s2=(0.3, 0.0, 0.0), constraint_iterations=3
    )
    all_positions = np.asarray(result.positions)
    assert result.max_segment_strain < 1e-14
    assert np.allclose(all_positions[:, 0], roots)
    assert all_positions[-1, 1, 0] != all_positions[0, 1, 0]

    collapsed = simulate_secondary_verlet_chain(
        stamps[:2],
        np.zeros((2, 3)),
        np.zeros((2, 3)),
        rest_lengths_m=(1.0,),
    )
    assert collapsed.max_segment_strain < 1e-14
    with pytest.raises(KernelError, match="invalid Verlet"):
        simulate_secondary_verlet_chain(stamps, roots[:-1], initial)
    with pytest.raises(KernelError, match="strictly increasing"):
        simulate_secondary_verlet_chain([stamps[0], stamps[0]], np.zeros((2, 3)), initial)
    with pytest.raises(KernelError, match="rest length"):
        simulate_secondary_verlet_chain(stamps, roots, initial, rest_lengths_m=(0.0, 1.0))
    with pytest.raises(KernelError, match="gravity"):
        simulate_secondary_verlet_chain(stamps, roots, initial, gravity_m_s2=(0.0, 0.0))
    with pytest.raises(KernelError, match="solver controls"):
        simulate_secondary_verlet_chain(stamps, roots, initial, drag=1.0)


def test_scene_linear_shutter_blur_conserves_energy_and_refuses_invalid() -> None:
    layer = np.zeros((12, 10, 3))
    layer[4:8, 4:6] = (4.0, 1.0, 0.5)
    result = render_shutter_motion_blur_linear(
        layer, (3.5, -0.75), shutter_angle_deg=180, subframe_samples=11
    )
    assert result.shutter_fraction == 0.5
    assert result.energy_relative_error < 1e-12
    assert result.image.sum() == pytest.approx(layer.sum(), rel=1e-12)
    assert not np.array_equal(result.image, layer)
    zero_motion = render_shutter_motion_blur_linear(layer, (0.0, 0.0), shutter_angle_deg=360)
    assert np.array_equal(zero_motion.image, layer)
    gray = render_shutter_motion_blur_linear(np.ones((3, 4, 1)), (1.0, 0.0), subframe_samples=1)
    assert np.array_equal(gray.image, np.ones((3, 4, 1)))
    rgba = render_shutter_motion_blur_linear(np.ones((3, 4, 4)), (0.25, 0.25))
    assert rgba.image.shape == (3, 4, 4)
    with pytest.raises(KernelError, match="invalid shutter"):
        render_shutter_motion_blur_linear(layer, (0.0, 0.0), shutter_angle_deg=0)
    with pytest.raises(KernelError, match="invalid shutter"):
        render_shutter_motion_blur_linear(layer, (0.0, 0.0), subframe_samples=0)
    with pytest.raises(KernelError, match="invalid shutter"):
        render_shutter_motion_blur_linear(-layer, (0.0, 0.0))
    with pytest.raises(KernelError, match="invalid shutter"):
        render_shutter_motion_blur_linear(layer[:, :, :2], (0.0, 0.0))


def test_wave4_motion_handlers_write_receipts(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.chronos.chronos_port import ChronosCommand, ChronosPort

    port = ChronosPort(build_default_chronos_handlers())
    ik_input = tmp_path / "ik.npz"
    np.savez(ik_input, root=np.zeros(3), target=np.array([1.0, 1.0, 0.0]))
    ik_output = tmp_path / "ik.txt"
    ik_receipt = port.execute(
        ChronosCommand(
            "motion.solve_ik",
            (str(ik_input),),
            {"output": str(ik_output), "segment_lengths_m": "1,1"},
        )
    )
    assert ik_output.exists() and ik_receipt.metadata["reachable"] == "True"

    frame_input = tmp_path / "feet.npz"
    positions = np.array([[0.0, 0.0, 0.0], [0.2, 0.0, 0.0], [0.4, 0.0, 0.0]])
    np.savez(frame_input, positions=positions, stance=np.array([False, True, True]))
    foot_output = tmp_path / "feet.txt"
    foot = port.execute(
        ChronosCommand(
            "motion.foot_lock",
            (str(frame_input),),
            {
                "output": str(foot_output),
                "pts": "0,1,2",
                "time_base_num": "1",
                "time_base_den": "24",
            },
        )
    )
    assert foot.metadata["slip_after_mm"] == "0.0"

    verlet_input = tmp_path / "chain-input.npz"
    np.savez(
        verlet_input,
        root_positions=np.zeros((2, 3)),
        initial_chain=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
    )
    verlet_output = tmp_path / "chain.npz"
    verlet = port.execute(
        ChronosCommand(
            "motion.verlet_chain",
            (str(verlet_input),),
            {
                "output": str(verlet_output),
                "pts": "0,1",
                "time_base_num": "1",
                "time_base_den": "24",
            },
        )
    )
    with np.load(verlet_output, allow_pickle=False) as data:
        assert data["positions"].shape == (2, 2, 3)
    assert float(verlet.metadata["max_segment_strain"]) < 1e-4

    image_input = tmp_path / "layer.npy"
    np.save(image_input, np.ones((5, 5, 3)))
    blur_output = tmp_path / "blur.npy"
    blur = port.execute(
        ChronosCommand(
            "motion.shutter_blur",
            (str(image_input),),
            {
                "output": str(blur_output),
                "velocity_x_px_per_frame": "2",
                "velocity_y_px_per_frame": "0",
            },
        )
    )
    assert np.load(blur_output, allow_pickle=False).shape == (5, 5, 3)
    assert float(blur.metadata["energy_relative_error"]) < 1e-12
