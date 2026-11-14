"""WAVE 15 — fast-sweeping Eikonal navigation, Helbing social-force crowd
mechanics and cross-cut topological continuity.

Background extras in AI films walk through walls, interpenetrate, or swap their
relative positions across cuts. This module provides, in pure NumPy + stdlib:

* :func:`solve_eikonal_navigation_potential` — solves ``|grad U| = C(x, y)`` with
  ``U|_G = 0`` by alternating Gauss–Seidel sweeps (``++``, ``+-``, ``-+``, ``--``)
  with upwind Godunov finite differences, yielding a global geodesic heading
  field ``v_des = -grad U / ||grad U||`` free of local minima;
* :func:`simulate_and_heal_crowd_trajectories` — the Helbing–Molnár multi-agent
  social-force integrator
  ``m_i dv_i/dt = m_i (v0 e_i^0 − v_i)/tau + Σ_j f_ij^soc + Σ_W f_iW^wall``
  with position-based contact projection enforcing ``||x_i − x_j|| >= r_i + r_j``
  (zero overlap) and zero wall penetration every frame;
* :func:`audit_crowd_topological_continuity` — verifies that pairwise winding
  angles and left-to-right image order survive a cut consistent with the finite
  physical speed ``||x_i(t_{k+1}) − x_i(t_k)|| <= v_max dt``.

Bounded, validated, offline and deterministic; malformed inputs raise
:class:`CrowdError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "MAX_GRID_SIDE",
    "MAX_AGENTS",
    "CrowdError",
    "EikonalField",
    "solve_eikonal_navigation_potential",
    "CrowdResult",
    "simulate_and_heal_crowd_trajectories",
    "TopologyAudit",
    "audit_crowd_topological_continuity",
]

MAX_GRID_SIDE: Final[int] = 2048
MAX_AGENTS: Final[int] = 4096
_EPS: Final[float] = 1e-12
_CONTACT_STIFFNESS: Final[float] = 50.0
_MAX_ACCEL: Final[float] = 50.0
_MAX_SPEED: Final[float] = 10.0


class CrowdError(ValueError):
    """Raised for malformed crowd/navigation inputs."""


@dataclass(frozen=True)
class EikonalField:
    """Solved navigation potential and its normalised geodesic heading field."""

    potential: np.ndarray  # (H, W), U with U|_G = 0
    heading: np.ndarray  # (H, W, 2), unit geodesic descent direction
    sweeps: int
    converged: bool


def solve_eikonal_navigation_potential(
    cost: np.ndarray,
    goal: np.ndarray,
    *,
    max_sweeps: int = 64,
    tol: float = 1e-9,
) -> EikonalField:
    """Solve ``|grad U| = C(x, y)`` with ``U|_G = 0`` by fast sweeping.

    ``cost`` is a strictly positive slowness field ``C >= c_min > 0``;
    ``goal`` is a boolean mask of zero-potential target cells. Alternating
    Gauss–Seidel sweeps in the four diagonal orders propagate the front
    globally; the iteration stops when the maximum update falls below ``tol``.
    Obstacles may be encoded as very high (but finite and positive) cost.
    """
    c = np.asarray(cost, dtype=np.float64)
    if c.ndim != 2 or min(c.shape) < 2:
        raise CrowdError("cost must be a 2-D grid at least 2x2")
    if max(c.shape) > MAX_GRID_SIDE:
        raise CrowdError("cost grid exceeds the bounded side budget")
    if not np.all(np.isfinite(c)):
        raise CrowdError("cost contains NaN/Inf")
    if np.any(c <= 0.0):
        raise CrowdError("cost must be strictly positive (c_min > 0)")
    g = np.asarray(goal, dtype=bool)
    if g.shape != c.shape:
        raise CrowdError("goal mask must match the cost grid shape")
    if not g.any():
        raise CrowdError("goal mask must contain at least one target cell")
    if max_sweeps < 1:
        raise CrowdError("max_sweeps must be positive")
    if tol <= 0.0:
        raise CrowdError("tol must be positive")

    h, w = c.shape
    big = 1e18
    u = np.full((h, w), big, dtype=np.float64)
    u[g] = 0.0
    converged = False
    sweeps = 0
    for sweep in range(max_sweeps):
        sweeps = sweep + 1
        max_delta = 0.0
        for y_range in (range(h), range(h - 1, -1, -1)):
            for x_range in (range(w), range(w - 1, -1, -1)):
                for y in y_range:
                    for x in x_range:
                        if g[y, x]:
                            continue
                        um = u[y, x - 1] if x > 0 else big
                        up = u[y, x + 1] if x < w - 1 else big
                        vm = u[y - 1, x] if y > 0 else big
                        vp = u[y + 1, x] if y < h - 1 else big
                        new = _solve_local_eikonal(um, up, vm, vp, c[y, x])
                        if new < u[y, x]:
                            delta = u[y, x] - new
                            if delta > max_delta:
                                max_delta = delta
                            u[y, x] = new
        if max_delta < tol:
            converged = True
            break

    # Heading field: descent direction of the potential (geodesic to the goal).
    gy, gx = np.gradient(u)
    norm = np.sqrt(gx * gx + gy * gy)
    norm[norm < _EPS] = 1.0
    heading = np.stack([-gx / norm, -gy / norm], axis=-1)
    heading[g] = 0.0
    return EikonalField(potential=u, heading=heading, sweeps=sweeps, converged=converged)


def _solve_local_eikonal(um: float, up: float, vm: float, vp: float, c: float) -> float:
    """Godunov upwind update for ``|grad U| = c``.

    The x-term is ``max(U - min(U_left, U_right), 0)^2`` and the y-term is
    ``max(U - min(U_up, U_down), 0)^2``; with ``a = min(U_left, U_right)`` and
    ``b = min(U_up, U_down)`` and ``a <= b`` the local quadratic
    ``(U-a)^2 + (U-b)^2 = c^2`` is solved when ``b - a < c``, else the
    one-sided ``U = a + c`` applies.
    """
    a = um if um < up else up
    b = vm if vm < vp else vp
    if a > b:
        a, b = b, a
    if a >= 1e17:
        return 1e18
    if b >= 1e17 or (b - a) >= c:
        return a + c
    return 0.5 * (a + b + float(np.sqrt(2.0 * c * c - (a - b) ** 2)))


@dataclass
class CrowdResult:
    """Simulated trajectories plus the measured minimum separation."""

    positions: np.ndarray  # (T+1, M, 2)
    velocities: np.ndarray  # (T+1, M, 2)
    min_separation: float
    max_overlap: float
    max_wall_penetration: float


@dataclass
class _Agent:
    pos: np.ndarray
    vel: np.ndarray
    radius: float
    goal: np.ndarray
    mass: float


def simulate_and_heal_crowd_trajectories(
    positions: np.ndarray,
    velocities: np.ndarray,
    radii: np.ndarray,
    goals: np.ndarray,
    *,
    dt: float = 0.1,
    steps: int = 100,
    desired_speed: float = 1.34,
    relax_time: float = 0.5,
    social_a: float = 2.0,
    social_b: float = 0.3,
    walls: np.ndarray | None = None,
) -> CrowdResult:
    """Integrate the Helbing–Molnár social-force model with contact healing.

    ``positions``/``velocities`` are ``(M, 2)``; ``radii`` is ``(M,)``;
    ``goals`` is ``(M, 2)``. ``walls`` (optional) is a set of axis-aligned
    segments ``(K, 4)`` = ``(x0, y0, x1, y1)`` the agents must not cross.
    After each integration step a position-based projection resolves any
    inter-agent overlap and wall penetration (zero overlap every frame).
    """
    pos = np.asarray(positions, dtype=np.float64)
    vel = np.asarray(velocities, dtype=np.float64)
    rad = np.asarray(radii, dtype=np.float64)
    gl = np.asarray(goals, dtype=np.float64)
    if pos.ndim != 2 or pos.shape[1] != 2:
        raise CrowdError("positions must be (M, 2)")
    m = pos.shape[0]
    if m < 1 or m > MAX_AGENTS:
        raise CrowdError(f"agent count must be in [1, {MAX_AGENTS}]")
    if vel.shape != pos.shape or gl.shape != pos.shape:
        raise CrowdError("velocities and goals must match positions shape")
    if rad.shape != (m,):
        raise CrowdError("radii must be (M,)")
    if not (np.all(np.isfinite(pos)) and np.all(np.isfinite(vel)) and np.all(np.isfinite(gl))):
        raise CrowdError("positions/velocities/goals contain NaN/Inf")
    if np.any(rad <= 0.0) or not np.all(np.isfinite(rad)):
        raise CrowdError("radii must be positive and finite")
    if dt <= 0.0:
        raise CrowdError("dt must be positive")
    if steps < 0:
        raise CrowdError("steps must be non-negative")
    if relax_time <= 0.0:
        raise CrowdError("relax_time must be positive")
    if desired_speed <= 0.0:
        raise CrowdError("desired_speed must be positive")

    wall_arr: np.ndarray | None = None
    if walls is not None:
        wall_arr = np.asarray(walls, dtype=np.float64)
        if wall_arr.ndim != 2 or wall_arr.shape[1] != 4 or wall_arr.shape[0] == 0:
            raise CrowdError("walls must be (K, 4) with K >= 1")
        if not np.all(np.isfinite(wall_arr)):
            raise CrowdError("walls contain NaN/Inf")

    traj = np.empty((steps + 1, m, 2))
    vtraj = np.empty((steps + 1, m, 2))
    traj[0] = pos
    vtraj[0] = vel
    cur_pos = pos.copy()
    cur_vel = vel.copy()
    min_sep = np.inf
    max_overlap = 0.0
    max_wall_pen = 0.0

    for step in range(steps):
        acc = np.zeros_like(cur_pos)
        for i in range(m):
            to_goal = gl[i] - cur_pos[i]
            dist_goal = float(np.linalg.norm(to_goal))
            if dist_goal > _EPS:
                e0 = to_goal / dist_goal
                acc[i] += (desired_speed * e0 - cur_vel[i]) / relax_time
            for j in range(m):
                if i == j:
                    continue
                d = cur_pos[i] - cur_pos[j]
                dist = float(np.linalg.norm(d))
                unit = d / dist if dist > _EPS else np.array([1.0, 0.0])
                gap = dist - (rad[i] + rad[j])
                if gap <= 0.0:
                    acc[i] += unit * _CONTACT_STIFFNESS * (-gap + 0.05)
                else:
                    acc[i] += unit * social_a * np.exp(-gap / social_b)
        if wall_arr is not None:
            acc += _wall_forces(cur_pos, rad, wall_arr)
        # Explicit-Euler stability: cap the acceleration and the speed.
        acc_norm = np.linalg.norm(acc, axis=1, keepdims=True)
        over_accel = acc_norm > _MAX_ACCEL
        acc = np.where(over_accel, acc * (_MAX_ACCEL / np.maximum(acc_norm, _EPS)), acc)
        cur_vel = cur_vel + acc * dt
        speed = np.linalg.norm(cur_vel, axis=1, keepdims=True)
        over_speed = speed > _MAX_SPEED
        cur_vel = np.where(over_speed, cur_vel * (_MAX_SPEED / np.maximum(speed, _EPS)), cur_vel)
        cur_pos = cur_pos + cur_vel * dt
        # Position-based healing: resolve overlaps and wall penetration.
        cur_pos = _resolve_overlaps(cur_pos, rad)
        if wall_arr is not None:
            cur_pos, pen = _resolve_walls(cur_pos, rad, wall_arr)
            max_wall_pen = max(max_wall_pen, pen)
        sep, overlap = _separation_stats(cur_pos, rad)
        min_sep = min(min_sep, sep)
        max_overlap = max(max_overlap, overlap)
        traj[step + 1] = cur_pos
        vtraj[step + 1] = cur_vel

    return CrowdResult(
        positions=traj,
        velocities=vtraj,
        min_separation=float(min_sep),
        max_overlap=float(max_overlap),
        max_wall_penetration=float(max_wall_pen),
    )


def _wall_forces(pos: np.ndarray, radii: np.ndarray, walls: np.ndarray) -> np.ndarray:
    acc = np.zeros_like(pos)
    for x0, y0, x1, y1 in walls:
        p0 = np.array([x0, y0])
        p1 = np.array([x1, y1])
        seg = p1 - p0
        seg_len2 = float(np.dot(seg, seg))
        for i in range(pos.shape[0]):
            t = (
                0.0
                if seg_len2 < _EPS
                else float(np.clip(np.dot(pos[i] - p0, seg) / seg_len2, 0.0, 1.0))
            )
            closest = p0 + t * seg
            d = pos[i] - closest
            dist = float(np.linalg.norm(d))
            gap = dist - radii[i]
            if dist < _EPS:
                d = np.array([0.0, 1.0])
                dist = _EPS
            if gap <= 0.0:
                acc[i] += (d / dist) * 200.0 * (-gap + 0.05)
            else:
                acc[i] += (d / dist) * 2.0 * np.exp(-gap / 0.3)
    return acc


def _resolve_overlaps(pos: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Iteratively push overlapping agents apart until separation >= r_i + r_j."""
    p = pos.copy()
    m = p.shape[0]
    for _ in range(64):
        moved = False
        for i in range(m):
            for j in range(i + 1, m):
                d = p[i] - p[j]
                dist = float(np.linalg.norm(d))
                need = radii[i] + radii[j]
                if dist < need - 1e-12:
                    if dist < _EPS:
                        d = np.array([1.0, 0.0])
                        dist = _EPS
                    push = (need - dist) * 0.5
                    p[i] += (d / dist) * push
                    p[j] -= (d / dist) * push
                    moved = True
        if not moved:
            break
    return p


