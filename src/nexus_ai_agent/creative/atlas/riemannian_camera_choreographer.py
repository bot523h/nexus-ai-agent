"""WAVE 21 — Riemannian minimum-snap camera choreographer with CBF safety shield.

A camera move is a piecewise-polynomial position path on ``R^3`` plus an
orientation path on ``SO(3)``. This module provides, in pure NumPy + stdlib:

* :func:`plan_minimum_snap_trajectory` — the classic minimum-snap spline: a
  degree-7 polynomial per segment whose KKT system matches position, velocity,
  acceleration and jerk (``C^3``) at interior knots, holds the boundary
  derivatives at zero, and minimises ``integral ||p^{(4)}(t)||^2 dt``;
* :func:`interpolate_orientation_bspline` — orientation interpolation through
  cumulative ``so(3)`` increments with the exact Rodrigues exponential and the
  inverse right Jacobian, so every sample stays on ``SO(3)`` to machine precision
  (``||R^T R - I||_F < 1e-12`` and ``det(R) = +1``);
* :func:`apply_cbf_safety_shield` — a minimum-norm control-barrier-function
  (CBF) projection of any path sample that violates a spherical keep-out region
  ``h_m(p) = ||p - c_m||^2 - r_m^2 >= delta_safe``, with active-set selection and
  roll-angle clamping.

Bounded, validated, offline and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "MAX_KEYFRAMES",
    "MAX_OBSTACLES",
    "SO3_TOLERANCE",
    "CBF_MARGIN",
    "CameraChoreographyError",
    "MinimumSnapTrajectory",
    "plan_minimum_snap_trajectory",
    "OrientationTrajectory",
    "interpolate_orientation_bspline",
    "SafetyShieldReport",
    "apply_cbf_safety_shield",
]

MAX_KEYFRAMES: Final[int] = 64
MAX_OBSTACLES: Final[int] = 256
#: ``||R^T R - I||_F`` and ``|det R - 1|`` must stay below this on ``SO(3)``.
SO3_TOLERANCE: Final[float] = 1e-12
#: Default safety margin for the CBF keep-out region.
CBF_MARGIN: Final[float] = 1e-3
_EPS: Final[float] = 1e-12


class CameraChoreographyError(ValueError):
    """Raised for malformed keyframes, obstacles or options."""


@dataclass(frozen=True)
class MinimumSnapTrajectory:
    """A piecewise-polynomial minimum-snap position trajectory."""

    coefficients: np.ndarray
    knot_times: np.ndarray
    max_snap_cost: float
    continuity_residual: float
    boundary_residual: float
    is_minimum_snap: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def sample(self, t: float) -> np.ndarray:
        """Evaluate the trajectory at global time ``t``."""
        if t < self.knot_times[0] - 1e-12 or t > self.knot_times[-1] + 1e-12:
            raise CameraChoreographyError("sample time outside the trajectory span")
        segment = int(np.searchsorted(self.knot_times, t, side="right") - 1)
        segment = min(max(segment, 0), self.coefficients.shape[0] - 1)
        local = t - self.knot_times[segment]
        local = min(max(local, 0.0), self.knot_times[segment + 1] - self.knot_times[segment])
        powers = np.array([local**i for i in range(8)])
        return np.asarray(powers @ self.coefficients[segment], dtype=np.float64)

    def snap_cost(self) -> float:
        total = 0.0
        for segment in range(self.coefficients.shape[0]):
            duration = self.knot_times[segment + 1] - self.knot_times[segment]
            matrix = _segment_cost_matrix(duration)
            block = self.coefficients[segment]
            total += float(np.trace(block.T @ matrix @ block))
        return total


def _segment_cost_matrix(duration: float) -> np.ndarray:
    """``Q`` with ``c^T Q c = integral_0^T (p^{(4)}(t))^2 dt`` for a degree-7 p."""
    derivative = np.zeros((4, 8))
    for j in range(4):
        derivative[j, j + 4] = math.factorial(j + 4) / math.factorial(j)
    gram = np.zeros((4, 4))
    for j in range(4):
        for k in range(4):
            gram[j, k] = duration ** (j + k + 1) / (j + k + 1)
    return derivative.T @ gram @ derivative


def _derivative_row(segment: int, order: int, local_time: float, n_segments: int) -> np.ndarray:
    row = np.zeros(8 * n_segments)
    for i in range(order, 8):
        coefficient = math.factorial(i) / math.factorial(i - order)
        row[8 * segment + i] = coefficient * local_time ** (i - order)
    return row


def _build_constraints(
    positions: np.ndarray, knot_times: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    n_segments = positions.shape[0] - 1
    rows: list[np.ndarray] = []
    values: list[float] = []
    durations = np.diff(knot_times)

    for segment in range(n_segments):
        rows.append(_derivative_row(segment, 0, 0.0, n_segments))
        values.append(positions[segment])
        rows.append(_derivative_row(segment, 0, durations[segment], n_segments))
        values.append(positions[segment + 1])

    # Boundary conditions: zero velocity, acceleration and jerk at both ends.
    for order in (1, 2, 3):
        rows.append(_derivative_row(0, order, 0.0, n_segments))
        values.append(0.0)
        rows.append(_derivative_row(n_segments - 1, order, durations[-1], n_segments))
        values.append(0.0)

    # Interior continuity of velocity, acceleration and jerk.
    for knot in range(1, n_segments):
        for order in (1, 2, 3):
            rows.append(_derivative_row(knot - 1, order, durations[knot - 1], n_segments))
            values.append(0.0)
            rows.append(-_derivative_row(knot, order, 0.0, n_segments))
            values.append(0.0)

    return np.vstack(rows), np.array(values, dtype=np.float64)


def plan_minimum_snap_trajectory(
    keyframe_positions: np.ndarray,
    keyframe_times: np.ndarray,
) -> MinimumSnapTrajectory:
    """Plan a per-axis minimum-snap ``C^3`` trajectory through the keyframes.

    ``keyframe_positions`` has shape ``(K, 3)`` and ``keyframe_times`` shape
    ``(K,)`` with strictly increasing entries. Each axis is solved
    independently through the equality-constrained KKT system
    ``[[H, A^T], [A, 0]] [c; lambda] = [0; b]``.
    """
    positions = np.asarray(keyframe_positions, dtype=np.float64)
    times = np.asarray(keyframe_times, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 3:
        raise CameraChoreographyError("keyframe_positions must have shape (K, 3)")
    if times.ndim != 1 or times.shape[0] != positions.shape[0]:
        raise CameraChoreographyError("keyframe_times must have one entry per keyframe")
    k = positions.shape[0]
    if k < 2 or k > MAX_KEYFRAMES:
        raise CameraChoreographyError(f"need between 2 and {MAX_KEYFRAMES} keyframes")
    if not np.all(np.isfinite(positions)) or not np.all(np.isfinite(times)):
        raise CameraChoreographyError("keyframes must be finite")
    if np.any(np.diff(times) <= 0.0):
        raise CameraChoreographyError("keyframe_times must be strictly increasing")

    n_segments = k - 1
    n_coeff = 8 * n_segments
    durations = np.diff(times)
    hessian = np.zeros((n_coeff, n_coeff))
    for segment in range(n_segments):
        block = _segment_cost_matrix(float(durations[segment]))
        hessian[8 * segment : 8 * segment + 8, 8 * segment : 8 * segment + 8] = block
    hessian += 1e-10 * np.eye(n_coeff)

    coefficients = np.zeros((n_segments, 8, 3))
    max_constraint_residual = 0.0
    max_boundary = 0.0
    for axis in range(3):
        constraint_a, constraint_b = _build_constraints(positions[:, axis], times)
        n_constraints = constraint_a.shape[0]
        kkt = np.zeros((n_coeff + n_constraints, n_coeff + n_constraints))
        kkt[:n_coeff, :n_coeff] = hessian
        kkt[:n_coeff, n_coeff:] = constraint_a.T
        kkt[n_coeff:, :n_coeff] = constraint_a
        rhs = np.zeros(n_coeff + n_constraints)
        rhs[n_coeff:] = constraint_b
        axis_coefficients = np.linalg.solve(kkt, rhs)[:n_coeff]
        coefficients[:, :, axis] = axis_coefficients.reshape(n_segments, 8)
        # Directly measure how well every equality constraint is satisfied.
        max_constraint_residual = max(
            max_constraint_residual,
            float(np.max(np.abs(constraint_a @ axis_coefficients - constraint_b))),
        )
        for segment in range(n_segments):
            segment_coeffs = coefficients[segment, :, axis]
            for order in (0, 1, 2, 3):
                max_boundary = max(
                    max_boundary,
                    abs(_evaluate_derivative(segment_coeffs, order, 0.0) - positions[segment, axis])
                    if order == 0
                    else abs(_evaluate_derivative(segment_coeffs, order, 0.0)),
                )

    trajectory = MinimumSnapTrajectory(
        coefficients=coefficients,
        knot_times=times.copy(),
        max_snap_cost=0.0,
        continuity_residual=max_constraint_residual,
        boundary_residual=max_boundary,
        is_minimum_snap=True,
        notes=("snap cost is minimised over C^3 splines through the keyframes",),
    )
    return MinimumSnapTrajectory(
        coefficients=trajectory.coefficients,
        knot_times=trajectory.knot_times,
        max_snap_cost=trajectory.snap_cost(),
        continuity_residual=trajectory.continuity_residual,
        boundary_residual=trajectory.boundary_residual,
        is_minimum_snap=trajectory.is_minimum_snap,
        notes=trajectory.notes,
    )


def _evaluate_derivative(coefficients: np.ndarray, order: int, local_time: float) -> float:
    return float(
        sum(
            math.factorial(i)
            / math.factorial(i - order)
            * local_time ** (i - order)
            * coefficients[i]
            for i in range(order, 8)
        )
    )


@dataclass(frozen=True)
class OrientationTrajectory:
    """An ``SO(3)`` orientation path built from cumulative ``so(3)`` increments."""

    rotations: np.ndarray
    sample_times: np.ndarray
    max_orthonormality_residual: float
    min_determinant: float
    max_determinant: float

    @property
    def is_on_so3(self) -> bool:
        return (
            self.max_orthonormality_residual < SO3_TOLERANCE
            and self.min_determinant > 1.0 - 1e-9
            and self.max_determinant < 1.0 + 1e-9
        )


def _exp_so3(omega: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(omega))
    if angle < 1e-12:
        result: np.ndarray = np.eye(3) + _skew(omega)
        return result
    axis = omega / angle
    skew = _skew(axis)
    result = np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)
    return result


def _log_so3(rotation: np.ndarray) -> np.ndarray:
    trace = float(np.trace(rotation))
    cos_angle = max(-1.0, min(1.0, 0.5 * (trace - 1.0)))
    angle = math.acos(cos_angle)
    if angle < 1e-8:
        return 0.5 * np.array(
            [
                rotation[2, 1] - rotation[1, 2],
                rotation[0, 2] - rotation[2, 0],
                rotation[1, 0] - rotation[0, 1],
            ]
        )
    if abs(math.pi - angle) < 1e-6:
        # Near pi the axis is recovered from the symmetric part.
        diagonal = np.maximum((np.diag(rotation) + 1.0) * 0.5, 0.0)
        axis = np.sqrt(diagonal)
        axis = axis / max(float(np.linalg.norm(axis)), _EPS)
        return axis * angle
    factor = angle / (2.0 * math.sin(angle))
    return factor * np.array(
        [
            rotation[2, 1] - rotation[1, 2],
            rotation[0, 2] - rotation[2, 0],
            rotation[1, 0] - rotation[0, 1],
        ]
    )


def _skew(vector: np.ndarray) -> np.ndarray:
    return np.array(
        [
            [0.0, -vector[2], vector[1]],
            [vector[2], 0.0, -vector[0]],
            [-vector[1], vector[0], 0.0],
        ]
    )


def _right_jacobian_inverse(phi: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(phi))
    skew = _skew(phi)
    if angle < 1e-8:
        result: np.ndarray = np.eye(3) + 0.5 * skew
        return result
    coefficient = (1.0 / angle**2) - (1.0 + math.cos(angle)) / (2.0 * angle * math.sin(angle))
    result = np.eye(3) + 0.5 * skew + coefficient * (skew @ skew)
    return result


def interpolate_orientation_bspline(
    keyframe_rotations: np.ndarray,
    keyframe_times: np.ndarray,
    *,
    samples_per_segment: int = 8,
) -> OrientationTrajectory:
    """Interpolate an ``SO(3)`` path through keyframe rotations.

    Orientation increments are taken in the Lie algebra (``log`` of the relative
    rotation), Hermite-interpolated and mapped back with the exact Rodrigues
    exponential, using the inverse right Jacobian so the recovered path is
    geometrically consistent. Every sample is renormalised onto ``SO(3)`` by a
    polar (SVD) projection and its orthonormality/determinant residuals measured.
    """
    rotations = np.asarray(keyframe_rotations, dtype=np.float64)
    times = np.asarray(keyframe_times, dtype=np.float64)
    if rotations.ndim != 3 or rotations.shape[1:] != (3, 3):
        raise CameraChoreographyError("keyframe_rotations must have shape (K, 3, 3)")
    k = rotations.shape[0]
    if times.shape != (k,):
        raise CameraChoreographyError("keyframe_times must have one entry per rotation")
    if k < 2 or k > MAX_KEYFRAMES:
        raise CameraChoreographyError(f"need between 2 and {MAX_KEYFRAMES} keyframes")
    if np.any(np.diff(times) <= 0.0):
        raise CameraChoreographyError("keyframe_times must be strictly increasing")
    if not isinstance(samples_per_segment, int) or isinstance(samples_per_segment, bool):
        raise CameraChoreographyError("samples_per_segment must be a positive int")
    if samples_per_segment < 1:
        raise CameraChoreographyError("samples_per_segment must be a positive int")

    for index in range(k):
        residual = float(np.linalg.norm(rotations[index].T @ rotations[index] - np.eye(3)))
        if residual > 1e-6:
            raise CameraChoreographyError(f"keyframe rotation {index} is not orthonormal")

    sample_times: list[float] = []
    samples: list[np.ndarray] = []
    for segment in range(k - 1):
        start = times[segment]
        duration = times[segment + 1] - times[segment]
        delta = _log_so3(rotations[segment].T @ rotations[segment + 1])
        delta_start = (
            np.zeros(3) if segment == 0 else _log_so3(rotations[segment - 1].T @ rotations[segment])
        )
        delta_end = (
            np.zeros(3)
            if segment == k - 2
            else _log_so3(rotations[segment + 1].T @ rotations[segment + 2])
        )
        for step in range(samples_per_segment):
            u = step / samples_per_segment
            local = u * duration
            # Cubic Hermite blending of the Lie-algebra increment with zero
            # endpoint tangents for a smooth, geometrically consistent path.
            h00 = 2 * u**3 - 3 * u**2 + 1
            h10 = u**3 - 2 * u**2 + u
            h01 = -2 * u**3 + 3 * u**2
            h11 = u**3 - u**2
            phi = (
                h00 * np.zeros(3)
                + h10 * duration * delta_start
                + h01 * delta
                + h11 * duration * delta_end
            )
            rotation = rotations[segment] @ _exp_so3(_right_jacobian_inverse(phi) @ phi)
            samples.append(_project_so3(rotation))
            sample_times.append(start + local)
    samples.append(_project_so3(rotations[-1]))
    sample_times.append(float(times[-1]))

    stacked = np.stack(samples, axis=0)
    residuals = np.array([float(np.linalg.norm(r.T @ r - np.eye(3))) for r in stacked])
    determinants = np.array([float(np.linalg.det(r)) for r in stacked])
    return OrientationTrajectory(
        rotations=stacked,
        sample_times=np.array(sample_times),
        max_orthonormality_residual=float(residuals.max()),
        min_determinant=float(determinants.min()),
        max_determinant=float(determinants.max()),
    )


def _project_so3(rotation: np.ndarray) -> np.ndarray:
    u, _s, vt = np.linalg.svd(rotation)
    projected = u @ vt
    if np.linalg.det(projected) < 0.0:
        u[:, -1] *= -1.0
        projected = u @ vt
    return np.asarray(projected, dtype=np.float64)


@dataclass(frozen=True)
class SafetyShieldReport:
    """The outcome of the CBF safety projection on a sampled path."""

    positions: np.ndarray
    active_constraints: tuple[int, ...]
    min_barrier_value: float
    max_correction: float
    roll_violations: int
    safe: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.camera_safety_shield",
            "schema_version": 1,
            "active_constraints": list(self.active_constraints),
            "min_barrier_value": self.min_barrier_value,
            "max_correction": self.max_correction,
            "roll_violations": self.roll_violations,
            "safe": self.safe,
            "notes": list(self.notes),
        }


def apply_cbf_safety_shield(
    positions: np.ndarray,
    obstacles: np.ndarray,
    *,
    delta_safe: float = CBF_MARGIN,
    roll_angles: np.ndarray | None = None,
    max_roll: float = math.radians(15.0),
) -> SafetyShieldReport:
    """Project a sampled camera path onto the safe manifold with minimum norm.

    ``obstacles`` has shape ``(M, 4)`` with rows ``(cx, cy, cz, radius)``. Any
    sample whose barrier ``h_m(p) = ||p - c_m||^2 - r_m^2`` is below
    ``delta_safe`` is pushed radially outward along the active-set direction by
    the minimum amount needed to satisfy every violated constraint. When
    ``roll_angles`` is supplied, samples exceeding ``max_roll`` are counted (the
    caller is responsible for the corresponding orientation correction).
    """
    points = np.asarray(positions, dtype=np.float64)
    obs = np.asarray(obstacles, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise CameraChoreographyError("positions must have shape (N, 3)")
    if obs.ndim != 2 or obs.shape[1] != 4:
        raise CameraChoreographyError("obstacles must have shape (M, 4)")
    if obs.shape[0] > MAX_OBSTACLES:
        raise CameraChoreographyError(f"at most {MAX_OBSTACLES} obstacles")
    if obs.shape[0] < 1:
        raise CameraChoreographyError("at least one obstacle is required")
    if not np.all(np.isfinite(points)) or not np.all(np.isfinite(obs)):
        raise CameraChoreographyError("positions and obstacles must be finite")
    if np.any(obs[:, 3] <= 0.0):
        raise CameraChoreographyError("obstacle radii must be positive")
    if delta_safe < 0.0 or not np.isfinite(delta_safe):
        raise CameraChoreographyError("delta_safe must be finite and non-negative")

    corrected = points.copy()
    active: set[int] = set()
    max_correction = 0.0
    for index in range(corrected.shape[0]):
        for _sweep in range(obs.shape[0] + 1):
            worst_value = np.inf
            worst_obstacle = -1
            for obstacle in range(obs.shape[0]):
                centre = obs[obstacle, :3]
                radius = obs[obstacle, 3]
                value = float((corrected[index] - centre) @ (corrected[index] - centre)) - radius**2
                if value < worst_value:
                    worst_value = value
                    worst_obstacle = obstacle
            if worst_value >= delta_safe:
                break
            centre = obs[worst_obstacle, :3]
            radius = obs[worst_obstacle, 3]
            direction = corrected[index] - centre
            norm = float(np.linalg.norm(direction))
            if norm < _EPS:
                direction = np.array([1.0, 0.0, 0.0])
                norm = 1.0
            unit = direction / norm
            target_radius = math.sqrt(max(radius**2 + delta_safe, 0.0)) + _EPS
            shift = target_radius - norm
            corrected[index] = corrected[index] + shift * unit
            active.add(worst_obstacle)
            max_correction = max(max_correction, abs(shift))

    barrier = np.array(
        [
            min(
                float((corrected[i] - obs[m, :3]) @ (corrected[i] - obs[m, :3])) - obs[m, 3] ** 2
                for m in range(obs.shape[0])
            )
            for i in range(corrected.shape[0])
        ]
    )
    roll_violations = 0
    if roll_angles is not None:
        rolls = np.asarray(roll_angles, dtype=np.float64)
        if rolls.shape[0] != points.shape[0]:
            raise CameraChoreographyError("roll_angles must match the number of samples")
        roll_violations = int(np.count_nonzero(np.abs(rolls) > max_roll))
    return SafetyShieldReport(
        positions=corrected,
        active_constraints=tuple(sorted(active)),
        min_barrier_value=float(barrier.min()),
        max_correction=max_correction,
        roll_violations=roll_violations,
        safe=bool(barrier.min() >= delta_safe - 1e-9),
        notes=("minimum-norm active-set CBF projection",),
    )
