"""WAVE 20 — tests for the differentiable adjoint inverse-cinema solver.

The analytic Jacobian is checked against a central finite difference; the
box-constrained Levenberg-Marquardt solver is checked for monotone loss
decrease, physical-box clamping, and recovery of a synthetic ground truth.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import adjoint_inverse_cinema_solver as aic


def _truth() -> np.ndarray:
    return np.array([1.3, 0.4, 2.2, 3.5, -0.8, 0.6, 1.7, 2.4, 0.5, -0.3, 2.5, 0.6, 0.35, 0.8, 0.4])


# --- the forward model ------------------------------------------------------------


def test_forward_returns_observation_and_jacobian() -> None:
    y, jac = aic.render_differentiable_scene_observation(aic.DEFAULT_PRIOR)
    assert y.shape == (aic.OBSERVATION_COUNT,)
    assert jac.shape == (aic.OBSERVATION_COUNT, aic.PARAMETER_COUNT)
    assert np.all(np.isfinite(y))
    assert np.all(np.isfinite(jac))


def test_forward_rejects_wrong_shape() -> None:
    with pytest.raises(aic.InverseCinemaError):
        aic.render_differentiable_scene_observation(np.zeros(3))


def test_forward_rejects_non_finite() -> None:
    bad = aic.DEFAULT_PRIOR.copy()
    bad[0] = np.nan
    with pytest.raises(aic.InverseCinemaError):
        aic.render_differentiable_scene_observation(bad)


def test_forward_rejects_out_of_box() -> None:
    bad = aic.DEFAULT_PRIOR.copy()
    bad[0] = 1e9
    with pytest.raises(aic.InverseCinemaError):
        aic.render_differentiable_scene_observation(bad)


def test_fog_transmittance_is_in_unit_interval() -> None:
    y, _jac = aic.render_differentiable_scene_observation(aic.DEFAULT_PRIOR)
    assert 0.0 < y[5] <= 1.0


# --- the analytic Jacobian vs finite differences ----------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_analytic_jacobian_matches_finite_difference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    lower, upper = aic.PARAMETER_BOUNDS
    theta = lower + (upper - lower) * rng.random(aic.PARAMETER_COUNT)
    # Keep away from the box boundary so the central difference stays interior.
    theta = np.clip(theta, lower + 1e-2, upper - 1e-2)
    error = aic.verify_adjoint_jacobian_accuracy(theta)
    assert error < aic.JACOBIAN_TOLERANCE


def test_verify_rejects_bad_step() -> None:
    with pytest.raises(aic.InverseCinemaError):
        aic.verify_adjoint_jacobian_accuracy(aic.DEFAULT_PRIOR, step=0.0)


def test_verify_rejects_out_of_box_theta() -> None:
    bad = aic.DEFAULT_PRIOR.copy()
    bad[8] = 100.0
    with pytest.raises(aic.InverseCinemaError):
        aic.verify_adjoint_jacobian_accuracy(bad)


# --- the inverse solver -----------------------------------------------------------


def test_inverse_recovers_ground_truth_and_reduces_loss() -> None:
    truth = _truth()
    target, _jac = aic.render_differentiable_scene_observation(truth)
    report = aic.solve_inverse_cinema_parameters(
        target, ground_truth=truth, gamma=1e-4, max_iterations=400
    )
    assert report.final_loss <= 1e-4 * report.initial_loss
    assert report.parameter_rmse < 0.5
    assert report.jacobian_error < aic.JACOBIAN_TOLERANCE


def test_inverse_loss_history_is_non_increasing() -> None:
    truth = _truth()
    target, _jac = aic.render_differentiable_scene_observation(truth)
    report = aic.solve_inverse_cinema_parameters(target, gamma=1e-4, max_iterations=200)
    history = np.array(report.loss_history)
    assert np.all(np.diff(history) <= 1e-12)


def test_inverse_result_stays_in_physical_box() -> None:
    truth = _truth()
    target, _jac = aic.render_differentiable_scene_observation(truth)
    report = aic.solve_inverse_cinema_parameters(target, gamma=1e-4, max_iterations=200)
    lower, upper = aic.PARAMETER_BOUNDS
    assert np.all(report.parameters >= lower - 1e-12)
    assert np.all(report.parameters <= upper + 1e-12)


def test_inverse_rejects_wrong_target_shape() -> None:
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(np.zeros(3))


def test_inverse_rejects_non_finite_target() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    target = target.copy()
    target[0] = np.inf
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(target)


def test_inverse_rejects_negative_gamma() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(target, gamma=-1.0)


def test_inverse_rejects_bad_max_iterations() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(target, max_iterations=0)


def test_inverse_rejects_bad_lambda_initial() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(target, lambda_initial=0.0)


def test_inverse_rejects_out_of_box_prior() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    bad_prior = aic.DEFAULT_PRIOR.copy()
    bad_prior[0] = 1e9
    with pytest.raises(aic.InverseCinemaError):
        aic.solve_inverse_cinema_parameters(target, prior=bad_prior)


def test_report_serialises() -> None:
    target, _jac = aic.render_differentiable_scene_observation(_truth())
    report = aic.solve_inverse_cinema_parameters(target, gamma=1e-4, max_iterations=50)
    payload = report.to_dict()
    assert payload["kind"] == "atlas.inverse_cinema_reconstruction"
    assert len(payload["parameters"]) == aic.PARAMETER_COUNT  # type: ignore[arg-type]
    assert payload["initial_loss"] == report.initial_loss
