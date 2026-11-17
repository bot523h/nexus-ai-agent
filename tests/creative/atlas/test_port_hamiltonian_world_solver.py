"""WAVE 18 — tests for the port-Hamiltonian multi-physics studio world solver.

Every proof the module claims is *measured* here: the discrete-gradient chain
rule, the skew-symmetry of ``J``, the positive-semidefinite dissipation ``R``,
the per-step discrete energy balance, and the 500-step isolated-world drift
bound. The negative cases prove the solver fails closed on a malformed system.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import port_hamiltonian_world_solver as phs


def _harmonic(*, nonlinear: float = 0.0) -> phs.PortHamiltonianSystem:
    j = np.array([[0.0, -1.0], [1.0, 0.0]])
    r = np.zeros((2, 2))
    return phs.PortHamiltonianSystem(J=j, R=r, Q=np.eye(2), dimension=2, nonlinear=nonlinear)


def _damped_driven() -> phs.PortHamiltonianSystem:
    j = np.array([[0.0, -1.0], [1.0, 0.0]])
    r = 0.05 * np.eye(2)
    return phs.PortHamiltonianSystem(
        J=j, R=r, Q=np.diag([1.0, 2.0]), dimension=2, G=np.array([[0.0], [1.0]])
    )


# --- discrete gradient: the exact chain rule -------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_discrete_gradient_satisfies_exact_chain_rule(seed: int) -> None:
    rng = np.random.default_rng(seed)
    system = _harmonic(nonlinear=0.4)
    a = rng.normal(size=2)
    b = a + 0.05 * rng.normal(size=2)
    g = phs.discrete_gradient(system, a, b)
    lhs = float(g @ (b - a))
    rhs = system.hamiltonian(b) - system.hamiltonian(a)
    assert abs(lhs - rhs) < 1e-13


def test_discrete_gradient_reduces_to_gradient_when_states_coincide() -> None:
    system = _harmonic(nonlinear=0.3)
    x = np.array([0.4, -0.7])
    g = phs.discrete_gradient(system, x, x.copy())
    assert np.allclose(g, system.gradient(x), atol=1e-14)


def test_discrete_gradient_rejects_wrong_dimension() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.discrete_gradient(system, np.zeros(2), np.zeros(3))


def test_discrete_gradient_rejects_non_finite() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.discrete_gradient(system, np.array([np.nan, 0.0]), np.zeros(2))


# --- structural proofs: skew J, PSD R --------------------------------------------


def test_skew_symmetry_residual_is_machine_precision() -> None:
    system = _harmonic()
    j = system.jacobian(np.array([0.3, -0.1]))
    assert float(np.linalg.norm(j + j.T, ord="fro")) <= phs.SKEW_TOLERANCE


def test_dissipation_is_positive_semidefinite() -> None:
    system = _damped_driven()
    r = system.dissipation(np.array([0.2, 0.2]))
    assert float(np.linalg.eigvalsh(0.5 * (r + r.T)).min()) >= -phs.PSD_TOLERANCE


def test_jacobian_rejects_non_skew() -> None:
    bad = np.array([[0.0, -1.0], [1.0, 0.5]])
    system = phs.PortHamiltonianSystem(J=bad, R=np.zeros((2, 2)), Q=np.eye(2), dimension=2)
    with pytest.raises(phs.PortHamiltonianError):
        system.jacobian(np.zeros(2))


def test_dissipation_rejects_negative_eigenvalue() -> None:
    r = np.array([[1.0, 0.0], [0.0, -0.5]])
    system = phs.PortHamiltonianSystem(
        J=np.array([[0.0, -1.0], [1.0, 0.0]]), R=r, Q=np.eye(2), dimension=2
    )
    with pytest.raises(phs.PortHamiltonianError):
        system.dissipation(np.zeros(2))


def test_dissipation_rejects_asymmetric() -> None:
    r = np.array([[1.0, 0.3], [0.0, 1.0]])
    system = phs.PortHamiltonianSystem(
        J=np.array([[0.0, -1.0], [1.0, 0.0]]), R=r, Q=np.eye(2), dimension=2
    )
    with pytest.raises(phs.PortHamiltonianError):
        system.dissipation(np.zeros(2))


def test_jacobian_rejects_non_finite() -> None:
    system = phs.PortHamiltonianSystem(
        J=np.array([[0.0, np.inf], [-np.inf, 0.0]]),
        R=np.zeros((2, 2)),
        Q=np.eye(2),
        dimension=2,
    )
    with pytest.raises(phs.PortHamiltonianError):
        system.jacobian(np.zeros(2))


def test_dissipation_rejects_non_finite() -> None:
    system = phs.PortHamiltonianSystem(
        J=np.array([[0.0, -1.0], [1.0, 0.0]]),
        R=np.array([[np.nan, 0.0], [0.0, 1.0]]),
        Q=np.eye(2),
        dimension=2,
    )
    with pytest.raises(phs.PortHamiltonianError):
        system.dissipation(np.zeros(2))


# --- constructor validation ------------------------------------------------------


@pytest.mark.parametrize("dimension", [0, -1, phs.MAX_DIM + 1])
def test_constructor_rejects_out_of_range_dimension(dimension: int) -> None:
    side = max(dimension, 0)
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((side, side)),
            R=np.zeros((side, side)),
            Q=np.zeros((side, side)),
            dimension=dimension,
        )


def test_constructor_rejects_wrong_shaped_q() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.eye(3), dimension=2)


def test_constructor_rejects_non_symmetric_q() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.array([[1.0, 0.5], [0.0, 1.0]]),
            dimension=2,
        )


def test_constructor_rejects_non_positive_definite_q() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.array([[1.0, 0.0], [0.0, 0.0]]),
            dimension=2,
        )


def test_constructor_rejects_negative_nonlinear_coefficient() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)), R=np.zeros((2, 2)), Q=np.eye(2), dimension=2, nonlinear=-1.0
        )


def test_constructor_rejects_bad_g_shape() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.eye(2),
            dimension=2,
            G=np.zeros((3, 1)),
        )


def test_constructor_rejects_non_finite_q() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.array([[np.inf, 0.0], [0.0, 1.0]]),
            dimension=2,
        )


def test_constructor_rejects_non_finite_g() -> None:
    with pytest.raises(phs.PortHamiltonianError):
        phs.PortHamiltonianSystem(
            J=np.zeros((2, 2)),
            R=np.zeros((2, 2)),
            Q=np.eye(2),
            dimension=2,
            G=np.array([[np.nan], [0.0]]),
        )


# --- the step solver -------------------------------------------------------------


def test_step_energy_balance_is_machine_precision() -> None:
    system = _damped_driven()
    _x1, receipt = phs.solve_port_hamiltonian_step(
        system, np.array([1.0, 0.0]), dt=0.01, u=np.array([0.3])
    )
    assert receipt.energy_balance_residual <= phs.ENERGY_TOLERANCE
    assert receipt.chain_rule_residual < 1e-13
    assert receipt.skew_residual <= phs.SKEW_TOLERANCE
    assert receipt.dissipation_eigenvalue_min >= -phs.PSD_TOLERANCE
    assert receipt.passive is True
    assert receipt.iteration_count >= 1


def test_step_rejects_non_positive_dt() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.0)
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=-0.1)


def test_step_rejects_non_finite_dt() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=float("inf"))


def test_step_requires_u_when_system_has_input_port() -> None:
    system = _damped_driven()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.01)


def test_step_rejects_u_without_input_port() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.01, u=np.array([1.0]))


def test_step_rejects_wrong_u_dimension() -> None:
    system = _damped_driven()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.01, u=np.array([1.0, 2.0]))


def test_step_rejects_wrong_state_dimension() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(3), dt=0.01)


def test_step_rejects_non_positive_tolerance() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.01, tolerance=0.0)


def test_step_rejects_bad_max_iterations() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.solve_port_hamiltonian_step(system, np.zeros(2), dt=0.01, max_iterations=0)


# --- the trajectory integrator ---------------------------------------------------


def test_isolated_world_conserves_energy_over_500_steps() -> None:
    system = _harmonic()
    report = phs.integrate_port_hamiltonian(system, np.array([1.0, 0.0]), dt=0.01, steps=500)
    assert report.isolated_world_drift_bound <= phs.ENERGY_TOLERANCE
    assert len(report.receipts) == 500
    assert report.all_passive is True
    assert report.max_skew_residual <= phs.SKEW_TOLERANCE


def test_nonlinear_world_conserves_energy() -> None:
    system = _harmonic(nonlinear=0.5)
    report = phs.integrate_port_hamiltonian(system, np.array([1.0, 0.0]), dt=0.01, steps=500)
    assert report.isolated_world_drift_bound <= phs.ENERGY_TOLERANCE


def test_driven_lossy_world_balances_power() -> None:
    system = _damped_driven()
    report = phs.integrate_port_hamiltonian(
        system, np.array([1.0, 0.0]), dt=0.01, steps=50, u=np.array([0.3])
    )
    assert report.max_energy_balance_residual <= phs.ENERGY_TOLERANCE
    # A driven world is not the isolated one, so no conservation bound is claimed.
    assert report.isolated_world_drift_bound == 0.0


def test_trajectory_receipts_have_monotone_step_index() -> None:
    system = _harmonic()
    report = phs.integrate_port_hamiltonian(system, np.array([1.0, 0.0]), dt=0.01, steps=5)
    assert [r.step_index for r in report.receipts] == [0, 1, 2, 3, 4]
    assert report.states.shape == (6, 2)


def test_integrate_rejects_zero_steps() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.integrate_port_hamiltonian(system, np.zeros(2), dt=0.01, steps=0)


def test_integrate_rejects_too_many_steps() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.integrate_port_hamiltonian(system, np.zeros(2), dt=0.01, steps=phs.MAX_STEPS + 1)


def test_integrate_rejects_wrong_state_dimension() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.integrate_port_hamiltonian(system, np.zeros(3), dt=0.01, steps=5)


def test_isolated_world_drift_bound_is_enforced() -> None:
    system = _harmonic()
    with pytest.raises(phs.PortHamiltonianError):
        phs.integrate_port_hamiltonian(
            system, np.array([1.0, 0.0]), dt=0.01, steps=500, drift_bound=1e-30
        )


def test_trajectory_report_serialises() -> None:
    system = _harmonic()
    report = phs.integrate_port_hamiltonian(system, np.array([1.0, 0.0]), dt=0.01, steps=3)
    payload = report.to_dict()
    assert payload["steps"] == 3
    assert payload["kind"] == "atlas.port_hamiltonian_trajectory"
    assert len(payload["receipts"]) == 3  # type: ignore[arg-type]
