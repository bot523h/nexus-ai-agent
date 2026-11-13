"""Law 5 — Lie-algebra se(3) object permanence and occlusion state persistence.

When an occluder crosses an object in AI video, the hidden object vanishes,
teleports or changes size. This module models persistent rigid objects with an
SE(3) pose, an se(3) twist and invariant extents, propagates them through 100%
Z-buffer occlusion by constant-momentum geodesic integration under gravity, and
audits/repairs post-occlusion re-emergence.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

EXTENT_TOLERANCE = 1e-9
DETERMINANT_TOLERANCE = 1e-9


class ObjectPermanenceError(ValueError):
    """Raised for invalid rigid-object states."""


@dataclass(frozen=True)
class ObjectState:
    """A persistent rigid object."""

    pose: FloatArray
    twist: FloatArray
    extents: FloatArray
    mass: float

    def __post_init__(self) -> None:
        if self.pose.shape != (4, 4):
            raise ObjectPermanenceError("pose must be 4x4")
        if self.twist.shape != (6,):
            raise ObjectPermanenceError("twist must be a 6-vector")
        if self.extents.shape != (3,):
            raise ObjectPermanenceError("extents must be a 3-vector")
        if not np.isfinite(self.mass) or self.mass <= 0.0:
            raise ObjectPermanenceError("mass must be positive and finite")
        if not (
            np.all(np.isfinite(self.pose))
            and np.all(np.isfinite(self.twist))
            and np.all(np.isfinite(self.extents))
        ):
            raise ObjectPermanenceError("object state must be finite")


@dataclass(frozen=True)
class PermanenceAudit:
    """Result of auditing an object's occlusion trajectory."""

    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]
    repaired_states: tuple[ObjectState, ...]
    max_impulse: float

    @property
    def healed(self) -> bool:
        return not self.violations_after


def _hat_omega(omega: FloatArray) -> FloatArray:
    return np.array(
        [
            [0.0, -omega[2], omega[1]],
            [omega[2], 0.0, -omega[0]],
            [-omega[1], omega[0], 0.0],
        ],
        dtype=np.float64,
    )


def _so3_exp(omega: FloatArray) -> FloatArray:
    theta = float(np.linalg.norm(omega))
    skew = _hat_omega(omega)
    if theta <= 1e-12:
        return np.eye(3, dtype=np.float64) + skew
    a = np.sin(theta) / theta
    b = (1.0 - np.cos(theta)) / theta**2
    return np.asarray(np.eye(3, dtype=np.float64) + a * skew + b * (skew @ skew), dtype=np.float64)


def _so3_log(rotation: FloatArray) -> FloatArray:
    cos_theta = np.clip((float(np.trace(rotation)) - 1.0) / 2.0, -1.0, 1.0)
    theta = float(np.arccos(cos_theta))
    if theta <= 1e-12:
        return np.zeros(3, dtype=np.float64)
    if abs(np.pi - theta) <= 1e-6:
        axis = np.sqrt(np.maximum((np.diag(rotation) + 1.0) / 2.0, 0.0))
        index = int(np.argmax(axis))
        axis = axis / max(axis[index], 1e-12)
        return np.asarray(theta * axis, dtype=np.float64)
    factor = theta / (2.0 * np.sin(theta))
    skew = factor * (rotation - rotation.T)
    return np.array([skew[2, 1], skew[0, 2], skew[1, 0]], dtype=np.float64)


def se3_exp(xi: FloatArray) -> FloatArray:
    """Exponential map from se(3) to SE(3)."""
    twist = np.asarray(xi, dtype=np.float64)
    if twist.shape != (6,):
        raise ObjectPermanenceError("se(3) twist must be a 6-vector")
    omega = twist[:3]
    velocity = twist[3:]
    rotation = _so3_exp(omega)
    theta = float(np.linalg.norm(omega))
    skew = _hat_omega(omega)
    if theta <= 1e-12:
        v_matrix = np.eye(3, dtype=np.float64) + 0.5 * skew
    else:
        a = (1.0 - np.cos(theta)) / theta**2
        b = (theta - np.sin(theta)) / theta**3
        v_matrix = np.eye(3, dtype=np.float64) + a * skew + b * (skew @ skew)
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = v_matrix @ velocity
    return transform


