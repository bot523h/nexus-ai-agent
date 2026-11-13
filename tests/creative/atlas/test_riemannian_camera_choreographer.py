"""WAVE 21 — tests for the Riemannian minimum-snap camera choreographer.

Checks the minimum-snap spline (knot interpolation, ``C^3`` continuity, boundary
rest, snap optimality), the ``SO(3)`` orientation path (orthonormality,
determinant ``+1``), and the CBF safety shield (keep-out enforcement, minimum-norm
active-set correction).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import riemannian_camera_choreographer as rcc


def _positions() -> np.ndarray:
    return np.array(
        [[0.0, 0.0, 0.0], [1.0, 1.0, 0.5], [2.0, 0.0, 1.0], [3.0, 2.0, 0.2], [4.0, 1.0, 1.5]]
    )


def _times() -> np.ndarray:
    return np.array([0.0, 1.0, 2.5, 4.0, 5.5])


def _rotation(axis: list[float], angle: float) -> np.ndarray:
    a = np.array(axis, dtype=np.float64)
    a = a / np.linalg.norm(a)
    k = rcc._skew(a)
    result: np.ndarray = np.eye(3) + math.sin(angle) * k + (1.0 - math.cos(angle)) * (k @ k)
    return result


# --- minimum-snap position trajectory --------------------------------------------


def test_minimum_snap_interpolates_keyframes() -> None:
    positions = _positions()
    times = _times()
    trajectory = rcc.plan_minimum_snap_trajectory(positions, times)
    for index in range(positions.shape[0]):
        assert np.allclose(trajectory.sample(float(times[index])), positions[index], atol=1e-8)


def test_minimum_snap_continuity_residual_is_tiny() -> None:
    trajectory = rcc.plan_minimum_snap_trajectory(_positions(), _times())
    assert trajectory.continuity_residual < 1e-9
    assert trajectory.boundary_residual < 1e-9


def test_minimum_snap_is_optimal_under_perturbation() -> None:
    trajectory = rcc.plan_minimum_snap_trajectory(_positions(), _times())
    baseline = trajectory.snap_cost()
    rng = np.random.default_rng(0)
    perturbed = trajectory.coefficients + 1e-4 * rng.normal(size=trajectory.coefficients.shape)
    # Recompute the snap cost of the perturbed coefficients directly.
    cost = 0.0
    for segment in range(perturbed.shape[0]):
        duration = trajectory.knot_times[segment + 1] - trajectory.knot_times[segment]
        matrix = rcc._segment_cost_matrix(float(duration))
        cost += float(np.trace(perturbed[segment].T @ matrix @ perturbed[segment]))
    assert cost > baseline


def test_minimum_snap_rejects_wrong_position_shape() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(np.zeros((3, 2)), _times()[:3])


def test_minimum_snap_rejects_non_monotone_times() -> None:
    times = np.array([0.0, 2.0, 1.0, 3.0, 4.0])
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(_positions(), times)


def test_minimum_snap_rejects_too_few_keyframes() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(np.zeros((1, 3)), np.zeros(1))


def test_minimum_snap_rejects_too_many_keyframes() -> None:
    k = rcc.MAX_KEYFRAMES + 1
    positions = np.zeros((k, 3))
    times = np.arange(k, dtype=np.float64)
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(positions, times)


def test_minimum_snap_rejects_non_finite_positions() -> None:
    positions = _positions()
    positions[0, 0] = np.nan
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(positions, _times())


def test_minimum_snap_rejects_mismatched_time_count() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.plan_minimum_snap_trajectory(_positions(), _times()[:3])


def test_minimum_snap_sample_outside_span_raises() -> None:
    trajectory = rcc.plan_minimum_snap_trajectory(_positions(), _times())
    with pytest.raises(rcc.CameraChoreographyError):
        trajectory.sample(100.0)


# --- SO(3) orientation path -------------------------------------------------------


def test_orientation_path_stays_on_so3() -> None:
    rotations = np.stack(
        [
            _rotation([0, 0, 1], 0.0),
            _rotation([0, 1, 0], 0.5),
            _rotation([1, 0, 0], 0.9),
            _rotation([0, 0, 1], 1.3),
        ]
    )
    times = np.array([0.0, 1.0, 2.0, 3.0])
    trajectory = rcc.interpolate_orientation_bspline(rotations, times)
    assert trajectory.is_on_so3 is True
    assert trajectory.max_orthonormality_residual < rcc.SO3_TOLERANCE
    assert trajectory.min_determinant > 1.0 - 1e-9


def test_orientation_path_hits_endpoints() -> None:
    rotations = np.stack([_rotation([0, 0, 1], 0.0), _rotation([0, 0, 1], 1.0)])
    trajectory = rcc.interpolate_orientation_bspline(rotations, np.array([0.0, 1.0]))
    assert np.allclose(trajectory.rotations[0], rotations[0], atol=1e-9)
    assert np.allclose(trajectory.rotations[-1], rotations[-1], atol=1e-9)


def test_orientation_path_sample_count() -> None:
    rotations = np.stack(
        [_rotation([1, 0, 0], 0.0), _rotation([1, 0, 0], 0.4), _rotation([1, 0, 0], 0.8)]
    )
    trajectory = rcc.interpolate_orientation_bspline(
        rotations, np.array([0.0, 1.0, 2.0]), samples_per_segment=4
    )
    assert trajectory.rotations.shape == (2 * 4 + 1, 3, 3)


def test_orientation_rejects_non_orthonormal_keyframe() -> None:
    bad = np.eye(3) * 2.0
    rotations = np.stack([bad, np.eye(3)])
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.interpolate_orientation_bspline(rotations, np.array([0.0, 1.0]))


def test_orientation_rejects_wrong_shape() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.interpolate_orientation_bspline(np.zeros((2, 3)), np.array([0.0, 1.0]))


def test_orientation_rejects_bad_samples_per_segment() -> None:
    rotations = np.stack([np.eye(3), _rotation([0, 0, 1], 0.5)])
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.interpolate_orientation_bspline(rotations, np.array([0.0, 1.0]), samples_per_segment=0)


def test_orientation_rejects_non_monotone_times() -> None:
    rotations = np.stack([np.eye(3), _rotation([0, 0, 1], 0.5)])
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.interpolate_orientation_bspline(rotations, np.array([1.0, 0.0]))


# --- CBF safety shield ------------------------------------------------------------


def test_cbf_shield_enforces_keep_out() -> None:
    points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [5.0, 5.0, 5.0]])
    obstacles = np.array([[1.0, 0.0, 0.0, 0.5]])
    report = rcc.apply_cbf_safety_shield(points, obstacles, delta_safe=1e-3)
    assert report.safe is True
    assert report.min_barrier_value >= 1e-3 - 1e-9
    assert 0 in report.active_constraints


def test_cbf_shield_leaves_clear_path_untouched() -> None:
    points = np.array([[10.0, 0.0, 0.0], [11.0, 0.0, 0.0]])
    obstacles = np.array([[0.0, 0.0, 0.0, 0.5]])
    report = rcc.apply_cbf_safety_shield(points, obstacles)
    assert np.allclose(report.positions, points)
    assert report.max_correction == 0.0


def test_cbf_shield_handles_centre_coincidence() -> None:
    points = np.array([[1.0, 0.0, 0.0]])
    obstacles = np.array([[1.0, 0.0, 0.0, 0.5]])
    report = rcc.apply_cbf_safety_shield(points, obstacles)
    assert report.safe is True


def test_cbf_shield_counts_roll_violations() -> None:
    points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    obstacles = np.array([[5.0, 5.0, 5.0, 0.5]])
    rolls = np.array([0.0, math.radians(30.0)])
    report = rcc.apply_cbf_safety_shield(points, obstacles, roll_angles=rolls)
    assert report.roll_violations == 1


def test_cbf_shield_rejects_wrong_position_shape() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(np.zeros((2, 2)), np.array([[0.0, 0.0, 0.0, 1.0]]))


def test_cbf_shield_rejects_wrong_obstacle_shape() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(np.zeros((2, 3)), np.array([[0.0, 0.0, 1.0]]))


def test_cbf_shield_rejects_empty_obstacles() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(np.zeros((2, 3)), np.zeros((0, 4)))


def test_cbf_shield_rejects_non_positive_radius() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(np.zeros((2, 3)), np.array([[0.0, 0.0, 0.0, 0.0]]))


def test_cbf_shield_rejects_non_finite_points() -> None:
    points = np.zeros((2, 3))
    points[0, 0] = np.inf
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(points, np.array([[0.0, 0.0, 0.0, 1.0]]))


def test_cbf_shield_rejects_bad_delta_safe() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(
            np.zeros((2, 3)), np.array([[0.0, 0.0, 0.0, 1.0]]), delta_safe=-1.0
        )


def test_cbf_shield_rejects_mismatched_roll_angles() -> None:
    with pytest.raises(rcc.CameraChoreographyError):
        rcc.apply_cbf_safety_shield(
            np.zeros((2, 3)), np.array([[0.0, 0.0, 0.0, 1.0]]), roll_angles=np.zeros(3)
        )


def test_cbf_shield_report_serialises() -> None:
    points = np.array([[0.0, 0.0, 0.0]])
    obstacles = np.array([[1.0, 0.0, 0.0, 0.5]])
    report = rcc.apply_cbf_safety_shield(points, obstacles)
    payload = report.to_dict()
    assert payload["kind"] == "atlas.camera_safety_shield"
    assert payload["safe"] == report.safe