def _resolve_walls(
    pos: np.ndarray, radii: np.ndarray, walls: np.ndarray
) -> tuple[np.ndarray, float]:
    p = pos.copy()
    max_pen = 0.0
    for x0, y0, x1, y1 in walls:
        p0 = np.array([x0, y0])
        p1 = np.array([x1, y1])
        seg = p1 - p0
        seg_len2 = float(np.dot(seg, seg))
        normal = np.array([-seg[1], seg[0]])
        nlen = float(np.linalg.norm(normal))
        if nlen < _EPS:
            continue
        normal = normal / nlen
        for i in range(p.shape[0]):
            t = (
                0.0
                if seg_len2 < _EPS
                else float(np.clip(np.dot(p[i] - p0, seg) / seg_len2, 0.0, 1.0))
            )
            closest = p0 + t * seg
            signed = float(np.dot(p[i] - closest, normal))
            pen = radii[i] - abs(signed)
            if pen > 1e-9:
                max_pen = max(max_pen, pen)
                p[i] += normal * np.sign(signed if abs(signed) > _EPS else 1.0) * pen
    return p, max_pen


def _separation_stats(pos: np.ndarray, radii: np.ndarray) -> tuple[float, float]:
    m = pos.shape[0]
    min_sep = np.inf
    max_overlap = 0.0
    for i in range(m):
        for j in range(i + 1, m):
            dist = float(np.linalg.norm(pos[i] - pos[j]))
            need = radii[i] + radii[j]
            min_sep = min(min_sep, dist)
            max_overlap = max(max_overlap, need - dist)
    if m == 1:
        min_sep = np.inf
    return min_sep, max_overlap


