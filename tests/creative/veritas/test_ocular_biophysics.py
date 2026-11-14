"""Unit tests for Law 2 — binocular vergence, saccades and pupil reflex."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    OcularGeometryError,
    PupilStimulusError,
    compute_watson_yellott_pupil_diameter,
    solve_binocular_vergence,
    synthesize_saccade_vor_trajectory,
)

LEFT = np.array([-31.5, 0.0, 0.0])
RIGHT = np.array([31.5, 0.0, 0.0])
FIXATION = np.array([0.0, 0.0, 500.0])


def test_exact_binocular_vergence() -> None:
    solution = solve_binocular_vergence(LEFT, RIGHT, FIXATION)
    assert solution.violations_before == ()
    assert solution.skew_mm < 0.5
    assert 0.0 < solution.vergence_deg < 90.0
    assert np.isclose(np.linalg.norm(solution.axis_left), 1.0)


def test_divergent_gaze_is_flagged_and_projected() -> None:
    solution = solve_binocular_vergence(
        LEFT,
        RIGHT,
        FIXATION,
        axis_left=np.array([0.0, 0.0, 1.0]),
        axis_right=np.array([0.0, 0.0, -1.0]),
    )
    assert "divergent_gaze" in solution.violations_before
    assert solution.vergence_deg < 90.0


def test_non_intersecting_gaze_is_projected() -> None:
    solution = solve_binocular_vergence(
        LEFT,
        RIGHT,
        FIXATION,
        axis_left=np.array([0.0, 0.0, 1.0]),
        axis_right=np.array([0.0, 0.0, 1.0]),
    )
    assert "non_intersecting_gaze" in solution.violations_before
    assert solution.skew_mm < 0.5


def test_ipd_bounds_and_shapes_raise() -> None:
    with pytest.raises(OcularGeometryError):
        solve_binocular_vergence(np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), FIXATION)
    with pytest.raises(OcularGeometryError):
        solve_binocular_vergence(np.zeros(2), RIGHT, FIXATION)
    with pytest.raises(OcularGeometryError):
        solve_binocular_vergence(np.array([np.nan, 0.0, 0.0]), RIGHT, FIXATION)


def test_saccade_main_sequence_and_vor() -> None:
    trajectory = synthesize_saccade_vor_trajectory(20.0, 0.1, head_angular_velocity_deg_s=50.0)
    assert trajectory.peak_velocity_deg_s == pytest.approx(500.0 * (1.0 - np.exp(-20.0 / 15.0)))
    assert trajectory.time_s.shape == trajectory.eye_angle_deg.shape
    assert trajectory.eye_angle_deg[-1] == pytest.approx(20.0)
    assert trajectory.world_angle_deg[-1] == pytest.approx(20.0 + 50.0 * 0.1)


def test_saccade_validation() -> None:
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(float("nan"), 0.1)
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(10.0, 0.0)
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(10.0, 0.1, sample_rate_hz=0.0)
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(10.0, 0.1, v_max_deg_s=0.0)
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(10.0, 0.1, theta_0_deg=0.0)


def test_pupil_constricts_to_bright_light() -> None:
    response = compute_watson_yellott_pupil_diameter(1000.0, 100.0, 30.0, 1.0)
    assert 2.0 <= response.steady_state_mm <= 8.0
    assert response.diameter_mm[0] == pytest.approx(8.0)
    assert response.diameter_mm[-1] < response.diameter_mm[0]
    assert response.constriction_mm > 0.0


def test_pupil_dilates_in_darkness() -> None:
    response = compute_watson_yellott_pupil_diameter(
        0.01, 100.0, 30.0, 1.0, initial_diameter_mm=2.5
    )
    assert response.diameter_mm[-1] > response.diameter_mm[0]


def test_pupil_validation() -> None:
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(-1.0)
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(10.0, field_area_deg2=0.0)
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(10.0, age_years=200.0)
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(10.0, binocular=2.0)
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(10.0, duration_s=0.0)
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(10.0, initial_diameter_mm=1.0)
