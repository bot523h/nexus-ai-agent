"""Law 7 — symplectic Stormer-Verlet mechanics and frictional impulse resolution.

AI video violates conservation of momentum and gravity during falls, impacts and
vehicle motion. This module provides a symplectic velocity-Verlet integrator and
an exact 3D impulse-momentum collision resolver with restitution and Coulomb
friction. Momentum is conserved to machine precision and kinetic energy can
never increase for a physical coefficient of restitution.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


class MechanicsViolationError(ValueError):
    """Raised for unphysical masses, restitution or friction."""


@dataclass(frozen=True)
class CollisionResult:
    """Outcome of an impulse-based collision resolution."""

    velocities: FloatArray
    impulses: FloatArray
    momentum_before: FloatArray
    momentum_after: FloatArray
    angular_momentum_before: FloatArray
    angular_momentum_after: FloatArray
    energy_before: float
    energy_after: float


def _validate_bodies(
    positions: FloatArray, velocities: FloatArray, masses: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    pos = np.asarray(positions, dtype=np.float64)
    vel = np.asarray(velocities, dtype=np.float64)
    mass = np.asarray(masses, dtype=np.float64)
    if pos.ndim != 2 or pos.shape[1] != 3:
        raise MechanicsViolationError("positions must have shape (N, 3)")
    if vel.shape != pos.shape:
        raise MechanicsViolationError("velocities must match positions")
    if mass.shape != (pos.shape[0],):
        raise MechanicsViolationError("masses must have shape (N,)")
    if np.any(mass <= 0.0) or not np.all(np.isfinite(mass)):
        raise MechanicsViolationError("masses must be positive and finite")
    if not (np.all(np.isfinite(pos)) and np.all(np.isfinite(vel))):
        raise MechanicsViolationError("positions and velocities must be finite")
    return pos, vel, mass


def total_linear_momentum(velocities: FloatArray, masses: FloatArray) -> FloatArray:
    """Total linear momentum sum(m_i v_i)."""
    vel = np.asarray(velocities, dtype=np.float64)
    mass = np.asarray(masses, dtype=np.float64)
    if vel.ndim != 2 or vel.shape[1] != 3 or mass.shape != (vel.shape[0],):
        raise MechanicsViolationError("velocities must be (N, 3) and masses (N,)")
    return np.asarray(np.sum(mass[:, None] * vel, axis=0), dtype=np.float64)


def total_angular_momentum(
    positions: FloatArray, velocities: FloatArray, masses: FloatArray
) -> FloatArray:
    """Total angular momentum about the origin sum(m_i r_i x v_i)."""
    pos, vel, mass = _validate_bodies(positions, velocities, masses)
    return np.asarray(np.sum(mass[:, None] * np.cross(pos, vel), axis=0), dtype=np.float64)


def kinetic_energy(velocities: FloatArray, masses: FloatArray) -> float:
    """Total translational kinetic energy."""
    vel = np.asarray(velocities, dtype=np.float64)
    mass = np.asarray(masses, dtype=np.float64)
    if vel.ndim != 2 or vel.shape[1] != 3 or mass.shape != (vel.shape[0],):
        raise MechanicsViolationError("velocities must be (N, 3) and masses (N,)")
    return float(0.5 * np.sum(mass * np.sum(vel**2, axis=1)))


def stormer_verlet_step(
    positions: FloatArray,
    velocities: FloatArray,
    masses: FloatArray,
    forces: FloatArray,
    dt: float,
) -> tuple[FloatArray, FloatArray]:
    """One symplectic velocity-Verlet step under constant external force."""
    pos, vel, mass = _validate_bodies(positions, velocities, masses)
    force = np.asarray(forces, dtype=np.float64)
    if force.shape != pos.shape:
        raise MechanicsViolationError("forces must match positions")
    if not np.isfinite(dt) or dt <= 0.0:
        raise MechanicsViolationError("time step must be positive and finite")
    acceleration = force / mass[:, None]
    half_velocity = vel + 0.5 * acceleration * dt
    new_positions = pos + half_velocity * dt
    new_velocities = half_velocity + 0.5 * acceleration * dt
    return new_positions, new_velocities


def resolve_rigid_body_collision(
    positions: FloatArray,
    velocities: FloatArray,
    masses: FloatArray,
    radii: FloatArray,
    *,
    restitution: float = 0.5,
    friction: float = 0.2,
    iterations: int = 4,
) -> CollisionResult:
    """Resolve overlapping spheres with restitution and Coulomb friction."""
    pos, vel, mass = _validate_bodies(positions, velocities, masses)
    radius = np.asarray(radii, dtype=np.float64)
    if radius.shape != (pos.shape[0],) or np.any(radius <= 0.0):
        raise MechanicsViolationError("radii must be positive and match bodies")
    if not 0.0 <= restitution <= 1.0:
        raise MechanicsViolationError("restitution must lie in [0, 1]")
    if friction < 0.0 or not np.isfinite(friction):
        raise MechanicsViolationError("friction must be non-negative and finite")
    if iterations < 1:
        raise MechanicsViolationError("iterations must be at least one")

    momentum_before = total_linear_momentum(vel, mass)
    angular_before = total_angular_momentum(pos, vel, mass)
    energy_before = kinetic_energy(vel, mass)

    resolved = np.array(vel, dtype=np.float64, copy=True)
    impulses = np.zeros_like(resolved)
    count = pos.shape[0]
    for _ in range(iterations):
        for i in range(count):
            for j in range(i + 1, count):
                delta = pos[j] - pos[i]
                distance = float(np.linalg.norm(delta))
                overlap = radius[i] + radius[j] - distance
                if overlap <= 0.0 or distance <= 1e-12:
                    continue
                normal = delta / distance
                relative = resolved[j] - resolved[i]
                normal_speed = float(relative @ normal)
                if normal_speed >= 0.0:
                    continue
                inverse_mass = 1.0 / mass[i] + 1.0 / mass[j]
                normal_impulse = -(1.0 + restitution) * normal_speed / inverse_mass
                tangential = relative - normal_speed * normal
                tangent_speed = float(np.linalg.norm(tangential))
                if tangent_speed > 1e-12:
                    tangent = tangential / tangent_speed
                    friction_impulse = min(friction * normal_impulse, tangent_speed / inverse_mass)
                else:
                    tangent = np.zeros(3, dtype=np.float64)
                    friction_impulse = 0.0
                impulse = normal_impulse * normal + friction_impulse * tangent
                resolved[i] -= impulse / mass[i]
                resolved[j] += impulse / mass[j]
                impulses[i] -= impulse
                impulses[j] += impulse

    momentum_after = total_linear_momentum(resolved, mass)
    angular_after = total_angular_momentum(pos, resolved, mass)
    energy_after = kinetic_energy(resolved, mass)
    return CollisionResult(
        velocities=resolved,
        impulses=impulses,
        momentum_before=momentum_before,
        momentum_after=momentum_after,
        angular_momentum_before=angular_before,
        angular_momentum_after=angular_after,
        energy_before=energy_before,
        energy_after=energy_after,
    )
