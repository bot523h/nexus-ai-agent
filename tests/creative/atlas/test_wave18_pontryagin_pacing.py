"""WAVE 18 — tests for the Pontryagin/Riccati LQ pacing solver."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import pontryagin_editing_pacing_solver as pp


def _scalar_problem(horizon: int = 3) -> pp.LQProblem:
    return pp.LQProblem(
        A=np.array([[1.0]]),
        B=np.array([[1.0]]),
        Q=np.array([[1.0]]),
        R=np.array([[1.0]]),
        Qf=np.array([[1.0]]),
        x0=np.array([1.0]),
        horizon=horizon,
    )


# --- problem validation ----------------------------------------------------------


def test_problem_rejects_nonsquare_A() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.ones((2, 3)),
            B=np.ones((2, 1)),
            Q=np.eye(2),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_B_shape() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((3, 1)),
            Q=np.eye(2),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_Q_shape() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 1)),
            Q=np.eye(3),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_x0_shape() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 1)),
            Q=np.eye(2),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(3),
            horizon=2,
        )


def test_problem_rejects_nan() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.array([[np.nan]]),
            B=np.ones((1, 1)),
            Q=np.eye(1),
            R=np.eye(1),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=2,
        )


def test_problem_rejects_non_psd_Q() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.array([[-1.0]]),
            R=np.eye(1),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=2,
        )


def test_problem_rejects_non_pd_R() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.eye(1),
            R=np.array([[0.0]]),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=2,
        )


def test_problem_rejects_asymmetric_Q() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 1)),
            Q=np.array([[1.0, 0.5], [0.0, 1.0]]),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_bad_horizon() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.eye(1),
            R=np.eye(1),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=0,
        )
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.eye(1),
            R=np.eye(1),
            Qf=np.eye(1),
            x0=np.zeros(1),
            horizon=pp.MAX_HORIZON + 1,
        )


# --- solver ----------------------------------------------------------------------


def test_solver_returns_optimal_claim() -> None:
    sol = pp.solve_lq_optimal_control(_scalar_problem(3))
    assert sol.optimal


def test_solver_shapes() -> None:
    sol = pp.solve_lq_optimal_control(_scalar_problem(4))
    assert sol.gains.shape == (4, 1, 1)
    assert sol.states.shape == (5, 1)
    assert sol.controls.shape == (4, 1)
    assert sol.riccati.shape == (5, 1, 1)


def test_solver_cost_matches_direct_evaluation() -> None:
    prob = _scalar_problem(3)
    sol = pp.solve_lq_optimal_control(prob)
    a, b, q, r, qf = prob.A, prob.B, prob.Q, prob.R, prob.Qf
    x = prob.x0.copy()
    cost = 0.0
    for k in range(prob.horizon):
        u = sol.controls[k]
        cost += float(x @ q @ x) + float(u @ r @ u)
        x = a @ x + b @ u
    cost += float(x @ qf @ x)
    assert cost == pytest.approx(sol.total_cost, rel=1e-9)


def test_solver_riccati_beats_brute_force_grid() -> None:
    prob = _scalar_problem(3)
    sol = pp.solve_lq_optimal_control(prob)
    grid = np.linspace(-3.0, 3.0, 41)  # 41^3 ~ 69k combos
    best, _seq = pp.brute_force_lq_cost(prob, control_grid=grid)
    # The continuous Riccati optimum must not be worse than any grid sequence.
    assert sol.total_cost <= best + 1e-9


def test_solver_costates_relation() -> None:
    sol = pp.solve_lq_optimal_control(_scalar_problem(3))
    for k in range(sol.states.shape[0]):
        expected = 2.0 * sol.riccati[k] @ sol.states[k]
        assert np.allclose(sol.costates[k], expected)


def test_solver_gain_matches_riccati_formula() -> None:
    prob = _scalar_problem(2)
    sol = pp.solve_lq_optimal_control(prob)
    for k in range(prob.horizon):
        p_next = sol.riccati[k + 1]
        btp = prob.B.T @ p_next
        s = prob.R + btp @ prob.B
        expected = np.linalg.solve(s, btp @ prob.A)
        assert np.allclose(sol.gains[k], expected)


def test_solver_drives_state_toward_zero() -> None:
    sol = pp.solve_lq_optimal_control(_scalar_problem(8))
    assert abs(float(sol.states[-1][0])) < abs(float(sol.states[0][0]))


def test_solver_handles_zero_initial_state() -> None:
    prob = pp.LQProblem(
        A=np.eye(2),
        B=np.ones((2, 1)),
        Q=np.eye(2),
        R=np.eye(1),
        Qf=np.eye(2),
        x0=np.zeros(2),
        horizon=3,
    )
    sol = pp.solve_lq_optimal_control(prob)
    assert sol.total_cost == pytest.approx(0.0)
    assert np.allclose(sol.controls, 0.0)


def test_solver_multivariate() -> None:
    prob = pp.LQProblem(
        A=np.eye(2),
        B=np.array([[1.0], [0.5]]),
        Q=np.eye(2),
        R=np.eye(1),
        Qf=np.eye(2),
        x0=np.array([1.0, -1.0]),
        horizon=5,
    )
    sol = pp.solve_lq_optimal_control(prob)
    assert sol.gains.shape == (5, 1, 2)
    assert sol.total_cost > 0.0


def test_solver_deterministic() -> None:
    a = pp.solve_lq_optimal_control(_scalar_problem(5))
    b = pp.solve_lq_optimal_control(_scalar_problem(5))
    assert np.array_equal(a.gains, b.gains)
    assert np.array_equal(a.controls, b.controls)


# --- brute-force oracle guards ---------------------------------------------------


def test_brute_force_rejects_multivariate() -> None:
    prob = pp.LQProblem(
        A=np.eye(2),
        B=np.ones((2, 2)),
        Q=np.eye(2),
        R=np.eye(2),
        Qf=np.eye(2),
        x0=np.ones(2),
        horizon=2,
    )
    with pytest.raises(pp.PacingError):
        pp.brute_force_lq_cost(prob, control_grid=np.linspace(-1, 1, 5))


def test_brute_force_rejects_bad_grid() -> None:
    with pytest.raises(pp.PacingError):
        pp.brute_force_lq_cost(_scalar_problem(2), control_grid=np.array([]))


def test_brute_force_rejects_too_large_search() -> None:
    with pytest.raises(pp.PacingError):
        pp.brute_force_lq_cost(_scalar_problem(10), control_grid=np.linspace(-5, 5, 200))


def test_brute_force_matches_riccati_on_zero_state() -> None:
    prob = pp.LQProblem(
        A=np.array([[1.0]]),
        B=np.array([[1.0]]),
        Q=np.array([[1.0]]),
        R=np.array([[1.0]]),
        Qf=np.array([[1.0]]),
        x0=np.array([0.0]),
        horizon=3,
    )
    sol = pp.solve_lq_optimal_control(prob)
    best, _ = pp.brute_force_lq_cost(prob, control_grid=np.linspace(-2, 2, 21))
    assert sol.total_cost == pytest.approx(0.0, abs=1e-12)
    assert best == pytest.approx(0.0, abs=1e-9)


# --- pacing plan -----------------------------------------------------------------


def test_pacing_plan_monotone_in_control() -> None:
    plan = pp.pacing_plan_from_control(np.array([-1.0, 0.0, 1.0]))
    # Higher control -> shorter duration (faster pacing).
    assert plan.durations[0] > plan.durations[1] > plan.durations[2]


def test_pacing_plan_respects_bounds() -> None:
    plan = pp.pacing_plan_from_control(
        np.array([-100.0, 100.0]), min_duration_s=0.5, max_duration_s=20.0
    )
    assert plan.durations[0] == pytest.approx(20.0)
    assert plan.durations[1] == pytest.approx(0.5)


def test_pacing_plan_cut_rate_is_inverse_duration() -> None:
    plan = pp.pacing_plan_from_control(np.array([0.5, -0.5]))
    assert np.allclose(plan.cut_rates, 1.0 / plan.durations)


def test_pacing_plan_total_duration() -> None:
    plan = pp.pacing_plan_from_control(np.array([0.0, 0.0]), base_duration_s=4.0)
    assert plan.total_duration == pytest.approx(8.0)


def test_pacing_plan_rejects_bad_controls() -> None:
    with pytest.raises(pp.PacingError):
        pp.pacing_plan_from_control(np.array([]))
    with pytest.raises(pp.PacingError):
        pp.pacing_plan_from_control(np.array([np.nan]))


def test_pacing_plan_rejects_bad_bounds() -> None:
    with pytest.raises(pp.PacingError):
        pp.pacing_plan_from_control(np.array([0.0]), min_duration_s=0.0)
    with pytest.raises(pp.PacingError):
        pp.pacing_plan_from_control(np.array([0.0]), min_duration_s=5.0, max_duration_s=1.0)
    with pytest.raises(pp.PacingError):
        pp.pacing_plan_from_control(np.array([0.0]), base_duration_s=0.0)


def test_problem_rejects_bad_B_ndim() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones(2),
            Q=np.eye(2),
            R=np.eye(1),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_zero_control_dim() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 0)),
            Q=np.eye(2),
            R=np.eye(0),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_R_shape() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 1)),
            Q=np.eye(2),
            R=np.eye(2),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_asymmetric_R() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(2),
            B=np.ones((2, 2)),
            Q=np.eye(2),
            R=np.array([[1.0, 0.5], [0.0, 1.0]]),
            Qf=np.eye(2),
            x0=np.zeros(2),
            horizon=2,
        )


def test_problem_rejects_state_dim_over_budget() -> None:
    n = pp.MAX_STATE_DIM + 1
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(n),
            B=np.ones((n, 1)),
            Q=np.eye(n),
            R=np.eye(1),
            Qf=np.eye(n),
            x0=np.zeros(n),
            horizon=2,
        )


def test_problem_rejects_non_psd_Qf() -> None:
    with pytest.raises(pp.PacingError):
        pp.LQProblem(
            A=np.eye(1),
            B=np.ones((1, 1)),
            Q=np.eye(1),
            R=np.eye(1),
            Qf=np.array([[-1.0]]),
            x0=np.zeros(1),
            horizon=2,
        )