@dataclass
class TopologyAudit:
    """Result of the cross-cut topological-continuity audit."""

    consistent: bool
    speed_violations: int
    order_flips: int
    winding_flips: int
    max_speed: float
    details: list[str]


def audit_crowd_topological_continuity(
    positions_a: np.ndarray,
    positions_b: np.ndarray,
    *,
    dt: float,
    v_max: float,
    camera_forward: np.ndarray | None = None,
) -> TopologyAudit:
    """Audit topological continuity of a crowd between two cut frames.

    Checks (1) the finite-speed constraint
    ``||x_i(t_b) − x_i(t_a)|| <= v_max dt`` for every agent; (2) that the
    left-to-right image order (projected on ``camera_forward``'s right vector)
    is preserved; (3) that the pairwise winding angle sign is preserved for the
    crowd's orientation. Returns a typed report with counts and messages.
    """
    a = np.asarray(positions_a, dtype=np.float64)
    b = np.asarray(positions_b, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 2 or b.shape != a.shape:
        raise CrowdError("positions_a and positions_b must be matching (M, 2)")
    if a.shape[0] < 1:
        raise CrowdError("need at least one agent")
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise CrowdError("positions contain NaN/Inf")
    if dt <= 0.0:
        raise CrowdError("dt must be positive")
    if v_max <= 0.0:
        raise CrowdError("v_max must be positive")
    fwd = (
        np.array([0.0, 1.0])
        if camera_forward is None
        else np.asarray(camera_forward, dtype=np.float64)
    )
    if fwd.shape != (2,) or not np.all(np.isfinite(fwd)):
        raise CrowdError("camera_forward must be a finite 2-vector")

    details: list[str] = []
    speed_violations = 0
    max_speed = 0.0
    limit = v_max * dt
    for i in range(a.shape[0]):
        d = float(np.linalg.norm(b[i] - a[i]))
        max_speed = max(max_speed, d / dt)
        if d > limit + 1e-9:
            speed_violations += 1
            details.append(f"agent {i} moved {d:.4f} > v_max*dt={limit:.4f}")

    right = np.array([-fwd[1], fwd[0]])
    order_a = np.argsort(a @ right, kind="stable")
    order_b = np.argsort(b @ right, kind="stable")
    order_flips = int(np.sum(order_a != order_b))
    if order_flips:
        details.append(f"{order_flips} left-to-right order changes")

    winding_flips = 0
    centroid_a = a.mean(axis=0)
    centroid_b = b.mean(axis=0)
    for i in range(a.shape[0]):
        for j in range(i + 1, a.shape[0]):
            da_i, da_j = a[i] - centroid_a, a[j] - centroid_a
            db_i, db_j = b[i] - centroid_b, b[j] - centroid_b
            cross_a = float(da_i[0] * da_j[1] - da_i[1] * da_j[0])
            cross_b = float(db_i[0] * db_j[1] - db_i[1] * db_j[0])
            if cross_a * cross_b < 0.0:
                winding_flips += 1
    if winding_flips:
        details.append(f"{winding_flips} pairwise winding-angle sign flips")

    consistent = speed_violations == 0 and order_flips == 0 and winding_flips == 0
    return TopologyAudit(
        consistent=consistent,
        speed_violations=speed_violations,
        order_flips=order_flips,
        winding_flips=winding_flips,
        max_speed=max_speed,
        details=details,
    )
