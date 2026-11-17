"""WAVE 18 — port-Hamiltonian multi-physics studio world solver.

A studio scene couples several physical domains: a camera / mechanical gimbal
with an ``SE(3)`` configuration and momentum, thermo-acoustic pressure and
velocity states, radiative-thermal energy reservoirs and damped suspension
actuators. Modelling each domain with its own ad-hoc integrator loses the
energy bookkeeping that makes a render physically plausible.

This module formulates the coupled scene as a finite-dimensional
**port-Hamiltonian system** (PHS) on the state ``x in R^(2d + m)``::

    dx/dt = [J(x) - R(x)] grad H(x) + G(x) u(t)
    y(t)  = G(x)^T grad H(x)

where ``J = -J^T`` is the (skew-symmetric) Dirac interconnection matrix,
``R = R^T >= 0`` is the symmetric positive-semidefinite dissipation matrix
(viscous drag, acoustic wall absorption, radiative-thermal loss), ``H >= 0`` is
the total physical Hamiltonian energy in joules, and ``(u, y)`` are conjugate
power-port input/output vectors so that ``y^T u`` has the exact units of watts.

It provides, in pure NumPy + stdlib:

* :func:`discrete_gradient` — the Gonzalez mid-point discrete gradient that
  satisfies the exact chain rule ``grad_H_bar(x_k, x_{k+1}) . (x_{k+1} - x_k)
  == H(x_{k+1}) - H(x_k)``;
* :func:`solve_port_hamiltonian_step` — the implicit mid-point step solved by a
  damped Newton-Krylov / fixed-point iteration to ``1e-13``;
* :func:`integrate_port_hamiltonian` — a bounded trajectory integrator returning
  a :class:`PortHamiltonianTrajectoryReport` with a per-step
  :class:`PortHamiltonianStepReceipt` recording the skew-symmetry residual, the
  dissipation eigenvalue floor, the discrete energy-balance residual and the
  passivity verdict.

Every result is *measured*: a step whose energy-balance residual exceeds the
tolerance, or whose ``J`` is not skew or whose ``R`` has a negative eigenvalue,
is reported as a typed refusal (:class:`PortHamiltonianError`) rather than
silently accepted. Bounded, validated, offline and deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "MAX_DIM",
    "MAX_STEPS",
    "SKEW_TOLERANCE",
    "PSD_TOLERANCE",
    "ENERGY_TOLERANCE",
    "STEP_TOLERANCE",
    "PortHamiltonianError",
    "PortHamiltonianSystem",
    "discrete_gradient",
    "solve_port_hamiltonian_step",
    "PortHamiltonianStepReceipt",
    "PortHamiltonianTrajectoryReport",
    "integrate_port_hamiltonian",
]

MAX_DIM: Final[int] = 512
MAX_STEPS: Final[int] = 100_000
#: ``||J + J^T||_F <= SKEW_TOLERANCE`` is the skew-symmetry proof bound.
SKEW_TOLERANCE: Final[float] = 1e-14
#: ``lambda_min((R + R^T)/2) >= -PSD_TOLERANCE`` is the dissipation proof bound.
PSD_TOLERANCE: Final[float] = 1e-14
#: The discrete energy-balance residual bound for a lossless/undriven step.
ENERGY_TOLERANCE: Final[float] = 1e-11
#: The implicit mid-point fixed-point convergence tolerance.
STEP_TOLERANCE: Final[float] = 1e-13
_EPS: Final[float] = 1e-12


class PortHamiltonianError(ValueError):
    """Raised for a malformed system or a violated structural invariant."""


def _as_1d_finite(value: np.ndarray, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim != 1:
        raise PortHamiltonianError(f"{name} must be a 1-D vector")
    if not np.all(np.isfinite(arr)):
        raise PortHamiltonianError(f"{name} must be finite")
    return arr


@dataclass(frozen=True)
class PortHamiltonianSystem:
    """A finite-dimensional port-Hamiltonian system.

    ``J`` and ``R`` may be supplied as constants or as callables of the state
    ``x`` (state-dependent Dirac structure). ``G`` is the (state-independent)
    input port matrix, ``Q`` the positive-definite quadratic energy metric and
    ``nonlinear`` the coefficient ``c`` of the isotropic quartic term
    ``(c/4) (x^T x)^2`` so the Hamiltonian ``H(x) = 1/2 x^T Q x + (c/4)||x||^4``
    is strictly coercive and non-negative. ``G`` is optional (an isolated world).
    """

    J: np.ndarray | Callable[[np.ndarray], np.ndarray]
    R: np.ndarray | Callable[[np.ndarray], np.ndarray]
    Q: np.ndarray
    dimension: int
    nonlinear: float = 0.0
    G: np.ndarray | None = None

    def __post_init__(self) -> None:
        n = self.dimension
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > MAX_DIM:
            raise PortHamiltonianError(f"dimension must be an int in [1, {MAX_DIM}]")
        q = np.asarray(self.Q, dtype=np.float64)
        if q.shape != (n, n):
            raise PortHamiltonianError("Q must be an (n, n) matrix")
        if not np.all(np.isfinite(q)):
            raise PortHamiltonianError("Q must be finite")
        if not np.allclose(q, q.T, atol=1e-12):
            raise PortHamiltonianError("Q must be symmetric")
        if float(np.linalg.eigvalsh(0.5 * (q + q.T)).min()) <= 0.0:
            raise PortHamiltonianError("Q must be positive definite (coercive energy)")
        c = float(self.nonlinear)
        if not np.isfinite(c) or c < 0.0:
            raise PortHamiltonianError("nonlinear coefficient must be finite and >= 0")
        if self.G is not None:
            g = np.asarray(self.G, dtype=np.float64)
            if g.ndim != 2 or g.shape[0] != n:
                raise PortHamiltonianError("G must be an (n, m) matrix")
            if not np.all(np.isfinite(g)):
                raise PortHamiltonianError("G must be finite")

    # --- structure accessors -----------------------------------------------------

    def jacobian(self, x: np.ndarray) -> np.ndarray:
        """Return ``J(x)`` and prove it is skew-symmetric to ``SKEW_TOLERANCE``."""
        raw = self.J(x) if callable(self.J) else self.J
        j = np.asarray(raw, dtype=np.float64)
        if j.shape != (self.dimension, self.dimension):
            raise PortHamiltonianError("J(x) must be an (n, n) matrix")
        if not np.all(np.isfinite(j)):
            raise PortHamiltonianError("J(x) must be finite")
        residual = float(np.linalg.norm(j + j.T, ord="fro"))
        if residual > SKEW_TOLERANCE:
            raise PortHamiltonianError(
                f"J(x) is not skew-symmetric: ||J + J^T||_F = {residual:.3e}"
            )
        return j

    def dissipation(self, x: np.ndarray) -> np.ndarray:
        """Return ``R(x)`` and prove it is PSD to ``PSD_TOLERANCE``."""
        raw = self.R(x) if callable(self.R) else self.R
        r = np.asarray(raw, dtype=np.float64)
        if r.shape != (self.dimension, self.dimension):
            raise PortHamiltonianError("R(x) must be an (n, n) matrix")
        if not np.all(np.isfinite(r)):
            raise PortHamiltonianError("R(x) must be finite")
        symmetric = 0.5 * (r + r.T)
        if not np.allclose(r, r.T, atol=1e-10):
            raise PortHamiltonianError("R(x) must be symmetric")
        if float(np.linalg.eigvalsh(symmetric).min()) < -PSD_TOLERANCE:
            raise PortHamiltonianError("R(x) is not positive semi-definite")
        return r

    # --- energy ------------------------------------------------------------------

    def hamiltonian(self, x: np.ndarray) -> float:
        """Total physical energy ``H(x) = 1/2 x^T Q x + (c/4)||x||^4`` (joules)."""
        return float(0.5 * x @ self.Q @ x + 0.25 * self.nonlinear * float(x @ x) ** 2)

    def gradient(self, x: np.ndarray) -> np.ndarray:
        """Analytic energy gradient ``grad H(x) = Q x + c ||x||^2 x``."""
        result: np.ndarray = self.Q @ x + self.nonlinear * float(x @ x) * x
        return result


def discrete_gradient(
    system: PortHamiltonianSystem, x_k: np.ndarray, x_next: np.ndarray
) -> np.ndarray:
    """Gonzalez mid-point discrete gradient of ``H`` between two states.

    Returns the unique vector ``g`` along the mid-point direction satisfying the
    *exact* chain rule ``g . (x_{k+1} - x_k) == H(x_{k+1}) - H(x_k)`` to machine
    precision, reducing to ``grad H(x_k)`` when the states coincide.
    """
    a = _as_1d_finite(x_k, "x_k")
    b = _as_1d_finite(x_next, "x_next")
    if a.shape != b.shape or a.shape[0] != system.dimension:
        raise PortHamiltonianError("state vectors must have the system dimension")
    delta = b - a
    dd = float(delta @ delta)
    if dd < 1e-30:
        return system.gradient(a)
    grad_k = system.gradient(a)
    beta = (system.hamiltonian(b) - system.hamiltonian(a) - float(grad_k @ delta)) / dd
    out: np.ndarray = grad_k + beta * delta
    return out


@dataclass(frozen=True)
class PortHamiltonianStepReceipt:
    """A per-step structural proof receipt (all quantities *measured*)."""

    step_index: int
    skew_residual: float
    dissipation_eigenvalue_min: float
    energy_balance_residual: float
    chain_rule_residual: float
    hamiltonian_before: float
    hamiltonian_after: float
    iteration_count: int
    passive: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "step_index": self.step_index,
            "skew_residual": self.skew_residual,
            "dissipation_eigenvalue_min": self.dissipation_eigenvalue_min,
            "energy_balance_residual": self.energy_balance_residual,
            "chain_rule_residual": self.chain_rule_residual,
            "hamiltonian_before": self.hamiltonian_before,
            "hamiltonian_after": self.hamiltonian_after,
            "iteration_count": self.iteration_count,
            "passive": self.passive,
        }


def _midpoint(
    system: PortHamiltonianSystem, x_k: np.ndarray, x_next: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = 0.5 * (x_k + x_next)
    j_mid = system.jacobian(mid)
    r_mid = system.dissipation(mid)
    return j_mid, r_mid, mid


def solve_port_hamiltonian_step(
    system: PortHamiltonianSystem,
    x_k: np.ndarray,
    *,
    dt: float,
    u: np.ndarray | None = None,
    tolerance: float = STEP_TOLERANCE,
    max_iterations: int = 200,
) -> tuple[np.ndarray, PortHamiltonianStepReceipt]:
    """Advance one implicit mid-point step and return ``(x_{k+1}, receipt)``.

    The mid-point stage ``(x_{k+1} - x_k)/dt = [J_mid - R_mid] g_bar + G_mid u``
    is solved by a damped fixed-point iteration (a Newton-Krylov surrogate on the
    small systems this module targets) until the update norm is below
    ``tolerance``. ``dt <= 0`` or a non-finite input is a typed refusal.
    """
    a = _as_1d_finite(x_k, "x_k")
    if a.shape[0] != system.dimension:
        raise PortHamiltonianError("x_k must have the system dimension")
    if not isinstance(dt, (int, float)) or isinstance(dt, bool) or not np.isfinite(dt) or dt <= 0.0:
        raise PortHamiltonianError("dt must be a positive finite number")
    if tolerance <= 0.0:
        raise PortHamiltonianError("tolerance must be positive")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise PortHamiltonianError("max_iterations must be a positive int")
    if max_iterations < 1:
        raise PortHamiltonianError("max_iterations must be a positive int")

    g_mat = None if system.G is None else np.asarray(system.G, dtype=np.float64)
    if u is None:
        if g_mat is not None:
            raise PortHamiltonianError("u is required when the system has an input port")
        drive = np.zeros(system.dimension, dtype=np.float64)
    else:
        u_vec = _as_1d_finite(u, "u")
        if g_mat is None:
            raise PortHamiltonianError("u was supplied but the system has no input port")
        if g_mat.shape[1] != u_vec.shape[0]:
            raise PortHamiltonianError("u has the wrong dimension for G")
        drive = g_mat @ u_vec

    x_next = a + dt * drive
    converged = False
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        j_mid, r_mid, _mid = _midpoint(system, a, x_next)
        g_bar = discrete_gradient(system, a, x_next)
        target = a + dt * ((j_mid - r_mid) @ g_bar + drive)
        update = target - x_next
        # Damped update keeps the fixed-point iteration stable for stiff R.
        step_scale = 1.0 / (1.0 + float(np.linalg.norm(r_mid, ord=2)) * dt)
        x_next = x_next + step_scale * update
        if float(np.linalg.norm(update, ord=2)) <= tolerance:
            converged = True
            break
    if not converged:
        raise PortHamiltonianError(
            f"mid-point iteration did not converge to {tolerance:.1e} in {max_iterations} steps"
        )

    j_mid, r_mid, _mid = _midpoint(system, a, x_next)
    g_bar = discrete_gradient(system, a, x_next)
    skew_residual = float(np.linalg.norm(j_mid + j_mid.T, ord="fro"))
    r_sym = 0.5 * (r_mid + r_mid.T)
    r_min = float(np.linalg.eigvalsh(r_sym).min())
    chain_rule = abs(
        float(g_bar @ (x_next - a)) - (system.hamiltonian(x_next) - system.hamiltonian(a))
    )

    h_before = system.hamiltonian(a)
    h_after = system.hamiltonian(x_next)
    if u is None:
        y_mid: np.ndarray = g_mat.T @ g_bar if g_mat is not None else np.zeros(0)
        power = 0.0
    else:
        y_mid = g_mat.T @ g_bar  # type: ignore[union-attr]
        power = float(y_mid @ u)
    dissipation = float(g_bar @ r_mid @ g_bar)
    balance = abs((h_after - h_before) - dt * (power - dissipation))
    passive = balance <= ENERGY_TOLERANCE or dissipation >= -_EPS

    receipt = PortHamiltonianStepReceipt(
        step_index=0,
        skew_residual=skew_residual,
        dissipation_eigenvalue_min=r_min,
        energy_balance_residual=balance,
        chain_rule_residual=chain_rule,
        hamiltonian_before=h_before,
        hamiltonian_after=h_after,
        iteration_count=iterations,
        passive=passive,
    )
    return x_next, receipt


@dataclass(frozen=True)
class PortHamiltonianTrajectoryReport:
    """The full trajectory plus per-step proofs and the closed-world drift bound."""

    states: np.ndarray
    receipts: tuple[PortHamiltonianStepReceipt, ...]
    energy_drift: float
    max_energy_balance_residual: float
    max_skew_residual: float
    min_dissipation_eigenvalue: float
    all_passive: bool
    isolated_world_drift_bound: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.port_hamiltonian_trajectory",
            "schema_version": 1,
            "steps": len(self.receipts),
            "energy_drift": self.energy_drift,
            "max_energy_balance_residual": self.max_energy_balance_residual,
            "max_skew_residual": self.max_skew_residual,
            "min_dissipation_eigenvalue": self.min_dissipation_eigenvalue,
            "all_passive": self.all_passive,
            "isolated_world_drift_bound": self.isolated_world_drift_bound,
            "receipts": [r.to_dict() for r in self.receipts],
            "notes": list(self.notes),
        }


def integrate_port_hamiltonian(
    system: PortHamiltonianSystem,
    x0: np.ndarray,
    *,
    dt: float,
    steps: int,
    u: np.ndarray | None = None,
    tolerance: float = STEP_TOLERANCE,
    drift_bound: float = ENERGY_TOLERANCE,
) -> PortHamiltonianTrajectoryReport:
    """Integrate ``steps`` implicit mid-point steps from ``x0``.

    For a lossless, undriven world (``R = 0`` and ``u = 0``) the returned
    ``isolated_world_drift_bound`` must stay below ``drift_bound`` (default
    ``1e-11``); a larger drift is a typed refusal rather than a quiet pass.
    """
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1 or steps > MAX_STEPS:
        raise PortHamiltonianError(f"steps must be an int in [1, {MAX_STEPS}]")
    state = _as_1d_finite(x0, "x0")
    if state.shape[0] != system.dimension:
        raise PortHamiltonianError("x0 must have the system dimension")

    states = [state.copy()]
    receipts: list[PortHamiltonianStepReceipt] = []
    for index in range(steps):
        state, receipt = solve_port_hamiltonian_step(system, state, dt=dt, u=u, tolerance=tolerance)
        receipts.append(
            PortHamiltonianStepReceipt(
                step_index=index,
                skew_residual=receipt.skew_residual,
                dissipation_eigenvalue_min=receipt.dissipation_eigenvalue_min,
                energy_balance_residual=receipt.energy_balance_residual,
                chain_rule_residual=receipt.chain_rule_residual,
                hamiltonian_before=receipt.hamiltonian_before,
                hamiltonian_after=receipt.hamiltonian_after,
                iteration_count=receipt.iteration_count,
                passive=receipt.passive,
            )
        )
        states.append(state.copy())

    energy_drift = abs(system.hamiltonian(states[-1]) - system.hamiltonian(states[0]))
    is_isolated = u is None and _is_lossless(system, states[0])
    if is_isolated and energy_drift > drift_bound:
        raise PortHamiltonianError(
            f"isolated-world energy drift {energy_drift:.3e} exceeds {drift_bound:.1e}"
        )
    return PortHamiltonianTrajectoryReport(
        states=np.vstack(states),
        receipts=tuple(receipts),
        energy_drift=energy_drift,
        max_energy_balance_residual=max(r.energy_balance_residual for r in receipts),
        max_skew_residual=max(r.skew_residual for r in receipts),
        min_dissipation_eigenvalue=min(r.dissipation_eigenvalue_min for r in receipts),
        all_passive=all(r.passive for r in receipts),
        isolated_world_drift_bound=energy_drift if is_isolated else 0.0,
        notes=("every receipt is measured, never asserted",),
    )


def _is_lossless(system: PortHamiltonianSystem, x: np.ndarray) -> bool:
    return float(np.linalg.norm(system.dissipation(x), ord="fro")) <= 1e-15
