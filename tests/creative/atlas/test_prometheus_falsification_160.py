"""WAVE 26 — the Popperian falsification campaign (P001-P160).

This is a *falsification* campaign, not a happy-path test: 160 independent,
deterministic probes attempt to **refute** the mathematical boundaries and the
fail-closed behaviour of the PROMETHEUS-ATLAS waves 18-25. Every probe names the
exact invariant it tries to break; a probe that stops failing is a regression.

Groups (each probe is a numbered ``test_pNNN_*``):

* P001-P025  port-Hamiltonian world solver (skew ``J``, PSD ``R``, the exact
  discrete chain rule, discrete passivity, the 500-step isolated drift bound).
* P026-P050  entropic 2-Wasserstein OT / Schrödinger bridge / barycentre.
* P051-P075  adjoint differentiable inverse cinema (analytic vs finite-difference
  Jacobian, box constraints, ground-truth recovery).
* P076-P095  Riemannian minimum-snap camera choreographer + the CBF shield.
* P096-P115  Nash dramatic staging (collision, camera visibility, eps-Nash).
* P116-P135  anamorphic/volumetric scattering + relativistic Doppler.
* P136-P160  counterfactual playground, post-disk verifier, Merkle proofs,
  anti-erasure guard, quarantine and fencing.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import adjoint_inverse_cinema_solver as inv
from nexus_ai_agent.creative.atlas import anamorphic_scattering_renderer as scat
from nexus_ai_agent.creative.atlas import nash_dramatic_staging_solver as nash
from nexus_ai_agent.creative.atlas import port_hamiltonian_world_solver as phs
from nexus_ai_agent.creative.atlas import prometheus_studio_playground as psp
from nexus_ai_agent.creative.atlas import prometheus_verifier_and_governance as pvg
from nexus_ai_agent.creative.atlas import riemannian_camera_choreographer as cam
from nexus_ai_agent.creative.atlas import wasserstein_schrodinger_bridge as wass
from nexus_ai_agent.creative.atlas.atlas_spine_governance import (
    AtlasGovernanceRefusalError,
    StaleFencingTokenError,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

# --------------------------------------------------------------------------------
# shared fixtures
# --------------------------------------------------------------------------------


def _phs_harmonic(*, nonlinear: float = 0.0) -> phs.PortHamiltonianSystem:
    return phs.PortHamiltonianSystem(
        J=np.array([[0.0, -1.0], [1.0, 0.0]]),
        R=np.zeros((2, 2)),
        Q=np.eye(2),
        dimension=2,
        nonlinear=nonlinear,
    )


def _phs_damped_driven() -> phs.PortHamiltonianSystem:
    return phs.PortHamiltonianSystem(
        J=np.array([[0.0, -1.0], [1.0, 0.0]]),
        R=0.05 * np.eye(2),
        Q=np.diag([1.0, 2.0]),
        dimension=2,
        G=np.array([[0.0], [1.0]]),
    )


def _simplex(seed: int, n: int = 4) -> np.ndarray:
    rng = np.random.default_rng(seed)
    raw = rng.random(n) + 0.05
    return raw / raw.sum()


def _wgrid(n: int) -> np.ndarray:
    return np.linspace(0.0, 1.0, n)[:, None]


def _wcost(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.asarray((a - b.T) ** 2, dtype=np.float64)


def _cost(n: int, m: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    a = rng.normal(size=(n, 1))
    b = rng.normal(size=(1, m))
    return np.asarray((a - b) ** 2, dtype=np.float64)


def _rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    return np.asarray(
        np.eye(3) + math.sin(angle) * k + (1.0 - math.cos(angle)) * (k @ k), dtype=np.float64
    )


def _keyframe_rotations(k: int) -> np.ndarray:
    return np.stack([_rotation(np.array([0.0, 0.0, 1.0]), 0.3 * i) for i in range(k)])


def _staging_problem(actor_count: int = 3) -> nash.DramaticStagingProblem:
    interactions = (
        ((0, 1, "personal"), (1, 2, "social")) if actor_count >= 3 else ((0, 1, "personal"),)
    )
    return nash.DramaticStagingProblem(
        actor_count=actor_count,
        camera_position=np.array([0.0, -6.0]),
        camera_yaw=0.0,
        key_light_azimuth=0.0,
        interactions=interactions,
        speakers=(0, 1),
    )


def _world_parameters(**overrides: object) -> psp.WorldParameters:
    base: dict[str, object] = {
        "key_light_azimuth_deg": 40.0,
        "key_light_intensity": 1.2,
        "fog_anisotropy_g": 0.3,
        "extinction_coefficient": 0.2,
        "anamorphic_squeeze": 1.33,
        "staging_proxemics_m": 1.2,
        "room_absorption": 0.4,
        "camera_keyframes": psp._default_camera_keyframes(),
        "port_state": psp._default_port_state(),
        "obstacle": None,
    }
    base.update(overrides)
    return psp.WorldParameters(**base)  # type: ignore[arg-type]


# ================================================================================
# P001-P025 — port-Hamiltonian world solver
# ================================================================================


def test_p001_non_skew_jacobian_is_rejected() -> None:
    bad = np.array([[0.0, 1.0], [1.0, 0.0]])  # symmetric, not skew
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(J=bad, R=np.zeros((2, 2)), Q=np.eye(2), dimension=2).jacobian(
            np.zeros(2)
        )


def test_p002_negative_eigenvalue_dissipation_is_rejected() -> None:
    bad = np.array([[1.0, 0.0], [0.0, -0.5]])
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.array([[0.0, -1.0], [1.0, 0.0]]), R=bad, Q=np.eye(2), dimension=2
        ).dissipation(np.zeros(2))


def test_p003_asymmetric_dissipation_is_rejected() -> None:
    bad = np.array([[1.0, 0.4], [0.0, 1.0]])
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.array([[0.0, -1.0], [1.0, 0.0]]), R=bad, Q=np.eye(2), dimension=2
        ).dissipation(np.zeros(2))


def test_p004_non_finite_state_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.discrete_gradient(_phs_harmonic(), np.array([np.inf, 0.0]), np.zeros(2))


def test_p005_nan_state_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.discrete_gradient(_phs_harmonic(), np.array([np.nan, 0.0]), np.zeros(2))


@pytest.mark.parametrize("dt", [0.0, -0.01])
def test_p006_non_positive_dt_is_rejected(dt: float) -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(2), dt=dt)


def test_p007_non_finite_dt_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(2), dt=float("inf"))


def test_p008_non_positive_tolerance_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(2), dt=0.01, tolerance=0.0)


def test_p009_zero_max_iterations_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(2), dt=0.01, max_iterations=0)


def test_p010_input_port_requires_u() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_damped_driven(), np.zeros(2), dt=0.01)


def test_p011_u_without_input_port_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(2), dt=0.01, u=np.array([0.1]))


def test_p012_wrong_u_dimension_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(
            _phs_damped_driven(), np.zeros(2), dt=0.01, u=np.array([0.1, 0.2])
        )


def test_p013_wrong_state_dimension_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(_phs_harmonic(), np.zeros(3), dt=0.01)


def test_p014_non_symmetric_q_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.array([[1.0, 0.3], [0.0, 1.0]]),
            dimension=2,
        )


def test_p015_non_positive_definite_q_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.diag([1.0, 0.0]), dimension=2
        )


def test_p016_negative_nonlinear_coefficient_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.eye(2), dimension=2, nonlinear=-0.5
        )


def test_p017_out_of_range_dimension_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.eye(2), dimension=phs.MAX_DIM + 1
        )


def test_p018_skew_residual_is_machine_precision() -> None:
    system = _phs_damped_driven()
    _x, receipt = phs.solve_port_hamiltonian_step(
        system, np.array([1.0, 0.0]), dt=0.01, u=np.array([0.3])
    )
    assert receipt.skew_residual <= phs.SKEW_TOLERANCE


def test_p019_dissipation_eigenvalue_min_is_psd() -> None:
    system = _phs_damped_driven()
    _x, receipt = phs.solve_port_hamiltonian_step(
        system, np.array([1.0, 0.0]), dt=0.01, u=np.array([0.3])
    )
    assert receipt.dissipation_eigenvalue_min >= -phs.PSD_TOLERANCE


@pytest.mark.parametrize("seed", range(5))
def test_p020_discrete_chain_rule_is_exact(seed: int) -> None:
    rng = np.random.default_rng(seed)
    system = _phs_harmonic(nonlinear=0.4)
    a = rng.normal(size=2)
    b = a + 0.05 * rng.normal(size=2)
    g = phs.discrete_gradient(system, a, b)
    assert abs(float(g @ (b - a)) - (system.hamiltonian(b) - system.hamiltonian(a))) < 1e-13


def test_p021_step_energy_balance_is_machine_precision() -> None:
    system = _phs_damped_driven()
    _x, receipt = phs.solve_port_hamiltonian_step(
        system, np.array([1.0, 0.0]), dt=0.01, u=np.array([0.3])
    )
    assert receipt.energy_balance_residual <= phs.ENERGY_TOLERANCE
    assert receipt.passive is True


def test_p022_isolated_world_500_step_drift_is_bounded() -> None:
    report = phs.integrate_port_hamiltonian(
        _phs_harmonic(), np.array([1.0, 0.0]), dt=0.01, steps=500
    )
    assert report.isolated_world_drift_bound <= phs.ENERGY_TOLERANCE
    assert report.all_passive is True


def test_p023_nonlinear_isolated_world_conserves_energy() -> None:
    report = phs.integrate_port_hamiltonian(
        _phs_harmonic(nonlinear=0.5), np.array([1.0, 0.0]), dt=0.01, steps=500
    )
    assert report.isolated_world_drift_bound <= phs.ENERGY_TOLERANCE


def test_p024_too_many_steps_is_rejected() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.integrate_port_hamiltonian(
            _phs_harmonic(), np.array([1.0, 0.0]), dt=0.01, steps=phs.MAX_STEPS + 1
        )


def test_p025_non_finite_jacobian_is_rejected() -> None:
    system = phs.PortHamiltonianSystem(
        J=np.array([[0.0, np.inf], [-np.inf, 0.0]]), R=np.zeros((2, 2)), Q=np.eye(2), dimension=2
    )
    with pytest.raises(phs.PortHamiltonianError):
        system.jacobian(np.zeros(2))


# ================================================================================
# P026-P050 — entropic Wasserstein / Schrödinger bridge / barycentre
# ================================================================================


def test_p026_zero_probability_mass_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5, -1e-9]), np.array([0.5, 0.5]), np.ones((3, 2))
        )


def test_p027_negative_mass_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(np.array([-0.2, 1.2]), np.array([0.5, 0.5]), _cost(2, 2))


def test_p028_non_normalised_simplex_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(np.array([0.2, 0.3]), np.array([0.5, 0.5]), _cost(2, 2))


def test_p029_non_finite_distribution_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(
            np.array([np.nan, 1.0]), np.array([0.5, 0.5]), _cost(2, 2)
        )


def test_p030_mismatched_cost_shape_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.zeros((2, 3))
        )


def test_p031_negative_cost_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.array([[0.0, -1.0], [1.0, 0.0]])
        )


def test_p032_out_of_range_eps_is_rejected() -> None:
    with pytest.raises(wass.TransportError):
        wass.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), _cost(2, 2), eps=100.0
        )


def test_p033_tiny_eps_log_domain_is_nan_free() -> None:
    result = wass.solve_entropic_wasserstein_ot(
        _simplex(1), _simplex(2), _cost(4, 4), eps=wass.MIN_EPS
    )
    assert np.all(np.isfinite(result.plan))
    assert result.plan.sum() == pytest.approx(1.0, abs=1e-8)


@pytest.mark.parametrize("eps", [0.001, 0.01, 0.5, 1.0, 10.0])
def test_p034_log_domain_is_stable_across_eps(eps: float) -> None:
    result = wass.solve_entropic_wasserstein_ot(_simplex(3), _simplex(4), _cost(4, 4), eps=eps)
    assert np.all(np.isfinite(result.plan))
    assert result.marginal_residual_source < 1e-8


def test_p035_plan_conserves_both_marginals() -> None:
    mu, nu = _simplex(5), _simplex(6)
    result = wass.solve_entropic_wasserstein_ot(mu, nu, _cost(4, 4))
    assert np.abs(result.plan.sum(axis=1) - mu).max() < wass.MARGINAL_TOLERANCE
    assert np.abs(result.plan.sum(axis=0) - nu).max() < wass.MARGINAL_TOLERANCE


def test_p036_plan_mass_is_one() -> None:
    result = wass.solve_entropic_wasserstein_ot(_simplex(7), _simplex(8), _cost(4, 4))
    assert result.mass == pytest.approx(1.0, abs=1e-9)


def test_p037_identity_divergence_vanishes() -> None:
    mu = _simplex(9, 6)
    self_cost = _wcost(_wgrid(6), _wgrid(6))
    result = wass.solve_entropic_wasserstein_ot(mu, mu, self_cost, eps=0.05)
    assert abs(result.sinkhorn_divergence) < wass.IDENTITY_TOLERANCE


def test_p038_divergence_is_symmetric() -> None:
    mu, nu = _simplex(10), _simplex(11)
    cost = _cost(4, 4)
    forward = wass.solve_entropic_wasserstein_ot(mu, nu, cost).sinkhorn_divergence
    backward = wass.solve_entropic_wasserstein_ot(nu, mu, cost.T).sinkhorn_divergence
    assert abs(forward - backward) < 1e-10


def test_p039_divergence_is_non_negative() -> None:
    forward = wass.solve_entropic_wasserstein_ot(_simplex(12), _simplex(13), _cost(4, 4))
    assert forward.sinkhorn_divergence >= -1e-10


def test_p040_bridge_rejects_time_out_of_range() -> None:
    with pytest.raises(wass.TransportError):
        wass.interpolate_schrodinger_bridge_scene(
            np.zeros((2, 2)), np.ones((2, 2)), np.array([0.5, 0.5]), np.array([0.5, 0.5]), t=1.5
        )


def test_p041_bridge_rejects_non_finite_points() -> None:
    with pytest.raises(wass.TransportError):
        wass.interpolate_schrodinger_bridge_scene(
            np.array([[np.nan, 0.0], [0.0, 0.0]]),
            np.ones((2, 2)),
            np.array([0.5, 0.5]),
            np.array([0.5, 0.5]),
            t=0.5,
        )


def test_p042_bridge_rejects_point_count_mismatch() -> None:
    with pytest.raises(wass.TransportError):
        wass.interpolate_schrodinger_bridge_scene(
            np.zeros((3, 2)), np.ones((2, 2)), np.array([0.5, 0.5]), np.array([0.5, 0.5]), t=0.5
        )


def test_p043_bridge_endpoint_at_zero_is_source() -> None:
    src = np.array([[0.0, 0.0], [1.0, 1.0]])
    result = wass.interpolate_schrodinger_bridge_scene(
        src, np.array([[2.0, 2.0], [3.0, 3.0]]), np.array([0.5, 0.5]), np.array([0.5, 0.5]), t=0.0
    )
    assert np.allclose(result.positions, src)


@pytest.mark.parametrize("t", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_p044_bridge_conserves_mass_at_every_time(t: float) -> None:
    result = wass.interpolate_schrodinger_bridge_scene(
        np.array([[0.0, 0.0], [1.0, 1.0]]),
        np.array([[2.0, 2.0], [3.0, 3.0]]),
        np.array([0.5, 0.5]),
        np.array([0.5, 0.5]),
        t=t,
    )
    assert result.masses.sum() == pytest.approx(1.0, abs=1e-12)


def test_p045_bridge_brownian_variance_formula() -> None:
    eps = 0.2
    result = wass.interpolate_schrodinger_bridge_scene(
        np.zeros((2, 2)),
        np.ones((2, 2)),
        np.array([0.5, 0.5]),
        np.array([0.5, 0.5]),
        t=0.5,
        eps=eps,
    )
    assert result.brownian_variance == pytest.approx(eps * 0.5 * 0.5, abs=1e-12)


def test_p046_barycenter_rejects_non_normalised_weights() -> None:
    dists = (_simplex(20), _simplex(21))
    with pytest.raises(wass.TransportError):
        wass.compute_wasserstein_barycenter(dists, np.array([0.3, 0.3]), _cost(4, 4))


def test_p047_barycenter_rejects_negative_weights() -> None:
    dists = (_simplex(22), _simplex(23))
    with pytest.raises(wass.TransportError):
        wass.compute_wasserstein_barycenter(dists, np.array([-0.5, 1.5]), _cost(4, 4))


def test_p048_barycenter_rejects_mismatched_support() -> None:
    with pytest.raises(wass.TransportError):
        wass.compute_wasserstein_barycenter(
            (_simplex(24, 4), _simplex(25, 3)), np.array([0.5, 0.5]), _cost(4, 4)
        )


def test_p049_barycenter_of_identical_references_is_that_reference() -> None:
    mu = _simplex(26, 5)
    bary = wass.compute_wasserstein_barycenter(
        (mu, mu, mu), np.array([0.2, 0.3, 0.5]), _wcost(_wgrid(5), _wgrid(5)), eps=1e-3
    )
    assert np.allclose(bary.support, mu, atol=1e-3)


def test_p050_metric_audit_triangle_inequality_holds() -> None:
    mu, nu, third = _simplex(27), _simplex(28), _simplex(29)
    cost = _cost(4, 4)
    audit = wass.audit_wasserstein_metric(mu, nu, cost, third=third, third_cost=(cost, cost))
    assert audit.marginal_conserved is True
    assert audit.identity_holds is True
    assert audit.triangle_residual <= 1e-9


# ================================================================================
# P051-P075 — adjoint differentiable inverse cinema
# ================================================================================


def _valid_theta() -> np.ndarray:
    return inv.DEFAULT_PRIOR.copy()


def test_p051_forward_returns_observation_and_jacobian() -> None:
    y, jac = inv.render_differentiable_scene_observation(_valid_theta())
    assert y.shape == (inv.OBSERVATION_COUNT,)
    assert jac.shape == (inv.OBSERVATION_COUNT, inv.PARAMETER_COUNT)


def test_p052_forward_rejects_wrong_shape() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.render_differentiable_scene_observation(np.zeros(inv.PARAMETER_COUNT - 1))


def test_p053_forward_rejects_non_finite() -> None:
    theta = _valid_theta()
    theta[0] = np.nan
    with pytest.raises(inv.InverseCinemaError):
        inv.render_differentiable_scene_observation(theta)


def test_p054_forward_rejects_out_of_box() -> None:
    theta = _valid_theta()
    lower, _upper = inv.PARAMETER_BOUNDS
    theta = theta.copy()
    theta[0] = float(lower[0]) - 1.0
    with pytest.raises(inv.InverseCinemaError):
        inv.render_differentiable_scene_observation(theta)


@pytest.mark.parametrize("seed", range(6))
def test_p055_analytic_jacobian_matches_finite_difference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    lower, upper = inv.PARAMETER_BOUNDS
    theta = lower + (upper - lower) * rng.random(inv.PARAMETER_COUNT)
    assert inv.verify_adjoint_jacobian_accuracy(theta) < inv.JACOBIAN_TOLERANCE


def test_p056_verify_jacobian_rejects_bad_step() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.verify_adjoint_jacobian_accuracy(_valid_theta(), step=0.0)


def test_p057_verify_jacobian_rejects_out_of_box_theta() -> None:
    lower, _upper = inv.PARAMETER_BOUNDS
    theta = _valid_theta().copy()
    theta[1] = float(lower[1]) - 2.0
    with pytest.raises(inv.InverseCinemaError):
        inv.verify_adjoint_jacobian_accuracy(theta)


def test_p058_inverse_rejects_wrong_target_shape() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.solve_inverse_cinema_parameters(np.zeros(inv.OBSERVATION_COUNT - 1))


def test_p059_inverse_rejects_non_finite_target() -> None:
    target = np.zeros(inv.OBSERVATION_COUNT)
    target[0] = np.inf
    with pytest.raises(inv.InverseCinemaError):
        inv.solve_inverse_cinema_parameters(target)


def test_p060_inverse_rejects_negative_gamma() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.solve_inverse_cinema_parameters(np.zeros(inv.OBSERVATION_COUNT), gamma=-0.1)


def test_p061_inverse_rejects_bad_lambda_initial() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.solve_inverse_cinema_parameters(np.zeros(inv.OBSERVATION_COUNT), lambda_initial=0.0)


def test_p062_inverse_rejects_bad_max_iterations() -> None:
    with pytest.raises(inv.InverseCinemaError):
        inv.solve_inverse_cinema_parameters(np.zeros(inv.OBSERVATION_COUNT), max_iterations=0)


def test_p063_inverse_recovers_ground_truth_and_reduces_loss() -> None:
    truth = np.array([1.3, 0.4, 2.2, 3.5, -0.8, 0.6, 1.7, 2.4, 0.5, -0.3, 2.5, 0.6, 0.35, 0.8, 0.4])
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(
        target, ground_truth=truth, gamma=1e-4, max_iterations=400
    )
    assert report.final_loss <= 1e-4 * report.initial_loss
    assert report.parameter_rmse < 0.5


def test_p064_inverse_result_stays_in_physical_box() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.03 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(target)
    assert np.all(report.parameters >= lower - 1e-9)
    assert np.all(report.parameters <= upper + 1e-9)


def test_p065_inverse_loss_history_is_non_increasing() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.04 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(target)
    history = report.loss_history
    assert len(history) >= 2
    assert history[-1] <= history[0] + 1e-12


def test_p066_fog_transmittance_is_in_unit_interval() -> None:
    y, _jac = inv.render_differentiable_scene_observation(_valid_theta())
    assert 0.0 < y[5] <= 1.0


def test_p067_render_is_deterministic() -> None:
    y1, j1 = inv.render_differentiable_scene_observation(_valid_theta())
    y2, j2 = inv.render_differentiable_scene_observation(_valid_theta())
    assert np.array_equal(y1, y2)
    assert np.array_equal(j1, j2)


def test_p068_report_serialises() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.04 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(target, ground_truth=truth)
    payload = report.to_dict()
    assert payload["kind"] == "atlas.inverse_cinema_reconstruction"


def test_p069_jacobian_is_finite() -> None:
    _y, jac = inv.render_differentiable_scene_observation(_valid_theta())
    assert np.all(np.isfinite(jac))


def test_p070_jacobian_row_for_extinction_is_negative() -> None:
    _y, jac = inv.render_differentiable_scene_observation(_valid_theta())
    assert jac[5, 13] < 0.0


def test_p071_jacobian_direction_rows_are_finite() -> None:
    _y, jac = inv.render_differentiable_scene_observation(_valid_theta())
    assert np.all(np.isfinite(jac[11:14, 0:3]))


def test_p072_solve_is_deterministic() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.04 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    a = inv.solve_inverse_cinema_parameters(target)
    b = inv.solve_inverse_cinema_parameters(target)
    assert np.array_equal(a.parameters, b.parameters)


def test_p073_zero_gamma_is_allowed() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.04 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(target, gamma=0.0)
    assert np.all(np.isfinite(report.parameters))


def test_p074_loss_history_is_a_tuple_of_floats() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    truth = np.clip(inv.DEFAULT_PRIOR + 0.04 * (upper - lower), lower, upper)
    target, _jac = inv.render_differentiable_scene_observation(truth)
    report = inv.solve_inverse_cinema_parameters(target)
    assert all(isinstance(value, float) for value in report.loss_history)


def test_p075_jacobian_matches_fd_interior() -> None:
    lower, upper = inv.PARAMETER_BOUNDS
    theta = np.clip(inv.DEFAULT_PRIOR, lower + 1e-2, upper - 1e-2)
    assert inv.verify_adjoint_jacobian_accuracy(theta) < inv.JACOBIAN_TOLERANCE


# ================================================================================
# P076-P095 — Riemannian minimum-snap camera choreographer + CBF shield
# ================================================================================


def _keyframes(k: int = 4) -> tuple[np.ndarray, np.ndarray]:
    times = np.arange(k, dtype=np.float64)
    positions = np.stack([np.array([float(i), 0.5 * i, 0.1 * i]) for i in range(k)])
    return positions, times


def test_p076_minimum_snap_interpolates_keyframes() -> None:
    positions, times = _keyframes()
    traj = cam.plan_minimum_snap_trajectory(positions, times)
    for index, time in enumerate(times):
        assert np.allclose(traj.sample(float(time)), positions[index], atol=1e-6)


def test_p077_minimum_snap_rejects_non_monotone_times() -> None:
    positions, times = _keyframes()
    times = times.copy()
    times[1] = times[0]
    with pytest.raises(cam.CameraChoreographyError):
        cam.plan_minimum_snap_trajectory(positions, times)


def test_p078_minimum_snap_rejects_wrong_position_shape() -> None:
    positions, times = _keyframes()
    with pytest.raises(cam.CameraChoreographyError):
        cam.plan_minimum_snap_trajectory(positions[:, :2], times)


def test_p079_minimum_snap_rejects_too_few_keyframes() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.plan_minimum_snap_trajectory(np.zeros((1, 3)), np.array([0.0]))


def test_p080_minimum_snap_rejects_non_finite_positions() -> None:
    positions, times = _keyframes()
    positions = positions.copy()
    positions[0, 0] = np.nan
    with pytest.raises(cam.CameraChoreographyError):
        cam.plan_minimum_snap_trajectory(positions, times)


def test_p081_minimum_snap_sample_outside_span_raises() -> None:
    positions, times = _keyframes()
    traj = cam.plan_minimum_snap_trajectory(positions, times)
    with pytest.raises(cam.CameraChoreographyError):
        traj.sample(float(times[-1]) + 10.0)


def test_p082_minimum_snap_is_optimal_under_perturbation() -> None:
    positions, times = _keyframes()
    traj = cam.plan_minimum_snap_trajectory(positions, times)
    perturbed = positions.copy()
    perturbed[1] += np.array([0.0, 0.2, 0.0])
    other = cam.plan_minimum_snap_trajectory(perturbed, times)
    assert traj.snap_cost() <= other.snap_cost() + 1e-9


def test_p083_orientation_path_stays_on_so3() -> None:
    rotations = _keyframe_rotations(4)
    times = np.arange(4, dtype=np.float64)
    traj = cam.interpolate_orientation_bspline(rotations, times)
    for rotation in traj.rotations:
        assert np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro") < cam.SO3_TOLERANCE
        assert abs(float(np.linalg.det(rotation)) - 1.0) < cam.SO3_TOLERANCE
    assert traj.is_on_so3 is True


def test_p084_orientation_path_hits_endpoints() -> None:
    rotations = _keyframe_rotations(3)
    times = np.arange(3, dtype=np.float64)
    traj = cam.interpolate_orientation_bspline(rotations, times)
    assert np.allclose(traj.rotations[0], rotations[0], atol=1e-6)
    assert np.allclose(traj.rotations[-1], rotations[-1], atol=1e-6)


def test_p085_orientation_rejects_non_orthonormal_keyframe() -> None:
    rotations = _keyframe_rotations(3)
    rotations = rotations.copy()
    rotations[1] = np.full((3, 3), 0.3)
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(rotations, np.arange(3, dtype=np.float64))


def test_p086_orientation_rejects_wrong_shape() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(np.zeros((3, 3)), np.arange(3, dtype=np.float64))


def test_p087_orientation_rejects_bad_samples_per_segment() -> None:
    rotations = _keyframe_rotations(3)
    with pytest.raises(cam.CameraChoreographyError):
        cam.interpolate_orientation_bspline(
            rotations, np.arange(3, dtype=np.float64), samples_per_segment=0
        )


def test_p088_cbf_shield_enforces_keep_out() -> None:
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]])
    obstacles = np.array([[0.1, 0.0, 0.0, 0.5]])
    report = cam.apply_cbf_safety_shield(positions, obstacles)
    assert report.safe is True
    assert report.min_barrier_value >= 0.0


def test_p089_cbf_shield_leaves_clear_path_untouched() -> None:
    positions = np.array([[5.0, 5.0, 0.0], [6.0, 6.0, 0.0]])
    obstacles = np.array([[0.0, 0.0, 0.0, 0.5]])
    report = cam.apply_cbf_safety_shield(positions, obstacles)
    assert np.allclose(report.positions, positions)
    assert report.active_constraints == ()


def test_p090_cbf_shield_rejects_wrong_position_shape() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(np.zeros((2, 2)), np.array([[0.0, 0.0, 0.0, 0.5]]))


def test_p091_cbf_shield_rejects_empty_obstacles() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(np.zeros((2, 3)), np.zeros((0, 4)))


def test_p092_cbf_shield_rejects_non_positive_radius() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(np.zeros((2, 3)), np.array([[0.0, 0.0, 0.0, 0.0]]))


def test_p093_cbf_shield_rejects_bad_delta_safe() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(
            np.zeros((2, 3)), np.array([[0.0, 0.0, 0.0, 0.5]]), delta_safe=-1.0
        )


def test_p094_cbf_shield_counts_roll_violations() -> None:
    positions = np.zeros((3, 3))
    obstacles = np.array([[9.0, 9.0, 9.0, 0.5]])
    rolls = np.array([0.0, 1.0, 0.1])
    report = cam.apply_cbf_safety_shield(positions, obstacles, roll_angles=rolls)
    assert report.roll_violations == 1


def test_p095_cbf_shield_rejects_mismatched_roll_angles() -> None:
    with pytest.raises(cam.CameraChoreographyError):
        cam.apply_cbf_safety_shield(
            np.zeros((3, 3)), np.array([[0.0, 0.0, 0.0, 0.5]]), roll_angles=np.zeros(2)
        )


# ================================================================================
# P096-P115 — Nash dramatic staging
# ================================================================================


def _staging_initial(actor_count: int) -> np.ndarray:
    base = np.stack([np.array([float(i) * 1.5 - 1.5, 0.0, 0.0]) for i in range(actor_count)])
    return base


def test_p096_problem_rejects_too_few_actors() -> None:
    with pytest.raises(nash.StagingError):
        nash.DramaticStagingProblem(
            actor_count=1,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(0,),
        )


def test_p097_problem_rejects_unknown_preset() -> None:
    with pytest.raises(nash.StagingError):
        nash.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 1, "nonsense"),),
            speakers=(0,),
        )


def test_p098_problem_rejects_self_interaction() -> None:
    with pytest.raises(nash.StagingError):
        nash.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 0, "personal"),),
            speakers=(0,),
        )


def test_p099_problem_rejects_speaker_out_of_range() -> None:
    with pytest.raises(nash.StagingError):
        nash.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 1, "personal"),),
            speakers=(5,),
        )


def test_p100_problem_rejects_bad_collision_distance() -> None:
    with pytest.raises(nash.StagingError):
        nash.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 1, "personal"),),
            speakers=(0,),
            d_collision=0.0,
        )


def test_p101_solver_rejects_wrong_pose_shape() -> None:
    problem = _staging_problem()
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(problem, np.zeros((2, 3)))


def test_p102_solver_rejects_non_finite_poses() -> None:
    problem = _staging_problem()
    poses = _staging_initial(problem.actor_count)
    poses[0, 0] = np.nan
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(problem, poses)


def test_p103_solver_rejects_negative_epsilon() -> None:
    problem = _staging_problem()
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(
            problem, _staging_initial(problem.actor_count), epsilon=-0.1
        )


def test_p104_solver_rejects_bad_response_samples() -> None:
    problem = _staging_problem()
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(
            problem, _staging_initial(problem.actor_count), response_samples=2
        )


def test_p105_solver_rejects_bad_learning_rate() -> None:
    problem = _staging_problem()
    with pytest.raises(nash.StagingError):
        nash.solve_nash_dramatic_staging(
            problem, _staging_initial(problem.actor_count), learning_rate=0.0
        )


def test_p106_solver_produces_collision_free_staging() -> None:
    problem = _staging_problem()
    report = nash.solve_nash_dramatic_staging(problem, _staging_initial(problem.actor_count))
    assert report.collisions == 0
    assert report.min_pairwise_distance >= problem.d_collision


def test_p107_solver_keeps_speakers_visible() -> None:
    problem = _staging_problem()
    report = nash.solve_nash_dramatic_staging(problem, _staging_initial(problem.actor_count))
    assert report.camera_visibility_fraction == pytest.approx(1.0)


def test_p108_solver_certifies_nash() -> None:
    problem = _staging_problem()
    report = nash.solve_nash_dramatic_staging(
        problem, _staging_initial(problem.actor_count), epsilon=0.5
    )
    assert report.is_nash is True
    assert report.nash_gain <= 0.5


def test_p109_solver_resolves_initial_collision() -> None:
    problem = _staging_problem()
    poses = _staging_initial(problem.actor_count)
    poses[1, :2] = poses[0, :2] + 0.01
    report = nash.solve_nash_dramatic_staging(problem, poses)
    assert report.collisions == 0


def test_p110_proxemic_preset_helper_returns_known_distances() -> None:
    assert nash._proxemic_preset("intimate") < nash._proxemic_preset("social")


def test_p111_wrap_angle_is_bounded() -> None:
    for angle in np.linspace(-20.0, 20.0, 41):
        wrapped = nash._wrap_angle(float(angle))
        assert -math.pi - 1e-9 <= wrapped <= math.pi + 1e-9


def test_p112_report_serialises() -> None:
    problem = _staging_problem()
    report = nash.solve_nash_dramatic_staging(problem, _staging_initial(problem.actor_count))
    payload = report.to_dict()
    assert payload["kind"] == "atlas.nash_dramatic_staging"


def test_p113_visibility_penalty_is_non_negative() -> None:
    problem = _staging_problem()
    poses = _staging_initial(problem.actor_count)
    assert nash._visibility_penalty(poses, problem) >= 0.0


def test_p114_proxemic_energy_diverges_on_collision() -> None:
    problem = _staging_problem()
    poses = _staging_initial(problem.actor_count)
    poses[1, :2] = poses[0, :2] + 0.001
    assert nash._proxemic_energy(poses, problem) < 0.0


def test_p115_solver_improves_potential() -> None:
    problem = _staging_problem()
    poses = np.array([[-1.0, 0.0, 0.0], [0.5, 0.3, 0.2], [2.0, -0.5, -0.3]])
    report = nash.solve_nash_dramatic_staging(
        problem, poses, epsilon=1e-2, max_iterations=80, response_samples=3
    )
    assert report.potential >= nash._potential(poses, problem) - 1e-9


# ================================================================================
# P116-P135 — anamorphic / volumetric scattering + relativistic Doppler
# ================================================================================


@pytest.mark.parametrize("g", [-0.9, 0.0, 0.5, 0.9])
def test_p116_henyey_greenstein_normalises_to_one(g: float) -> None:
    field = scat.compute_volumetric_mie_rayleigh_field(g=g, extinction_coefficient=0.1, depth=1.0)
    assert field.phase_normalisation == pytest.approx(1.0, abs=1e-10)


@pytest.mark.parametrize("g", [1.0, -1.0, 1.5])
def test_p117_henyey_greenstein_rejects_out_of_range_g(g: float) -> None:
    with pytest.raises(scat.ScatteringError):
        scat.henyey_greenstein(np.array([0.0]), g)


def test_p118_henyey_greenstein_rejects_bad_cosine() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.henyey_greenstein(np.array([1.5]), 0.3)


def test_p119_rayleigh_ratios_follow_lambda_minus_four() -> None:
    field = scat.compute_volumetric_mie_rayleigh_field(g=0.2, extinction_coefficient=0.1, depth=1.0)
    ratios = field.rayleigh_ratios
    assert ratios["B"] > ratios["G"] > ratios["R"]
    expected = (scat.RGB_WAVELENGTHS_NM["B"] / scat.RGB_WAVELENGTHS_NM["R"]) ** -4
    assert ratios["B"] / ratios["R"] == pytest.approx(expected, rel=1e-9)


def test_p120_transmittance_is_beer_lambert() -> None:
    field = scat.compute_volumetric_mie_rayleigh_field(g=0.0, extinction_coefficient=0.3, depth=2.0)
    assert field.transmittance == pytest.approx(math.exp(-0.6), abs=1e-12)
    assert 0.0 < field.transmittance <= 1.0


def test_p121_field_rejects_bad_quadrature_nodes() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_volumetric_mie_rayleigh_field(
            g=0.0, extinction_coefficient=0.1, depth=1.0, quadrature_nodes=4
        )


def test_p122_field_rejects_negative_extinction() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_volumetric_mie_rayleigh_field(g=0.0, extinction_coefficient=-0.1, depth=1.0)


def test_p123_field_rejects_negative_depth() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_volumetric_mie_rayleigh_field(g=0.0, extinction_coefficient=0.1, depth=-1.0)


def test_p124_circle_of_confusion_is_zero_at_focus() -> None:
    assert scat.circle_of_confusion_radius(4.0, 4.0, 2.0, 0.05) == pytest.approx(0.0)


def test_p125_circle_of_confusion_grows_with_defocus() -> None:
    near = scat.circle_of_confusion_radius(4.1, 4.0, 2.0, 0.05)
    far = scat.circle_of_confusion_radius(8.0, 4.0, 2.0, 0.05)
    assert far > near > 0.0


def test_p126_circle_of_confusion_rejects_bad_f_number() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.circle_of_confusion_radius(4.0, 4.0, 0.0, 0.05)


def test_p127_bokeh_kernel_is_energy_normalised() -> None:
    kernel = scat.render_anamorphic_cinema_frame(coc_radius=3.0, squeeze=1.0)
    assert float(kernel.kernel.sum()) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("squeeze", [1.0, 1.33, 1.8, 2.0])
def test_p128_bokeh_accepts_standard_squeezes(squeeze: float) -> None:
    kernel = scat.render_anamorphic_cinema_frame(coc_radius=3.0, squeeze=squeeze)
    assert float(kernel.kernel.sum()) == pytest.approx(1.0, abs=1e-9)
    assert kernel.semi_axis_x == pytest.approx(3.0 / squeeze)


def test_p129_bokeh_rejects_unknown_squeeze() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.render_anamorphic_cinema_frame(coc_radius=3.0, squeeze=1.5)


def test_p130_bokeh_rejects_non_positive_coc() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.render_anamorphic_cinema_frame(coc_radius=0.0, squeeze=1.0)


def test_p131_bokeh_rejects_even_side() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.render_anamorphic_cinema_frame(coc_radius=3.0, squeeze=1.0, side=32)


def test_p132_acoustic_doppler_ratio() -> None:
    result = scat.compute_doppler_shifted_signal(source_frequency=440.0, source_velocity=-10.0)
    expected = (scat.SPEED_OF_SOUND + 0.0) / (scat.SPEED_OF_SOUND - (-10.0))
    assert result.ratio == pytest.approx(expected)


def test_p133_acoustic_doppler_refuses_sonic_boom() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=scat.SPEED_OF_SOUND
        )


def test_p134_optical_doppler_redshift_is_less_than_one() -> None:
    result = scat.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=-0.3 * scat.SPEED_OF_LIGHT, mode="optical"
    )
    assert result.ratio < 1.0


def test_p135_optical_doppler_refuses_luminal_speed() -> None:
    with pytest.raises(scat.ScatteringError):
        scat.compute_doppler_shifted_signal(
            source_frequency=5e14, source_velocity=scat.SPEED_OF_LIGHT, mode="optical"
        )


# ================================================================================
# P136-P160 — counterfactual playground, verifier, Merkle, governance
# ================================================================================


def _two_universes() -> tuple[psp.PrometheusStudioPlayground, str, list[str]]:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_world_parameters())
    branch, _receipt = playground.fork_counterfactual_universe(
        root.universe_id,
        psp.CounterfactualIntervention(fork_time=1.0, anamorphic_squeeze=2.0),
        side=24,
    )
    return playground, root.universe_id, [branch.universe_id]


def test_p136_fork_of_unknown_parent_is_rejected() -> None:
    playground = psp.PrometheusStudioPlayground()
    with pytest.raises(psp.PlaygroundError):
        playground.fork_counterfactual_universe(
            "universe-does-not-exist", psp.CounterfactualIntervention(fork_time=0.0)
        )


def test_p137_universe_digest_is_content_addressed() -> None:
    playground = psp.PrometheusStudioPlayground()
    root = playground.create_root_universe(_world_parameters())
    again = playground.create_root_universe(_world_parameters())
    assert root.universe_id == again.universe_id


def test_p138_intervention_rejects_negative_fork_time() -> None:
    with pytest.raises(psp.PlaygroundError):
        psp.CounterfactualIntervention(fork_time=-1.0).validate()


def test_p139_intervention_rejects_bad_fog_g() -> None:
    with pytest.raises(psp.PlaygroundError):
        psp.CounterfactualIntervention(fork_time=0.0, fog_anisotropy_g=1.0).validate()


def test_p140_intervention_rejects_non_positive_proxemics() -> None:
    with pytest.raises(psp.PlaygroundError):
        psp.CounterfactualIntervention(fork_time=0.0, target_staging_proxemics_m=0.0).validate()


def test_p141_compile_requires_two_or_four_universes() -> None:
    playground, root, ids = _two_universes()
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, [ids[0]], ancestor_universe_id=root, filesystem=object()
        )


def test_p142_compile_rejects_distinct_requirement() -> None:
    playground, root, ids = _two_universes()
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [ids[0], ids[0]],
            ancestor_universe_id=root,
            filesystem=object(),
        )


def test_p143_compile_rejects_non_descendant() -> None:
    playground = psp.PrometheusStudioPlayground()
    root_a = playground.create_root_universe(_world_parameters())
    root_b = playground.create_root_universe(_world_parameters(key_light_azimuth_deg=120.0))
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground,
            [root_a.universe_id, root_b.universe_id],
            ancestor_universe_id=root_a.universe_id,
            filesystem=object(),
        )


def test_p144_compile_rejects_bad_side() -> None:
    playground, root, ids = _two_universes()
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, ids, ancestor_universe_id=root, filesystem=object(), side=8
        )


def test_p145_compile_rejects_unsafe_out_dir() -> None:
    playground, root, ids = _two_universes()
    with pytest.raises(psp.PlaygroundError):
        psp.compile_counterfactual_quadrant_mp4(
            playground, ids, ancestor_universe_id=root, filesystem=object(), out_dir="../escape"
        )


def test_p146_merkle_empty_tree_has_no_proof() -> None:
    with pytest.raises(pvg.VerifierError):
        pvg.merkle_inclusion_proof([], 0)


def test_p147_merkle_index_out_of_range() -> None:
    with pytest.raises(pvg.VerifierError):
        pvg.merkle_inclusion_proof([b"a"], 5)


def test_p148_merkle_root_of_empty_tree_is_sha256_of_empty() -> None:
    assert pvg.merkle_root([]) == hashlib.sha256(b"").digest()


@pytest.mark.parametrize("count", [3, 5, 7, 11, 13])
def test_p149_merkle_inclusion_holds_for_odd_leaf_counts(count: int) -> None:
    leaves = [f"leaf-{i}".encode() for i in range(count)]
    root = pvg.merkle_root(leaves)
    for index in range(count):
        proof = pvg.merkle_inclusion_proof(leaves, index)
        assert pvg.verify_merkle_inclusion(leaves[index], index, count, proof, root)


def test_p150_merkle_leaf_prefix_differs_from_node_prefix() -> None:
    assert pvg.merkle_leaf_hash(b"x") != pvg.merkle_node_hash(b"x", b"x")


def test_p151_merkle_tampered_leaf_is_rejected() -> None:
    leaves = [f"leaf-{i}".encode() for i in range(5)]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 2)
    assert not pvg.verify_merkle_inclusion(b"tampered", 2, 5, proof, root)


def test_p152_merkle_second_preimage_attack_is_rejected() -> None:
    leaves = [b"a", b"b", b"ab"]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 2)
    assert pvg.verify_merkle_inclusion(b"ab", 2, 3, proof, root)
    assert not pvg.verify_merkle_inclusion(b"a", 2, 3, proof, root)


def test_p153_ledger_anti_erasure_guard_refuses_pass_to_unobserved() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("x", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    with pytest.raises(pvg.VerifierError):
        ledger.record(
            pvg.Observation(
                "x", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
            )
        )


def test_p154_ledger_records_three_states() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("a", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    ledger.record(
        pvg.Observation("b", pvg.LedgerState.VIOLATED, pvg.ObservationProvenance.ESTIMATED_PROXY)
    )
    ledger.record(
        pvg.Observation(
            "c", pvg.LedgerState.UNOBSERVED, pvg.ObservationProvenance.SYNTHETIC_FIXTURE
        )
    )
    assert ledger.state_of("a") is pvg.LedgerState.PASS
    assert ledger.state_of("b") is pvg.LedgerState.VIOLATED
    assert ledger.state_of("c") is pvg.LedgerState.UNOBSERVED


def test_p155_ledger_all_pass_flag() -> None:
    ledger = pvg.EpistemicLedger()
    ledger.record(
        pvg.Observation("a", pvg.LedgerState.PASS, pvg.ObservationProvenance.MEASURED_MEDIA)
    )
    assert ledger.all_pass is True
    assert ledger.any_violated is False


def test_p156_governed_fails_closed_without_authorizer(tmp_path: object) -> None:
    playground, root, ids = _two_universes()
    filesystem = WorkspaceFilesystem(str(tmp_path))
    with pytest.raises(AtlasGovernanceRefusalError) as excinfo:
        pvg.run_prometheus_playground_governed(
            playground,
            ids,
            ancestor_universe_id=root,
            filesystem=filesystem,
            side=24,
            frames=2,
            fps=2,
        )
    assert excinfo.value.code == "AUTHORIZER_REQUIRED"


def test_p157_quarantine_receipt_is_canonical(tmp_path: object) -> None:
    filesystem = WorkspaceFilesystem(str(tmp_path))
    receipt = pvg.quarantine_failed_attempt(
        filesystem,
        attempt_id="att",
        fencing_token=3,
        failure_reason="tampered",
        failed_bundle={"a.png": b"bytes"},
        published_root=".staging/att_3/published",
    )
    assert receipt.fencing_token == 3
    assert receipt.canonical_bytes().startswith(b"{")


def test_p158_stale_fencing_token_error_exists() -> None:
    assert issubclass(StaleFencingTokenError, RuntimeError)


def test_p159_verifier_rejects_bad_total_leaves() -> None:
    leaves = [b"a", b"b"]
    root = pvg.merkle_root(leaves)
    proof = pvg.merkle_inclusion_proof(leaves, 0)
    assert not pvg.verify_merkle_inclusion(leaves[0], 0, 0, proof, root)
    assert not pvg.verify_merkle_inclusion(leaves[0], 9, 2, proof, root)


def test_p160_verifier_detects_wav_tamper_digest(tmp_path: object) -> None:
    playground, root, ids = _two_universes()
    filesystem = WorkspaceFilesystem(str(tmp_path))
    compilation = psp.compile_counterfactual_quadrant_mp4(
        playground,
        [root, *ids],
        ancestor_universe_id=root,
        filesystem=filesystem,
        out_dir="staging/published",
        side=24,
        frames=2,
        fps=2,
    )
    raw = filesystem.read_bytes(compilation.wav_path, max_bytes=1 << 26)
    filesystem.write_bytes(
        compilation.wav_path, raw[:44] + bytes([raw[44] ^ 0xFF]) + raw[45:], create_parents=True
    )
    report = pvg.verify_prometheus_playground_bundle_from_disk(
        filesystem, compilation, require_media=False
    )
    assert report.verified is False
    assert report.ledger.state_of("wav_file_digest") is pvg.LedgerState.VIOLATED
