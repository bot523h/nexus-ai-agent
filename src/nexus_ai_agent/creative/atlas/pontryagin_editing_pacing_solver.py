"""WAVE 18 — Pontryagin-minimum-principle editing-velocity solver.

Editing pacing is a control problem: the *state* is the film's accumulated
narrative tension / position through the story, and the *control* is the pacing
rate (how much story time a cut advances). This module solves the discrete
finite-horizon **linear-quadratic** (LQ) optimal-control problem

    x_{k+1} = A x_k + B u_k
    J = x_N^T Q_f x_N + Σ_{k=0}^{N-1} ( x_k^T Q x_k + u_k^T R u_k )

which is exactly the class for which the discrete Pontryagin minimum principle
and the Riccati recursion give a **provably optimal** feedback law
``u_k* = -K_k x_k``. It provides, in pure NumPy + stdlib:

* :func:`solve_lq_optimal_control` — backward Riccati recursion producing the
  optimal gains ``K_k``, the closed-loop trajectory, the cost-to-go values
  ``V_k = x_k^T P_k x_k`` and the costate (adjoint) sequence ``λ_k = 2 P_k x_k``;
* :func:`brute_force_lq_cost` — an independent grid-search oracle used to verify
  optimality on small scalar problems;
* :func:`pacing_plan_from_control` — converts the control sequence into per-beat
  durations (positive, bounded), a human-readable pacing plan.

The optimality claim is scoped strictly to the stated finite LQ problem. It is
not a claim about artistic quality, audience response, or any quantity that the
inputs do not define.

Bounded, validated, offline and deterministic; malformed inputs raise
:class:`PacingError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "MAX_HORIZON",
    "MAX_STATE_DIM",
    "PacingError",
    "LQProblem",
    "LQSolution",
    "solve_lq_optimal_control",
    "brute_force_lq_cost",
    "pacing_plan_from_control",
]

MAX_HORIZON: Final[int] = 200_000
MAX_STATE_DIM: Final[int] = 64
_EPS: Final[float] = 1e-12


class PacingError(ValueError):
    """Raised for malformed pacing/control inputs."""


@dataclass(frozen=True)
class LQProblem:
    """A discrete finite-horizon LQ optimal-control problem."""

    A: np.ndarray  # (n, n) state transition
    B: np.ndarray  # (n, m) control matrix
    Q: np.ndarray  # (n, n) running state cost (PSD)
    R: np.ndarray  # (m, m) running control cost (PD)
    Qf: np.ndarray  # (n, n) terminal state cost (PSD)
    x0: np.ndarray  # (n,) initial state
    horizon: int

    def __post_init__(self) -> None:
        a = np.asarray(self.A, dtype=np.float64)
        b = np.asarray(self.B, dtype=np.float64)
        q = np.asarray(self.Q, dtype=np.float64)
        r = np.asarray(self.R, dtype=np.float64)
        qf = np.asarray(self.Qf, dtype=np.float64)
        x0 = np.asarray(self.x0, dtype=np.float64)
        if a.ndim != 2 or a.shape[0] != a.shape[1]:
            raise PacingError("A must be square")
        n = a.shape[0]
        if n < 1 or n > MAX_STATE_DIM:
            raise PacingError(f"state dim must be in [1, {MAX_STATE_DIM}]")
        if b.ndim != 2 or b.shape[0] != n:
            raise PacingError("B must be (n, m)")
        m = b.shape[1]
        if m < 1:
            raise PacingError("control dim must be >= 1")
        for mat, shape, name in ((q, (n, n), "Q"), (r, (m, m), "R"), (qf, (n, n), "Qf")):
            if mat.shape != shape:
                raise PacingError(f"{name} must be {shape}")
        if x0.shape != (n,):
            raise PacingError("x0 must be (n,)")
        for mat, name in ((a, "A"), (b, "B"), (q, "Q"), (r, "R"), (qf, "Qf"), (x0, "x0")):
            if not np.all(np.isfinite(mat)):
                raise PacingError(f"{name} contains NaN/Inf")
        if not _is_symmetric_psd(q):
            raise PacingError("Q must be symmetric positive semi-definite")
        if not _is_symmetric_psd(qf):
            raise PacingError("Qf must be symmetric positive semi-definite")
        if not _is_symmetric_pd(r):
            raise PacingError("R must be symmetric positive definite")
        if self.horizon < 1 or self.horizon > MAX_HORIZON:
            raise PacingError(f"horizon must be in [1, {MAX_HORIZON}]")
        object.__setattr__(self, "A", a)
        object.__setattr__(self, "B", b)
        object.__setattr__(self, "Q", q)
        object.__setattr__(self, "R", r)
        object.__setattr__(self, "Qf", qf)
        object.__setattr__(self, "x0", x0)


def _is_symmetric_psd(mat: np.ndarray, tol: float = 1e-9) -> bool:
    if not np.allclose(mat, mat.T, atol=tol):
        return False
    eigs = np.linalg.eigvalsh((mat + mat.T) / 2.0)
    return bool(np.min(eigs) >= -tol * max(1.0, float(np.max(np.abs(eigs)))))


def _is_symmetric_pd(mat: np.ndarray, tol: float = 1e-9) -> bool:
    if not np.allclose(mat, mat.T, atol=tol):
        return False
    eigs = np.linalg.eigvalsh((mat + mat.T) / 2.0)
    return bool(np.min(eigs) > tol * max(1.0, float(np.max(np.abs(eigs)))))


@dataclass
class LQSolution:
    """The provably optimal LQ solution and its Pontryagin quantities."""

    gains: np.ndarray  # (N, m, n) optimal feedback gains K_k
    states: np.ndarray  # (N+1, n) closed-loop trajectory
    controls: np.ndarray  # (N, m)
    costates: np.ndarray  # (N+1, n) adjoint lambda_k = 2 P_k x_k
    riccati: np.ndarray  # (N+1, n, n) value matrices P_k
    total_cost: float
    optimal: bool  # True: the Riccati recursion proves optimality for this LQ problem


def solve_lq_optimal_control(problem: LQProblem) -> LQSolution:
    """Solve the discrete LQ problem by the backward Riccati recursion.

    For ``k = N-1 .. 0`` with ``P_N = Qf``:

        K_k = (R + B^T P_{k+1} B)^{-1} B^T P_{k+1} A
        P_k = Q + A^T P_{k+1} A - A^T P_{k+1} B K_k

    The optimal control is ``u_k* = -K_k x_k`` and the cost-to-go is
    ``V_k = x_k^T P_k x_k``; the costate is ``λ_k = 2 P_k x_k``. This is exact
    for the stated finite LQ problem (no approximation), so ``optimal`` is
    ``True`` for any problem that reaches this routine.
    """
    a, b, q, r, qf = problem.A, problem.B, problem.Q, problem.R, problem.Qf
    n, m = a.shape[0], b.shape[1]
    horizon = problem.horizon

    p = qf.copy()
    riccati = np.empty((horizon + 1, n, n))
    riccati[horizon] = p
    gains = np.empty((horizon, m, n))
    for k in range(horizon - 1, -1, -1):
        btp = b.T @ p
        s = r + btp @ b
        k_gain = np.linalg.solve(s, btp @ a)
        gains[k] = k_gain
        p = q + a.T @ p @ a - a.T @ p @ b @ k_gain
        p = (p + p.T) / 2.0  # keep symmetry against round-off
        riccati[k] = p

    states = np.empty((horizon + 1, n))
    controls = np.empty((horizon, m))
    costates = np.empty((horizon + 1, n))
    x = problem.x0.copy()
    states[0] = x
    for k in range(horizon):
        u = -gains[k] @ x
        controls[k] = u
        x = a @ x + b @ u
        states[k + 1] = x
    for k in range(horizon + 1):
        costates[k] = 2.0 * riccati[k] @ states[k]

    total = float(problem.x0 @ riccati[0] @ problem.x0)
    return LQSolution(
        gains=gains,
        states=states,
        controls=controls,
        costates=costates,
        riccati=riccati,
        total_cost=total,
        optimal=True,
    )


def brute_force_lq_cost(
    problem: LQProblem,
    *,
    control_grid: np.ndarray,
) -> tuple[float, np.ndarray]:
    """Independent oracle: minimise the LQ cost by exhaustive grid search.

    Only valid for scalar control problems (``m == 1``) with a small horizon.
    Enumerates every control sequence on ``control_grid`` and returns the minimum
    cost and the argmin sequence. Used in tests to confirm the Riccati optimum.
    """
    if problem.B.shape[1] != 1:
        raise PacingError("brute-force oracle supports scalar control only")
    grid = np.asarray(control_grid, dtype=np.float64)
    if grid.ndim != 1 or grid.size == 0 or not np.all(np.isfinite(grid)):
        raise PacingError("control_grid must be a non-empty finite 1-D array")
    horizon = problem.horizon
    total_combos = grid.size**horizon
    if total_combos > 2_000_000:
        raise PacingError("brute-force search space too large")

    best_cost = np.inf
    best_seq = np.zeros(horizon)
    a, b, q, r, qf = problem.A, problem.B, problem.Q, problem.R, problem.Qf
    for combo in range(total_combos):
        u_seq = np.empty(horizon)
        c = combo
        for k in range(horizon):
            u_seq[k] = grid[c % grid.size]
            c //= grid.size
        x = problem.x0.copy()
        cost = 0.0
        for k in range(horizon):
            u = np.array([u_seq[k]])
            cost += float(x @ q @ x) + float(u @ r @ u)
            x = a @ x + b @ u
        cost += float(x @ qf @ x)
        if cost < best_cost - 1e-12:
            best_cost = cost
            best_seq = u_seq
    return float(best_cost), best_seq


@dataclass
class PacingPlan:
    """A human-readable per-beat pacing plan derived from the control sequence."""

    durations: np.ndarray  # (N,) positive per-beat durations (seconds)
    cut_rates: np.ndarray  # (N,) cuts per second (derived from durations)
    total_duration: float


def pacing_plan_from_control(
    controls: np.ndarray,
    *,
    base_duration_s: float = 4.0,
    min_duration_s: float = 0.5,
    max_duration_s: float = 20.0,
) -> PacingPlan:
    """Map a scalar control sequence to bounded per-beat durations.

    A positive control *accelerates* pacing (shorter beats): the duration is
    ``clip(base_duration * exp(-u), min_duration, max_duration)``, so the map is
    smooth, monotone in ``u``, and always produces physically valid durations.
    """
    u = np.asarray(controls, dtype=np.float64)
    if u.ndim != 1 or u.size == 0:
        raise PacingError("controls must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(u)):
        raise PacingError("controls contain NaN/Inf")
    if min_duration_s <= 0.0 or max_duration_s < min_duration_s:
        raise PacingError("durations must satisfy 0 < min <= max")
    if base_duration_s <= 0.0:
        raise PacingError("base_duration_s must be positive")
    durations = np.clip(base_duration_s * np.exp(-u), min_duration_s, max_duration_s)
    cut_rates = 1.0 / durations
    return PacingPlan(
        durations=durations,
        cut_rates=cut_rates,
        total_duration=float(np.sum(durations)),
    )
