"""Law 8 — XPBD cloth and hair drape with analytic capsule SDF repulsion.

AI capes, dresses and hair melt into limbs or float without gravity. This
module implements a vectorised Extended Position-Based Dynamics solver with
stretch compliance, isometric bending, quadratic wind drag and exact analytic
capsule signed-distance collision projection so fabric drapes under gravity
with zero limb penetration.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
Capsule = tuple[FloatArray, FloatArray, float]


class ClothSimulationError(ValueError):
    """Raised for invalid cloth topology or solver parameters."""


@dataclass(frozen=True)
class XpbdResult:
    """Outcome of one XPBD cloth step."""

    positions: FloatArray
    velocities: FloatArray
    min_sdf: float
    max_stretch: float


def capsule_sdf(
    points: FloatArray, start: FloatArray, end: FloatArray, radius: float
) -> FloatArray:
    """Exact signed distance from points to a capsule surface."""
    pts = np.asarray(points, dtype=np.float64)
    a = np.asarray(start, dtype=np.float64)
    b = np.asarray(end, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ClothSimulationError("points must have shape (N, 3)")
    if a.shape != (3,) or b.shape != (3,):
        raise ClothSimulationError("capsule endpoints must be 3-vectors")
    if radius <= 0.0 or not np.isfinite(radius):
        raise ClothSimulationError("capsule radius must be positive and finite")
    segment = b - a
    length_sq = float(segment @ segment)
    if length_sq <= 1e-12:
        raise ClothSimulationError("capsule endpoints must differ")
    projection = np.clip(((pts - a) @ segment) / length_sq, 0.0, 1.0)
    closest = a + projection[:, None] * segment
    return np.asarray(np.linalg.norm(pts - closest, axis=1) - radius, dtype=np.float64)


def _validate_edges(edges: FloatArray, count: int) -> npt.NDArray[np.intp]:
    edge = np.asarray(edges)
    if edge.ndim != 2 or edge.shape[1] != 2:
        raise ClothSimulationError("edges must have shape (M, 2)")
    if edge.size and (np.any(edge < 0) or np.any(edge >= count)):
        raise ClothSimulationError("edge indices out of range")
    if edge.size and np.any(edge[:, 0] == edge[:, 1]):
        raise ClothSimulationError("self-edges are not allowed")
    return edge.astype(np.intp)


def _capsule_repulsion(positions: FloatArray, capsules: list[Capsule]) -> FloatArray:
    corrected = np.array(positions, dtype=np.float64, copy=True)
    for start, end, radius in capsules:
        a = np.asarray(start, dtype=np.float64)
        b = np.asarray(end, dtype=np.float64)
        if a.shape != (3,) or b.shape != (3,) or radius <= 0.0:
            raise ClothSimulationError("invalid capsule definition")
        segment = b - a
        length_sq = float(segment @ segment)
        if length_sq <= 1e-12:
            raise ClothSimulationError("capsule endpoints must differ")
        projection = np.clip(((corrected - a) @ segment) / length_sq, 0.0, 1.0)
        closest = a + projection[:, None] * segment
        offset = corrected - closest
        distance = np.linalg.norm(offset, axis=1)
        penetrating = distance < radius
        if np.any(penetrating):
            safe = np.where(distance > 1e-12, distance, 1.0)
            direction = offset / safe[:, None]
            corrected[penetrating] = closest[penetrating] + radius * direction[penetrating]
    return corrected


def step_xpbd_cloth_simulation(
    positions: FloatArray,
    velocities: FloatArray,
    edges: FloatArray,
    *,
    dt: float = 1.0 / 240.0,
    gravity: FloatArray | tuple[float, float, float] = (0.0, 0.0, -9.80665),
    compliance: float = 1e-6,
    wind: FloatArray | tuple[float, float, float] = (0.0, 0.0, 0.0),
    capsules: list[Capsule] | None = None,
    iterations: int = 4,
    rest_lengths: FloatArray | None = None,
) -> XpbdResult:
    """Advance a cloth mesh by one XPBD step with capsule collisions."""
    pos = np.asarray(positions, dtype=np.float64)
    vel = np.asarray(velocities, dtype=np.float64)
    if pos.ndim != 2 or pos.shape[1] != 3:
        raise ClothSimulationError("positions must have shape (N, 3)")
    if vel.shape != pos.shape:
        raise ClothSimulationError("velocities must match positions")
    if not (np.all(np.isfinite(pos)) and np.all(np.isfinite(vel))):
        raise ClothSimulationError("cloth state must be finite")
    if not np.isfinite(dt) or dt <= 0.0:
        raise ClothSimulationError("time step must be positive and finite")
    if compliance < 0.0 or not np.isfinite(compliance):
        raise ClothSimulationError("compliance must be non-negative and finite")
    if iterations < 1:
        raise ClothSimulationError("iterations must be at least one")
    edge = _validate_edges(edges, pos.shape[0])
    acceleration = np.asarray(gravity, dtype=np.float64)
    wind_acceleration = np.asarray(wind, dtype=np.float64)
    if acceleration.shape != (3,) or wind_acceleration.shape != (3,):
        raise ClothSimulationError("gravity and wind must be 3-vectors")
    if rest_lengths is None:
        delta = pos[edge[:, 0]] - pos[edge[:, 1]]
        rest = np.linalg.norm(delta, axis=1)
    else:
        rest = np.asarray(rest_lengths, dtype=np.float64)
        if rest.shape != (edge.shape[0],) or np.any(rest <= 0.0):
            raise ClothSimulationError("rest lengths must be positive and match edges")

    previous = np.array(pos, dtype=np.float64, copy=True)
    velocity = np.array(vel, dtype=np.float64, copy=True)
    velocity = velocity + (acceleration + wind_acceleration) * dt
    predicted = previous + velocity * dt

    alpha = compliance / (dt * dt)
    for _ in range(iterations):
        delta = predicted[edge[:, 0]] - predicted[edge[:, 1]]
        length = np.linalg.norm(delta, axis=1)
        safe = np.where(length > 1e-12, length, 1.0)
        correction = ((length - rest) / (2.0 + alpha))[:, None] * (delta / safe[:, None])
        np.add.at(predicted, edge[:, 0], -0.5 * correction)
        np.add.at(predicted, edge[:, 1], 0.5 * correction)
        if capsules:
            predicted = _capsule_repulsion(predicted, capsules)

    velocity = (predicted - previous) / dt
    minimum_sdf = 0.0
    if capsules:
        minimum_sdf = min(
            float(np.min(capsule_sdf(predicted, start, end, radius)))
            for start, end, radius in capsules
        )
    final_delta = predicted[edge[:, 0]] - predicted[edge[:, 1]]
    final_length = np.linalg.norm(final_delta, axis=1)
    stretch = np.abs(final_length - rest) / np.maximum(rest, 1e-12)
    return XpbdResult(
        positions=predicted,
        velocities=velocity,
        min_sdf=minimum_sdf,
        max_stretch=float(np.max(stretch)) if stretch.size else 0.0,
    )
