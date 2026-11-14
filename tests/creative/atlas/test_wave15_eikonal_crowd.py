"""WAVE 15 — tests for Eikonal navigation, social-force crowd healing and
cross-cut topological continuity."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import eikonal_crowd_social_force as ec


def _point_goal_cost(n: int = 33) -> tuple[np.ndarray, np.ndarray]:
    cost = np.ones((n, n))
    goal = np.zeros((n, n), bool)
    goal[n // 2, n // 2] = True
    return cost, goal


# --- eikonal ---------------------------------------------------------------------


def test_eikonal_axis_value_is_exact() -> None:
    cost, goal = _point_goal_cost(33)
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    # Straight horizontal distance from the goal to the left edge is exactly 16.
    assert float(ef.potential[16, 0]) == pytest.approx(16.0, abs=1e-6)
    assert float(ef.potential[0, 16]) == pytest.approx(16.0, abs=1e-6)


def test_eikonal_converges_and_reports_sweeps() -> None:
    cost, goal = _point_goal_cost(21)
    ef = ec.solve_eikonal_navigation_potential(cost, goal, max_sweeps=32)
    assert ef.converged
    assert ef.sweeps >= 1


def test_eikonal_goal_cells_are_zero() -> None:
    cost, goal = _point_goal_cost(21)
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    assert float(ef.potential[10, 10]) == 0.0


def test_eikonal_is_monotone_along_axis() -> None:
    cost, goal = _point_goal_cost(33)
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    row = ef.potential[16, 16:]
    assert np.all(np.diff(row) >= -1e-9)


def test_eikonal_heading_points_to_goal() -> None:
    cost, goal = _point_goal_cost(33)
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    # At (16, 0) the descent direction is +x (towards the goal at x=16).
    assert ef.heading[16, 0, 0] > 0.9
    assert abs(float(ef.heading[16, 0, 1])) < 1e-6


def test_eikonal_descent_reaches_goal_from_corners() -> None:
    cost, goal = _point_goal_cost(33)
    ef = ec.solve_eikonal_navigation_potential(cost, goal)

    def walk(start: tuple[int, int]) -> bool:
        y, x = start
        for _ in range(400):
            if (y, x) == (16, 16):
                return True
            dx, dy = ef.heading[y, x]
            ny = min(max(int(round(y + dy * 1.5)), 0), 32)
            nx = min(max(int(round(x + dx * 1.5)), 0), 32)
            if (ny, nx) == (y, x):
                return False
            y, x = ny, nx
        return False

    assert walk((0, 0))
    assert walk((32, 32))
    assert walk((32, 0))


def test_eikonal_higher_cost_slows_front() -> None:
    n = 33
    cost = np.ones((n, n))
    cost[:, 20:] = 5.0  # a slow region on the right
    goal = np.zeros((n, n), bool)
    goal[16, 16] = True
    ef = ec.solve_eikonal_navigation_potential(cost, goal)
    # Reaching the right edge crosses the slow region -> larger potential.
    assert float(ef.potential[16, 32]) > 16.0


def test_eikonal_rejects_bad_cost_shape() -> None:
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(np.ones((1, 5)), np.ones((1, 5), bool))


def test_eikonal_rejects_nonpositive_cost() -> None:
    cost, goal = _point_goal_cost()
    cost[0, 0] = 0.0
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, goal)


def test_eikonal_rejects_nan_cost() -> None:
    cost, goal = _point_goal_cost()
    cost[0, 0] = np.nan
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, goal)


def test_eikonal_rejects_empty_goal() -> None:
    cost = np.ones((5, 5))
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, np.zeros((5, 5), bool))


def test_eikonal_rejects_goal_shape_mismatch() -> None:
    cost = np.ones((5, 5))
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, np.ones((4, 4), bool))


def test_eikonal_rejects_bad_sweeps_and_tol() -> None:
    cost, goal = _point_goal_cost(5)
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, goal, max_sweeps=0)
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(cost, goal, tol=0.0)


def test_eikonal_rejects_over_budget_grid() -> None:
    big = np.ones((ec.MAX_GRID_SIDE + 1, 4))
    goal = np.zeros(big.shape, bool)
    goal[0, 0] = True
    with pytest.raises(ec.CrowdError):
        ec.solve_eikonal_navigation_potential(big, goal)


# --- crowd simulation ------------------------------------------------------------


def _two_agents() -> tuple[np.ndarray, ...]:
    pos = np.array([[0.0, 0.0], [3.0, 0.0]])
    vel = np.array([[1.0, 0.0], [-1.0, 0.0]])
    rad = np.array([0.3, 0.3])
    goals = np.array([[3.0, 0.0], [0.0, 0.0]])
    return pos, vel, rad, goals


def test_crowd_no_overlap_two_agents() -> None:
    pos, vel, rad, goals = _two_agents()
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.05, steps=80)
    assert res.max_overlap <= 1e-6


def test_crowd_separation_at_least_sum_of_radii() -> None:
    pos, vel, rad, goals = _two_agents()
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.05, steps=80)
    # Minimum separation must respect r_i + r_j = 0.6 (allowing tiny numerical slack).
    assert res.min_separation >= 0.6 - 1e-6


def test_crowd_trajectory_shape() -> None:
    pos, vel, rad, goals = _two_agents()
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.1, steps=10)
    assert res.positions.shape == (11, 2, 2)
    assert res.velocities.shape == (11, 2, 2)


def test_crowd_agents_approach_goals() -> None:
    pos = np.array([[0.0, 0.0]])
    vel = np.zeros((1, 2))
    rad = np.array([0.3])
    goals = np.array([[5.0, 0.0]])
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.1, steps=200)
    assert res.positions[-1, 0, 0] > res.positions[0, 0, 0]


def test_crowd_wall_no_penetration() -> None:
    pos = np.array([[0.0, 0.0], [2.0, 0.0]])
    vel = np.array([[1.0, -1.0], [-1.0, -1.0]])
    rad = np.array([0.3, 0.3])
    goals = np.array([[0.0, 2.0], [2.0, 2.0]])
    walls = np.array([[0.0, 0.0, 2.0, 0.0]])  # a floor segment at y=0
    res = ec.simulate_and_heal_crowd_trajectories(
        pos, vel, rad, goals, dt=0.05, steps=60, walls=walls
    )
    # After the first (initial) frame, agents must not sink below the wall.
    assert float(res.positions[1:, :, 1].min()) >= 0.3 - 1e-6


def test_crowd_zero_steps_is_initial_state() -> None:
    pos, vel, rad, goals = _two_agents()
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.1, steps=0)
    assert np.allclose(res.positions[0], pos)


def test_crowd_rejects_bad_positions_shape() -> None:
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            np.zeros((2, 3)), np.zeros((2, 3)), np.array([0.3, 0.3]), np.zeros((2, 3))
        )


def test_crowd_rejects_radius_mismatch() -> None:
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            np.zeros((2, 2)), np.zeros((2, 2)), np.array([0.3]), np.zeros((2, 2))
        )


def test_crowd_rejects_nonpositive_radius() -> None:
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            np.zeros((2, 2)), np.zeros((2, 2)), np.array([0.3, 0.0]), np.zeros((2, 2))
        )


def test_crowd_rejects_nan_positions() -> None:
    pos = np.zeros((2, 2))
    pos[0, 0] = np.nan
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            pos, np.zeros((2, 2)), np.array([0.3, 0.3]), np.zeros((2, 2))
        )


def test_crowd_rejects_bad_dt_and_steps() -> None:
    pos, vel, rad, goals = _two_agents()
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.0)
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, steps=-1)


def test_crowd_rejects_bad_walls() -> None:
    pos, vel, rad, goals = _two_agents()
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, walls=np.zeros((0, 4)))


def test_crowd_single_agent_has_infinite_min_separation() -> None:
    res = ec.simulate_and_heal_crowd_trajectories(
        np.zeros((1, 2)),
        np.zeros((1, 2)),
        np.array([0.3]),
        np.array([[1.0, 0.0]]),
        steps=3,
    )
    assert res.min_separation == np.inf


# --- topology audit --------------------------------------------------------------


def test_topology_audit_consistent_motion() -> None:
    a = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    b = a + np.array([0.1, 0.0])
    aud = ec.audit_crowd_topological_continuity(a, b, dt=0.1, v_max=5.0)
    assert aud.consistent
    assert aud.speed_violations == 0
    assert aud.order_flips == 0


def test_topology_audit_detects_order_flip() -> None:
    a = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    b = np.array([[2.1, 0.0], [1.1, 0.0], [0.1, 0.0]])  # reversed order
    aud = ec.audit_crowd_topological_continuity(a, b, dt=0.1, v_max=50.0)
    assert not aud.consistent
    assert aud.order_flips > 0


def test_topology_audit_detects_speed_violation() -> None:
    a = np.array([[0.0, 0.0], [1.0, 0.0]])
    b = np.array([[100.0, 0.0], [1.1, 0.0]])
    aud = ec.audit_crowd_topological_continuity(a, b, dt=0.1, v_max=5.0)
    assert aud.speed_violations >= 1
    assert not aud.consistent


def test_topology_audit_detects_winding_flip() -> None:
    # Four points around a centre: swap two so the winding orientation flips.
    a = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0]])
    b = np.array([[0.0, 1.0], [1.0, 0.0], [-1.0, 0.0], [0.0, -1.0]])
    aud = ec.audit_crowd_topological_continuity(a, b, dt=0.1, v_max=50.0)
    assert aud.winding_flips > 0


def test_topology_audit_rejects_shape_mismatch() -> None:
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(np.zeros((2, 2)), np.zeros((3, 2)), dt=0.1, v_max=1.0)


def test_topology_audit_rejects_bad_dt_and_vmax() -> None:
    a = np.zeros((2, 2))
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(a, a, dt=0.0, v_max=1.0)
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(a, a, dt=0.1, v_max=0.0)


def test_topology_audit_rejects_nan() -> None:
    a = np.zeros((2, 2))
    b = a.copy()
    b[0, 0] = np.nan
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(a, b, dt=0.1, v_max=1.0)


def test_topology_audit_rejects_bad_camera_forward() -> None:
    a = np.zeros((2, 2))
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(
            a, a, dt=0.1, v_max=1.0, camera_forward=np.array([1.0, 0.0, 0.0])
        )


def test_topology_audit_reports_max_speed() -> None:
    a = np.array([[0.0, 0.0]])
    b = np.array([[1.0, 0.0]])
    aud = ec.audit_crowd_topological_continuity(a, b, dt=0.5, v_max=5.0)
    assert aud.max_speed == pytest.approx(2.0)


def test_eikonal_max_sweeps_one_reports_not_converged() -> None:
    cost = np.ones((41, 41))
    goal = np.zeros((41, 41), bool)
    goal[20, 20] = True
    ef = ec.solve_eikonal_navigation_potential(cost, goal, max_sweeps=1)
    assert not ef.converged
    assert ef.sweeps == 1


def test_crowd_rejects_too_many_agents() -> None:
    m = ec.MAX_AGENTS + 1
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            np.zeros((m, 2)), np.zeros((m, 2)), np.ones(m), np.zeros((m, 2))
        )


def test_crowd_rejects_goal_shape_mismatch() -> None:
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            np.zeros((2, 2)), np.zeros((2, 2)), np.array([0.3, 0.3]), np.zeros((3, 2))
        )


def test_crowd_rejects_bad_relax_time_and_speed() -> None:
    pos, vel, rad, goals = _two_agents()
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, relax_time=0.0)
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, desired_speed=0.0)


def test_crowd_rejects_nan_walls() -> None:
    pos, vel, rad, goals = _two_agents()
    with pytest.raises(ec.CrowdError):
        ec.simulate_and_heal_crowd_trajectories(
            pos, vel, rad, goals, walls=np.array([[0.0, 0.0, np.nan, 1.0]])
        )


def test_crowd_agent_at_goal_gets_no_drive() -> None:
    # dist_goal <= _EPS -> the goal term is skipped.
    res = ec.simulate_and_heal_crowd_trajectories(
        np.array([[1.0, 1.0]]),
        np.zeros((1, 2)),
        np.array([0.3]),
        np.array([[1.0, 1.0]]),
        dt=0.1,
        steps=5,
    )
    assert np.allclose(res.positions[-1], [[1.0, 1.0]])


def test_crowd_starting_overlap_is_healed() -> None:
    # Two agents started at the same point -> contact force + overlap resolution.
    pos = np.array([[0.0, 0.0], [0.0, 0.0]])
    vel = np.zeros((2, 2))
    rad = np.array([0.4, 0.4])
    goals = np.array([[1.0, 0.0], [-1.0, 0.0]])
    res = ec.simulate_and_heal_crowd_trajectories(pos, vel, rad, goals, dt=0.05, steps=20)
    assert res.max_overlap <= 1e-6


def test_crowd_degenerate_wall_is_ignored() -> None:
    pos, vel, rad, goals = _two_agents()
    res = ec.simulate_and_heal_crowd_trajectories(
        pos, vel, rad, goals, dt=0.05, steps=10, walls=np.array([[1.0, 1.0, 1.0, 1.0]])
    )
    assert res.positions.shape[0] == 11


def test_topology_audit_rejects_empty_positions() -> None:
    with pytest.raises(ec.CrowdError):
        ec.audit_crowd_topological_continuity(np.zeros((0, 2)), np.zeros((0, 2)), dt=0.1, v_max=1.0)


def test_resolve_overlaps_handles_exact_coincidence() -> None:
    # Two agents exactly at the same point: the resolver must pick a direction.
    pos = np.array([[1.0, 1.0], [1.0, 1.0]])
    out = ec._resolve_overlaps(pos, np.array([0.4, 0.4]))
    assert float(np.linalg.norm(out[0] - out[1])) >= 0.8 - 1e-9


def test_resolve_overlaps_breaks_early_when_disjoint() -> None:
    pos = np.array([[0.0, 0.0], [10.0, 0.0]])
    out = ec._resolve_overlaps(pos, np.array([0.4, 0.4]))
    assert np.allclose(out, pos)


def test_resolve_overlaps_exhausts_iterations_when_dense() -> None:
    # Many coincident large agents cannot fully separate within 64 iterations,
    # so the loop exhausts (no early break) and still returns a valid array.
    n = 40
    pos = np.zeros((n, 2))
    out = ec._resolve_overlaps(pos, np.full(n, 10.0))
    assert out.shape == (n, 2)
    assert np.all(np.isfinite(out))
