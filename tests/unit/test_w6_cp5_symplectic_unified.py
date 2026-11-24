"""W6-CP5 (W6.4): analytic-vs-finite-difference separation + full symplectic identity.

The analytic Jacobian (closed-form chain rule, no finite-difference ``eps``)
and the finite-difference oracle (central differences, ``eps=1e-6``) are
*separate* code paths by design.  This file pins both properties explicitly:

* ``test_analytic_jacobian_matches_finite_difference_quartic_and_damped`` —
  the two Jacobian classes agree to 1e-7 for non-zero (q, p), with and without
  quartic coupling (α = 0, α > 0), with and without Rayleigh damping (c = 0, c > 0).
* ``test_analytic_jacobian_satisfies_full_symplectic_matrix_identity`` —
  the analytic Jacobian satisfies the *full* symplectic matrix identity
  ``J^T Ω J = Ω`` (not just ``det J = 1``) to 1e-12, with the standard
  ``Ω = [[0, I_n], [-I_n, 0]]``, for the conservative (c = 0) case in the
  presence of the nonlinear quartic term (α > 0); and for the damped case
  (c > 0) the determinant identity ``det(J) = exp(-n c Δt / m)`` at 1e-12.
"""

from __future__ import annotations

import numpy as np

from nexus_ai_agent.creative.science.symplectic_hamiltonian_pipeline import (
    HamiltonianSpec,
    analytic_jacobian_of_step,
    jacobian_of_step,
)


def _omega(n: int) -> np.ndarray:
    """Standard symplectic matrix ``Ω = [[0, I_n], [-I_n, 0]]``."""
    omega = np.zeros((2 * n, 2 * n))
    omega[:n, n:] = np.eye(n)
    omega[n:, :n] = -np.eye(n)
    return omega


class TestAnalyticFiniteDifferenceSeparation:
    def test_analytic_jacobian_matches_finite_difference_quartic_and_damped(self):
        # Nonzero state, nonzero mass, both α settings, both c settings.
        spec_alpha_zero = HamiltonianSpec(
            mass=1.3, stiffness=np.diag([1.1, 2.2]), quartic_coupling=0.0
        )
        spec_alpha = HamiltonianSpec(mass=1.3, stiffness=np.diag([1.1, 2.2]), quartic_coupling=0.3)
        q = np.array([0.4, -0.7])  # q != 0
        p = np.array([0.5, 0.2])  # p != 0
        dt = 0.02
        for spec, damping in (
            (spec_alpha_zero, 0.0),
            (spec_alpha_zero, 0.4),
            (spec_alpha, 0.0),
            (spec_alpha, 0.4),
        ):
            fd = jacobian_of_step(q, p, dt, spec, rayleigh_damping=damping)
            an = analytic_jacobian_of_step(spec, q, p, dt, rayleigh_damping=damping)
            err = np.linalg.norm(an - fd, ord="fro")
            assert err < 1e-7, (
                f"FD vs analytic residual {err:.3e} (alpha={spec.quartic_coupling}, "
                f"damping={damping}) must stay below 1e-7"
            )


class TestFullSymplecticMatrixIdentity:
    def test_analytic_jacobian_satisfies_full_symplectic_matrix_identity(self):
        # Conservative case (c = 0), quartic coupling α > 0, nonzero state.
        d = 2
        omega = _omega(d)
        mass = 1.0
        spec = HamiltonianSpec(mass=mass, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        q = np.array([0.5, 0.3])
        p = np.array([0.2, -0.1])
        for dt in (0.05, 0.01, 0.001):
            jac_conservative = analytic_jacobian_of_step(spec, q, p, dt, rayleigh_damping=0.0)
            residual = np.linalg.norm(
                jac_conservative.T @ omega @ jac_conservative - omega, ord="fro"
            )
            assert residual < 1e-12, f"full symplectic residual {residual:.3e} at dt={dt}"

        # Damped case (c > 0): det(J_analytic) == exp(-n c dt / m).
        c = 0.4
        dt = 0.05
        spec_damped = HamiltonianSpec(
            mass=mass, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2
        )
        jac_damped = analytic_jacobian_of_step(spec_damped, q, p, dt, rayleigh_damping=c)
        n = spec_damped.dimension
        det_jacobian = float(np.linalg.det(jac_damped))
        expected_det = np.exp(-n * c * dt / mass)
        assert abs(det_jacobian - expected_det) < 1e-12, (
            f"det(J) = {det_jacobian:.15f} must equal exp(-n c Δt / m) = {expected_det:.15f}"
        )
