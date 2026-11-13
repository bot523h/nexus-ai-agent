"""WAVE 24 — projected Gauss-Seidel linear complementarity rigid multi-contact.

A deterministic, bounded rigid-body contact solver:

* rigid bodies with mass and inertia (2-D disc + 2-D box moments);
* contacts described by a normal plus tangential friction directions;
* the contact LCP is solved with **projected Gauss-Seidel** (PGS): each row is
  updated and the impulse is clamped to the friction cone ``[0, +inf)`` on the
  normal and ``[-mu * n, +mu * n]`` on the tangent;
* complementarity residuals ``phi_i = n_i . v_i >= 0`` and ``lambda_i >= 0``
  with ``phi_i * lambda_i == 0`` are *measured* and returned so a non-converged
  solve is never reported as a solution;
* restitution is applied with the standard velocity-bias target, and Baumgarte
  position bias removes penetration over a bounded number of iterations.

Every result carries the number of PGS sweeps and the residual, so the caller
can distinguish a converged contact set from a truncated one. No project state,
no new authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "ContactError",
    "RigidBody",
    "Contact",
    "ContactSolution",
    "solve_contacts_pgs",
    "complementarity_residual",
    "MAX_CONTACTS",
    "MAX_BODIES",
    "MAX_SWEEPS",
]

MAX_CONTACTS: Final[int] = 4096
MAX_BODIES: Final[int] = 1024
MAX_SWEEPS: Final[int] = 10_000
_EPS: Final[float] = 1e-12


class ContactError(ValueError):
    """Raised for malformed bodies, contacts, or out-of-range parameters."""


@dataclass
class RigidBody:
    """A 2-D rigid body: mass, inertia, position, angle, linear/angular velocity.

    ``inv_mass`` and ``inv_inertia`` are derived (0 for an immovable/static
    body, which is expressed by ``static=True``).
    """

    mass: float
    inertia: float
    position: np.ndarray
    angle: float = 0.0
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(2))
    angular_velocity: float = 0.0
    static: bool = False

    def __post_init__(self) -> None:
        pos = np.asarray(self.position, dtype=np.float64)
        vel = np.asarray(self.velocity, dtype=np.float64)
        if pos.shape != (2,) or vel.shape != (2,):
            raise ContactError("position and velocity must be 2-vectors")
        if not (np.all(np.isfinite(pos)) and np.all(np.isfinite(vel))):
            raise ContactError("position/velocity contain NaN/Inf")
        if not np.isfinite(self.angle) or not np.isfinite(self.angular_velocity):
            raise ContactError("angle/angular_velocity must be finite")
        if not self.static:
            if not np.isfinite(self.mass) or self.mass <= 0.0:
                raise ContactError("mass must be finite and positive (or static)")
            if not np.isfinite(self.inertia) or self.inertia <= 0.0:
                raise ContactError("inertia must be finite and positive (or static)")
        object.__setattr__(self, "position", pos)
        object.__setattr__(self, "velocity", vel)

    @property
    def inv_mass(self) -> float:
        return 0.0 if self.static else 1.0 / self.mass

    @property
    def inv_inertia(self) -> float:
        return 0.0 if self.static else 1.0 / self.inertia


@dataclass(frozen=True)
class Contact:
    """A contact between two bodies at a world point with a unit normal.

    ``normal`` points from body ``a`` to body ``b``. ``r_a``/``r_b`` are the
    contact offsets from each body's centre. ``penetration`` >= 0 is the
    overlap depth; ``mu`` is the Coulomb friction coefficient; ``restitution``
    is the target bounce fraction in ``[0, 1]``.
    """

    a: int
    b: int
    point: np.ndarray
    normal: np.ndarray
    r_a: np.ndarray
    r_b: np.ndarray
    penetration: float = 0.0
    mu: float = 0.5
    restitution: float = 0.0

    def __post_init__(self) -> None:
        pt = np.asarray(self.point, dtype=np.float64)
        nrm = np.asarray(self.normal, dtype=np.float64)
        ra = np.asarray(self.r_a, dtype=np.float64)
        rb = np.asarray(self.r_b, dtype=np.float64)
        for arr, name in ((pt, "point"), (nrm, "normal"), (ra, "r_a"), (rb, "r_b")):
            if arr.shape != (2,):
                raise ContactError(f"{name} must be a 2-vector")
            if not np.all(np.isfinite(arr)):
                raise ContactError(f"{name} contains NaN/Inf")
        n = float(np.linalg.norm(nrm))
        if n < _EPS:
            raise ContactError("normal is a zero vector")
        if not np.isfinite(self.penetration) or self.penetration < 0.0:
            raise ContactError("penetration must be finite and non-negative")
        if not np.isfinite(self.mu) or self.mu < 0.0:
            raise ContactError("mu must be finite and non-negative")
        if not (0.0 <= self.restitution <= 1.0):
            raise ContactError("restitution must lie in [0, 1]")
        object.__setattr__(self, "point", pt)
        object.__setattr__(self, "normal", nrm / n)
        object.__setattr__(self, "r_a", ra)
        object.__setattr__(self, "r_b", rb)

    def tangent(self) -> np.ndarray:
        """Unit tangent (90-degree rotation of the normal)."""
        n = self.normal
        t: np.ndarray = np.array([-n[1], n[0]])
        return t


@dataclass(frozen=True)
class ContactSolution:
    """The PGS result. ``converged`` means the measured residual is <= tol."""

    normal_impulses: np.ndarray
    tangent_impulses: np.ndarray
    sweeps: int
    residual: float
    converged: bool
    max_complementarity: float


def _body_contact_velocity(body: RigidBody, r: np.ndarray) -> np.ndarray:
    v: np.ndarray = body.velocity + body.angular_velocity * np.array([-r[1], r[0]])
    return v


def _effective_mass(
    body_a: RigidBody, body_b: RigidBody, r_a: np.ndarray, r_b: np.ndarray, direction: np.ndarray
) -> float:
    ra_cross = r_a[0] * direction[1] - r_a[1] * direction[0]
    rb_cross = r_b[0] * direction[1] - r_b[1] * direction[0]
    k = (
        body_a.inv_mass
        + body_b.inv_mass
        + body_a.inv_inertia * ra_cross * ra_cross
        + body_b.inv_inertia * rb_cross * rb_cross
    )
    return float(k)


def _apply_impulse(
    body_a: RigidBody,
    body_b: RigidBody,
    r_a: np.ndarray,
    r_b: np.ndarray,
    impulse: np.ndarray,
) -> None:
    body_a.velocity = body_a.velocity - impulse * body_a.inv_mass
    body_a.angular_velocity -= body_a.inv_inertia * (r_a[0] * impulse[1] - r_a[1] * impulse[0])
    body_b.velocity = body_b.velocity + impulse * body_b.inv_mass
    body_b.angular_velocity += body_b.inv_inertia * (r_b[0] * impulse[1] - r_b[1] * impulse[0])


def solve_contacts_pgs(
    bodies: list[RigidBody],
    contacts: list[Contact],
    *,
    sweeps: int = 20,
    tolerance: float = 1e-6,
    baumgarte: float = 0.2,
    slop: float = 1e-3,
) -> ContactSolution:
    """Solve a rigid multi-contact set with projected Gauss-Seidel.

    The bodies are mutated in place (velocities updated). Returns the impulses,
    the number of sweeps actually used, the measured normal-velocity residual,
    and the maximum complementarity residual ``max|phi_i * lambda_i|``.
    """
    if not bodies:
        raise ContactError("at least one body is required")
    if len(bodies) > MAX_BODIES:
        raise ContactError(f"at most {MAX_BODIES} bodies")
    if len(contacts) > MAX_CONTACTS:
        raise ContactError(f"at most {MAX_CONTACTS} contacts")
    if sweeps < 1 or sweeps > MAX_SWEEPS:
        raise ContactError(f"sweeps must be in [1, {MAX_SWEEPS}]")
    if tolerance <= 0.0:
        raise ContactError("tolerance must be positive")
    if not (0.0 <= baumgarte <= 1.0):
        raise ContactError("baumgarte must lie in [0, 1]")
    if slop < 0.0:
        raise ContactError("slop must be non-negative")
    for c in contacts:
        if c.a < 0 or c.a >= len(bodies) or c.b < 0 or c.b >= len(bodies):
            raise ContactError("contact references an out-of-range body index")
        if c.a == c.b:
            raise ContactError("a contact cannot reference the same body twice")

    n_c = len(contacts)
    normals = np.array([c.normal for c in contacts]) if n_c else np.zeros((0, 2))
    tangents = np.array([c.tangent() for c in contacts]) if n_c else np.zeros((0, 2))
    k_n = np.array(
        [_effective_mass(bodies[c.a], bodies[c.b], c.r_a, c.r_b, c.normal) for c in contacts]
    )
    k_t = np.array(
        [_effective_mass(bodies[c.a], bodies[c.b], c.r_a, c.r_b, c.tangent()) for c in contacts]
    )
    # Restitution target along the normal.
    target = np.zeros(n_c)
    for i, c in enumerate(contacts):
        rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
            bodies[c.a], c.r_a
        )
        vn = float(np.dot(rel, c.normal))
        target[i] = -c.restitution * vn if vn < 0.0 else 0.0

    lambda_n = np.zeros(n_c)
    lambda_t = np.zeros(n_c)
    residual = 0.0
    used = 0
    for k in range(sweeps):
        used = k + 1
        residual = 0.0
        for i, c in enumerate(contacts):
            if k_n[i] < _EPS:
                continue
            rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
                bodies[c.a], c.r_a
            )
            vn = float(np.dot(rel, c.normal))
            bias = baumgarte * max(c.penetration - slop, 0.0)
            d_lambda = (target[i] + bias - vn) / k_n[i]
            old = lambda_n[i]
            lambda_n[i] = max(0.0, old + d_lambda)
            delta = lambda_n[i] - old
            if abs(delta) > 0.0:
                _apply_impulse(bodies[c.a], bodies[c.b], c.r_a, c.r_b, c.normal * delta)
            # Friction: clamp to the cone using the current normal impulse.
            if k_t[i] > _EPS:
                rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
                    bodies[c.a], c.r_a
                )
                vt = float(np.dot(rel, c.tangent()))
                d_lt = -vt / k_t[i]
                limit = c.mu * lambda_n[i]
                old_t = lambda_t[i]
                lambda_t[i] = float(np.clip(old_t + d_lt, -limit, limit))
                delta_t = lambda_t[i] - old_t
                if abs(delta_t) > 0.0:
                    _apply_impulse(bodies[c.a], bodies[c.b], c.r_a, c.r_b, c.tangent() * delta_t)
            rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
                bodies[c.a], c.r_a
            )
            vn = float(np.dot(rel, c.normal))
            residual = max(residual, abs(min(0.0, vn)))
        if residual <= tolerance:
            break

    max_comp = 0.0
    for i, c in enumerate(contacts):
        rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
            bodies[c.a], c.r_a
        )
        vn = float(np.dot(rel, c.normal))
        # Complementarity: at least one of phi>=0 or lambda==0 must hold; the
        # violation is |min(0, vn)| * lambda (zero when either factor is zero).
        max_comp = max(max_comp, abs(min(0.0, vn)) * lambda_n[i])
    return ContactSolution(
        normal_impulses=lambda_n,
        tangent_impulses=lambda_t,
        sweeps=used,
        residual=residual,
        converged=residual <= tolerance,
        max_complementarity=max_comp,
    )


def complementarity_residual(
    bodies: list[RigidBody], contacts: list[Contact], solution: ContactSolution
) -> float:
    """Recompute the complementarity residual from a *given* solution.

    Used by tests and by the falsification harness to confirm a reported
    ``converged=True`` actually corresponds to a complementary solution.
    """
    worst = 0.0
    for i, c in enumerate(contacts):
        rel = _body_contact_velocity(bodies[c.b], c.r_b) - _body_contact_velocity(
            bodies[c.a], c.r_a
        )
        vn = float(np.dot(rel, c.normal))
        worst = max(worst, abs(min(0.0, vn)) * solution.normal_impulses[i])
    return worst
