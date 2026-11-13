"""Calibrated camera, physical lens, and sensor-color continuity coverage."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.multicam_epipolar_matching import (
    audit_and_project_epipolar_correspondences,
    compensate_focus_breathing_frame,
    compute_fundamental_matrix_from_rig,
    compute_physical_circle_of_confusion_mm,
    harmonize_multicam_sensor_color,
    sampson_epipolar_distance_squared,
)


def _horizontal_epipolar_matrix() -> np.ndarray:
    return np.array(
        [[0.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]],
        dtype=np.float64,
    )


def test_rig_essential_fundamental_and_sampson_projection() -> None:
    intrinsics = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])
    rig = compute_fundamental_matrix_from_rig(
        intrinsics, intrinsics, np.eye(3), np.array([1.0, 0.0, 0.0])
    )
    assert np.allclose(rig.essential_matrix, _horizontal_epipolar_matrix())
    assert np.allclose(rig.fundamental_matrix, _horizontal_epipolar_matrix() / 800.0)
    assert rig.essential_singular_values[0] == pytest.approx(1.0)
    assert rig.essential_singular_values[1] == pytest.approx(1.0)
    assert rig.essential_singular_values[2] == pytest.approx(0.0, abs=1e-14)

    points_a = np.array([[100.0, 40.0], [200.0, 80.0], [300.0, 120.0]])
    points_b = points_a + np.array([[2.0, 2.5], [-1.0, -3.0], [5.0, 1.5]])
    distances = sampson_epipolar_distance_squared(points_a, points_b, _horizontal_epipolar_matrix())
    assert np.allclose(distances, np.array([2.5**2 / 2, 3.0**2 / 2, 1.5**2 / 2]))
    receipt = audit_and_project_epipolar_correspondences(
        points_a,
        points_b,
        _horizontal_epipolar_matrix(),
        tolerance_px2=1e-10,
    )
    assert np.all(receipt.sampson_distance_squared_after < 1e-10)
    assert np.all(receipt.sampson_distance_squared_after < receipt.sampson_distance_squared_before)
    assert receipt.correction_iterations >= 1
    assert not np.array_equal(receipt.points_a_px, receipt.repaired_points_a_px)


def test_epipolar_refuses_invalid_geometry_and_unconverged_projection() -> None:
    intrinsics = np.eye(3)
    with pytest.raises(ValueError):
        compute_fundamental_matrix_from_rig(intrinsics, intrinsics, np.eye(3), np.zeros(3))
    with pytest.raises(ValueError):
        compute_fundamental_matrix_from_rig(
            intrinsics, intrinsics, np.diag([1.0, 1.0, -1.0]), np.ones(3)
        )
    with pytest.raises(ValueError):
        compute_fundamental_matrix_from_rig(np.zeros((3, 3)), intrinsics, np.eye(3), np.ones(3))
    with pytest.raises(ValueError):
        sampson_epipolar_distance_squared(np.empty((0, 2)), np.empty((0, 2)), np.eye(3))
    with pytest.raises(ValueError):
        sampson_epipolar_distance_squared(np.zeros((1, 2)), np.zeros((1, 2)), np.zeros((3, 3)))
    with pytest.raises(ValueError):
        sampson_epipolar_distance_squared(
            np.zeros((1, 2)), np.zeros((1, 2)), np.diag([1.0, 0.0, 0.0])
        )
    with pytest.raises(ValueError):
        audit_and_project_epipolar_correspondences(
            np.zeros((1, 2)), np.ones((1, 2)), _horizontal_epipolar_matrix(), max_iterations=0
        )


def test_focus_breathing_and_physical_circle_of_confusion() -> None:
    frame = np.full((12, 16, 3), 0.25, dtype=np.float64)
    unchanged = compensate_focus_breathing_frame(
        frame,
        focal_length_mm=50.0,
        reference_focus_distance_mm=2000.0,
        focus_distance_mm=2000.0,
        principal_point_px=(7.5, 5.5),
    )
    assert unchanged.breathing_scale == pytest.approx(1.0)
    assert np.allclose(unchanged.frame, frame)
    moved = compensate_focus_breathing_frame(
        frame,
        focal_length_mm=50.0,
        reference_focus_distance_mm=2000.0,
        focus_distance_mm=1000.0,
        principal_point_px=(7.5, 5.5),
    )
    assert moved.breathing_scale > 0
    assert np.all(np.isfinite(moved.frame))
    assert compute_physical_circle_of_confusion_mm(2000.0, 2000.0, 50.0, 2.8) == 0.0
    coc = compute_physical_circle_of_confusion_mm(1500.0, 3000.0, 50.0, 2.8)
    assert coc > 0.0 and np.isfinite(coc)


def test_focus_breathing_and_coc_refusals() -> None:
    frame = np.ones((4, 4, 3))
    with pytest.raises(ValueError):
        compensate_focus_breathing_frame(
            frame,
            focal_length_mm=-1.0,
            reference_focus_distance_mm=1000.0,
            focus_distance_mm=1000.0,
            principal_point_px=(1.5, 1.5),
        )
    with pytest.raises(ValueError):
        compensate_focus_breathing_frame(
            frame,
            focal_length_mm=50.0,
            reference_focus_distance_mm=1000.0,
            focus_distance_mm=1000.0,
            principal_point_px=(5.0, 1.0),
        )
    with pytest.raises(ValueError):
        compensate_focus_breathing_frame(
            np.full((4, 4, 2), np.nan),
            focal_length_mm=50.0,
            reference_focus_distance_mm=1000.0,
            focus_distance_mm=1000.0,
            principal_point_px=(1.5, 1.5),
        )
    with pytest.raises(ValueError):
        compute_physical_circle_of_confusion_mm(0.0, 1000.0, 50.0, 2.8)
    with pytest.raises(ValueError):
        compute_physical_circle_of_confusion_mm(1000.0, 40.0, 50.0, 2.8)


def test_ridge_sensor_calibration_and_asc_cdl_delta_e_receipt() -> None:
    rng = np.random.default_rng(731)
    source = rng.uniform(0.05, 0.65, size=(48, 3))
    sensor_to_reference = np.array([[0.90, 0.06, 0.02], [0.03, 0.88, 0.04], [0.02, 0.05, 0.86]])
    reference = source @ sensor_to_reference + np.array([0.015, 0.02, 0.01])
    frame = rng.uniform(0.05, 0.65, size=(8, 9, 3))
    receipt = harmonize_multicam_sensor_color(source, reference, frame, ridge=1e-7)
    assert receipt.sensor_matrix_b_to_a.shape == (3, 3)
    assert receipt.harmonized_rgb.shape == frame.shape
    assert receipt.delta_e00_before_max > receipt.delta_e00_after_max
    assert receipt.delta_e00_after_max < 1e-3
    assert receipt.cdl_saturation == 1.0
    assert receipt.cdl_power == (1.0, 1.0, 1.0)


def test_sensor_calibration_refusals() -> None:
    chart = np.full((5, 3), 0.2)
    frame = np.full((4, 4, 3), 0.2)
    with pytest.raises(ValueError):
        harmonize_multicam_sensor_color(chart, chart, frame)
    with pytest.raises(ValueError):
        harmonize_multicam_sensor_color(np.ones((5, 3)), np.ones((5, 3)), frame)
    with pytest.raises(ValueError):
        harmonize_multicam_sensor_color(np.eye(4, 3), np.eye(4, 3), np.full((2, 2, 3), 2.0))