def se3_log(transform: FloatArray) -> FloatArray:
    """Logarithm map from SE(3) to se(3)."""
    matrix = np.asarray(transform, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise ObjectPermanenceError("SE(3) transform must be 4x4")
    rotation = matrix[:3, :3]
    omega = _so3_log(rotation)
    theta = float(np.linalg.norm(omega))
    skew = _hat_omega(omega)
    if theta <= 1e-12:
        inverse_v = np.eye(3, dtype=np.float64) - 0.5 * skew
    else:
        coefficient = (1.0 / theta**2) * (
            1.0 - (theta * np.sin(theta)) / (2.0 * (1.0 - np.cos(theta)))
        )
        inverse_v = np.eye(3, dtype=np.float64) - 0.5 * skew + coefficient * (skew @ skew)
    velocity = inverse_v @ matrix[:3, 3]
    return np.concatenate([omega, velocity])


def propagate_occluded_object(
    state: ObjectState,
    dt: float,
    *,
    steps: int = 1,
    gravity: FloatArray | tuple[float, float, float] = (0.0, 0.0, -9.80665),
) -> tuple[ObjectState, ...]:
    """Propagate an occluded object by constant-momentum geodesic integration."""
    if not np.isfinite(dt) or dt <= 0.0:
        raise ObjectPermanenceError("time step must be positive and finite")
    if steps < 0:
        raise ObjectPermanenceError("steps must be non-negative")
    acceleration = np.asarray(gravity, dtype=np.float64)
    if acceleration.shape != (3,) or not np.all(np.isfinite(acceleration)):
        raise ObjectPermanenceError("gravity must be a finite 3-vector")
    states: list[ObjectState] = [state]
    pose = np.array(state.pose, dtype=np.float64, copy=True)
    twist = np.array(state.twist, dtype=np.float64, copy=True)
    for _ in range(steps):
        pose = pose @ se3_exp(twist * dt)
        twist = twist.copy()
        twist[3:] = twist[3:] + acceleration * dt
        states.append(ObjectState(pose=pose, twist=twist, extents=state.extents, mass=state.mass))
    return tuple(states)


def _translation(state: ObjectState) -> FloatArray:
    return state.pose[:3, 3]


def _violations(states: tuple[ObjectState, ...], impulse_bound: float) -> tuple[str, ...]:
    violations: list[str] = []
    reference = states[0]
    for state in states:
        if float(np.max(np.abs(state.extents - reference.extents))) > EXTENT_TOLERANCE:
            violations.append("volume_drift")
            break
    for state in states:
        if abs(float(np.linalg.det(state.pose[:3, :3])) - 1.0) > DETERMINANT_TOLERANCE:
            violations.append("rotation_determinant")
            break
    for previous, current in zip(states[:-1], states[1:], strict=True):
        jump = float(np.linalg.norm(_translation(current) - _translation(previous)))
        if jump > impulse_bound:
            violations.append("teleport")
            break
    return tuple(dict.fromkeys(violations))


def _orthonormalise(rotation: FloatArray) -> FloatArray:
    u, _, vt = np.linalg.svd(rotation)
    orthonormal = u @ vt
    if float(np.linalg.det(orthonormal)) < 0.0:
        u[:, -1] *= -1.0
        orthonormal = u @ vt
    return np.asarray(orthonormal, dtype=np.float64)


def repair_object_permanence(
    states: tuple[ObjectState, ...], *, impulse_bound: float
) -> tuple[ObjectState, ...]:
    """Repair extent drift, non-rigid rotations and teleportation."""
    if not states:
        raise ObjectPermanenceError("at least one state is required")
    if impulse_bound <= 0.0:
        raise ObjectPermanenceError("impulse bound must be positive")
    reference_extents = np.array(states[0].extents, dtype=np.float64, copy=True)
    repaired: list[ObjectState] = []
    previous_translation: FloatArray | None = None
    for state in states:
        pose = np.array(state.pose, dtype=np.float64, copy=True)
        pose[:3, :3] = _orthonormalise(pose[:3, :3])
        if previous_translation is not None:
            delta = pose[:3, 3] - previous_translation
            distance = float(np.linalg.norm(delta))
            if distance > impulse_bound:
                pose[:3, 3] = previous_translation + delta * (impulse_bound / distance)
        repaired.append(
            ObjectState(
                pose=pose,
                twist=np.array(state.twist, dtype=np.float64, copy=True),
                extents=np.array(reference_extents, dtype=np.float64, copy=True),
                mass=state.mass,
            )
        )
        previous_translation = pose[:3, 3]
    return tuple(repaired)


def audit_object_permanence(
    states: tuple[ObjectState, ...], *, impulse_bound: float = 0.5
) -> PermanenceAudit:
    """Audit and repair an occlusion trajectory for object permanence."""
    if not states:
        raise ObjectPermanenceError("at least one state is required")
    if impulse_bound <= 0.0:
        raise ObjectPermanenceError("impulse bound must be positive")
    violations_before = _violations(states, impulse_bound)
    repaired = repair_object_permanence(states, impulse_bound=impulse_bound)
    violations_after = _violations(repaired, impulse_bound)
    max_impulse = 0.0
    for previous, current in zip(repaired[:-1], repaired[1:], strict=True):
        max_impulse = max(
            max_impulse, float(np.linalg.norm(_translation(current) - _translation(previous)))
        )
    return PermanenceAudit(
        violations_before=violations_before,
        violations_after=violations_after,
        repaired_states=repaired,
        max_impulse=max_impulse,
    )
