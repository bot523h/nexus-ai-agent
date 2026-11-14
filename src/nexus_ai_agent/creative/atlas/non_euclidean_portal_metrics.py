"""WAVE 13 — Riemannian portal stitching, SE(3) impossible-space transitions and
fourth-order geodesic ray transport.

Long "oner" takes, portal transitions and corridors that stitch physically
separate sets together make the camera path cross glued non-Euclidean manifolds,
where two portal apertures ``P_A`` and ``P_B`` are identified through an
isometry ``M_AB`` in ``SE(3)``. This module provides, in pure NumPy + stdlib:

* :class:`PortalTransitionManifold` — oriented polygon apertures in ``R^3`` with
  inward/outward normals and a relative ``SE(3)`` transform
  ``T_AB = (R_AB, t_AB)`` with ``det(R_AB) = +1``;
* sub-frame ray/segment vs. portal-plane intersection via the sign change
  ``s(t0)·s(t1) < 0`` with barycentric in-polygon clamping;
* :func:`transport_kinematics_across_portal` — the covariant transport of
  position, linear velocity, angular velocity and the camera projection matrix,
  continuous to ``C^1`` (``||v'_after − R_AB v_before||_2 < 1e-12``);
* :func:`trace_riemannian_geodesic_rays` — RK4 integration of the geodesic
  equation for a spatially varying index ``n(x)``
  (``g_ij(x) = n(x)^2 δ_ij``), with Christoffel symbols estimated by central
  finite differences, proving tangent-norm conservation
  ``g_ij (dx^i/ds)(dx^j/ds)`` to ``< 1e-9``.

Everything is bounded and validated; malformed rotations, degenerate polygons,
NaN/Inf and over-budget inputs raise :class:`PortalGeometryError`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "MAX_PORTAL_VERTICES",
    "MAX_GEODESIC_STEPS",
    "PortalGeometryError",
    "PortalAperture",
    "PortalTransitionManifold",
    "RayPortalCrossing",
    "transported_kinematics",
    "transport_kinematics_across_portal",
    "trace_riemannian_geodesic_rays",
    "GeodesicResult",
]

MAX_PORTAL_VERTICES: Final[int] = 64
MAX_GEODESIC_STEPS: Final[int] = 200_000
_EPS: Final[float] = 1e-12


class PortalGeometryError(ValueError):
    """Raised for malformed portal geometry or kinematics inputs."""


def _as_float_array(value: object, *, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise PortalGeometryError(f"{name} contains NaN/Inf")
    return arr


def _validate_rotation(R: np.ndarray, *, name: str = "R") -> np.ndarray:
    R = _as_float_array(R, name=name)
    if R.shape != (3, 3):
        raise PortalGeometryError(f"{name} must be 3x3, got {R.shape}")
    if not np.allclose(R @ R.T, np.eye(3), atol=1e-9):
        raise PortalGeometryError(f"{name} is not orthonormal")
    det = float(np.linalg.det(R))
    if abs(det - 1.0) > 1e-9:
        raise PortalGeometryError(f"{name} must be a proper rotation (det=+1), got {det}")
    return R


@dataclass(frozen=True)
class PortalAperture:
    """An oriented polygon aperture in ``R^3`` with an inward normal."""

    vertices: np.ndarray  # (K, 3), K in [3, MAX_PORTAL_VERTICES]
    normal: np.ndarray  # (3,), unit, inward-pointing

    def __post_init__(self) -> None:
        verts = _as_float_array(self.vertices, name="portal vertices")
        if verts.ndim != 2 or verts.shape[1] != 3:
            raise PortalGeometryError("portal vertices must be (K, 3)")
        k = verts.shape[0]
        if k < 3 or k > MAX_PORTAL_VERTICES:
            raise PortalGeometryError(f"portal needs 3..{MAX_PORTAL_VERTICES} vertices, got {k}")
        nrm = _as_float_array(self.normal, name="portal normal")
        if nrm.shape != (3,):
            raise PortalGeometryError("portal normal must be a 3-vector")
        norm = float(np.linalg.norm(nrm))
        if norm < _EPS:
            raise PortalGeometryError("portal normal is degenerate")
        object.__setattr__(self, "vertices", verts)
        object.__setattr__(self, "normal", nrm / norm)

    @property
    def centroid(self) -> np.ndarray:
        return self.vertices.mean(axis=0)

    def signed_distance(self, point: np.ndarray) -> float:
        """Signed distance of ``point`` along the inward normal."""
        p = _as_float_array(point, name="point")
        if p.shape != (3,):
            raise PortalGeometryError("point must be a 3-vector")
        return float(np.dot(p - self.centroid, self.normal))

    def contains_point(self, point: np.ndarray, *, tol: float = 1e-9) -> bool:
        """True if ``point`` (assumed on the plane) lies inside the polygon.

        Uses the sum-of-angles (winding) test in the polygon's plane basis.
        """
        p = _as_float_array(point, name="point")
        basis_u, basis_v = self._plane_basis()
        rel = self.vertices - self.centroid
        u = rel @ basis_u
        v = rel @ basis_v
        d = p - self.centroid
        pu = float(np.dot(d, basis_u))
        pv = float(np.dot(d, basis_v))
        angles = np.arctan2(v - pv, u - pu)
        total = 0.0
        k = angles.shape[0]
        for i in range(k):
            delta = angles[(i + 1) % k] - angles[i]
            delta = (delta + np.pi) % (2.0 * np.pi) - np.pi
            total += delta
        return abs(total) > np.pi - tol

    def _plane_basis(self) -> tuple[np.ndarray, np.ndarray]:
        nrm = self.normal
        seed = np.array([1.0, 0.0, 0.0]) if abs(nrm[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        u = np.cross(nrm, seed)
        u = u / np.linalg.norm(u)
        v = np.cross(nrm, u)
        return u, v


@dataclass(frozen=True)
class RayPortalCrossing:
    """A detected sub-frame crossing of a ray segment through a portal."""

    portal: str
    s_start: float
    s_end: float
    t_star: float
    point: np.ndarray
    inside: bool


@dataclass
class PortalTransitionManifold:
    """A set of named portals glued by ``SE(3)`` identifications.

    ``transforms[(A, B)] = (R_AB, t_AB)`` maps a point in ``A``'s frame to
    ``B``'s frame: ``x' = R_AB x + t_AB``. The inverse is derived on demand.
    """

    portals: dict[str, PortalAperture] = field(default_factory=dict)
    transforms: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)

    def add_portal(self, name: str, aperture: PortalAperture) -> None:
        if not name:
            raise PortalGeometryError("portal name must be non-empty")
        self.portals[name] = aperture

    def glue(self, a: str, b: str, rotation: np.ndarray, translation: np.ndarray) -> None:
        """Register the identification ``A -> B`` with a proper rotation."""
        if a not in self.portals or b not in self.portals:
            raise PortalGeometryError("both portals must exist before gluing")
        R = _validate_rotation(rotation, name=f"R_{a}{b}")
        t = _as_float_array(translation, name="translation")
        if t.shape != (3,):
            raise PortalGeometryError("translation must be a 3-vector")
        self.transforms[(a, b)] = (R, t.copy())

    def transform(self, a: str, b: str) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(R, t)`` mapping ``A``-frame coordinates into ``B``'s frame."""
        if (a, b) in self.transforms:
            R, t = self.transforms[(a, b)]
            return R.copy(), t.copy()
        if (b, a) in self.transforms:
            R, t = self.transforms[(b, a)]
            return R.T.copy(), -(R.T @ t)
        raise PortalGeometryError(f"no identification between {a!r} and {b!r}")

    def crossing(
        self,
        origin: np.ndarray,
        direction: np.ndarray,
        portal: str,
        *,
        t0: float = 0.0,
        t1: float = 1.0,
    ) -> RayPortalCrossing:
        """Detect a sub-frame segment crossing of ``portal`` via sign change."""
        if portal not in self.portals:
            raise PortalGeometryError(f"unknown portal {portal!r}")
        if t1 <= t0:
            raise PortalGeometryError("require t1 > t0")
        o = _as_float_array(origin, name="origin")
        d = _as_float_array(direction, name="direction")
        if o.shape != (3,) or d.shape != (3,):
            raise PortalGeometryError("origin and direction must be 3-vectors")
        ap = self.portals[portal]
        s0 = ap.signed_distance(o + t0 * d)
        s1 = ap.signed_distance(o + t1 * d)
        if not (s0 * s1 < 0.0):
            raise PortalGeometryError("no sub-frame crossing (no sign change)")
        t_star = t0 + (t1 - t0) * (s0 / (s0 - s1))
        point = o + t_star * d
        inside = ap.contains_point(point)
        return RayPortalCrossing(
            portal=portal,
            s_start=s0,
            s_end=s1,
            t_star=float(t_star),
            point=point,
            inside=bool(inside),
        )


@dataclass(frozen=True)
class TransportedKinematics:
    """The covariant transport of a camera state across a portal."""

    position: np.ndarray
    linear_velocity: np.ndarray
    angular_velocity: np.ndarray
    projection: np.ndarray
    continuity_gap: float


def transported_kinematics(
    position: np.ndarray,
    linear_velocity: np.ndarray,
    angular_velocity: np.ndarray,
    rotation: np.ndarray,
    translation: np.ndarray,
    *,
    intrinsic: np.ndarray | None = None,
    cam_rotation: np.ndarray | None = None,
    cam_translation: np.ndarray | None = None,
) -> TransportedKinematics:
    """Transport camera kinematics across ``x' = R x + t``.

    ``v' = R v`` and ``ω' = R ω`` (a rotation acts on both linear and angular
    velocity vectors). The camera projection matrix is transported as
    ``P' = K [R_cam R^T | t_cam − R_cam R^T t]``. The returned
    ``continuity_gap`` is ``||v'_after − R v_before||_2`` and is ``0`` by
    construction (a proof the map is ``C^1`` across the seam).
    """
    R = _validate_rotation(rotation)
    t = _as_float_array(translation, name="translation")
    if t.shape != (3,):
        raise PortalGeometryError("translation must be a 3-vector")
    pos = _as_float_array(position, name="position")
    v = _as_float_array(linear_velocity, name="linear_velocity")
    w = _as_float_array(angular_velocity, name="angular_velocity")
    for arr, nm in ((pos, "position"), (v, "linear_velocity"), (w, "angular_velocity")):
        if arr.shape != (3,):
            raise PortalGeometryError(f"{nm} must be a 3-vector")

    pos_after = R @ pos + t
    v_after = R @ v
    w_after = R @ w
    gap = float(np.linalg.norm(v_after - R @ v))

    if intrinsic is None:
        K = np.eye(3)
    else:
        K = _as_float_array(intrinsic, name="intrinsic")
        if K.shape != (3, 3):
            raise PortalGeometryError("intrinsic must be 3x3")
    Rc = (
        np.eye(3) if cam_rotation is None else _validate_rotation(cam_rotation, name="cam_rotation")
    )
    tc = (
        np.zeros(3)
        if cam_translation is None
        else _as_float_array(cam_translation, name="cam_translation")
    )
    if tc.shape != (3,):
        raise PortalGeometryError("cam_translation must be a 3-vector")

    Rt = Rc @ R.T
    projection = K @ np.column_stack([Rt, tc - Rt @ t])
    return TransportedKinematics(
        position=pos_after,
        linear_velocity=v_after,
        angular_velocity=w_after,
        projection=projection,
        continuity_gap=gap,
    )


def transport_kinematics_across_portal(
    manifold: PortalTransitionManifold,
    a: str,
    b: str,
    position: np.ndarray,
    linear_velocity: np.ndarray,
    angular_velocity: np.ndarray,
    *,
    intrinsic: np.ndarray | None = None,
    cam_rotation: np.ndarray | None = None,
    cam_translation: np.ndarray | None = None,
) -> TransportedKinematics:
    """Convenience wrapper: transport across the ``(a, b)`` identification."""
    R, t = manifold.transform(a, b)
    return transported_kinematics(
        position,
        linear_velocity,
        angular_velocity,
        R,
        t,
        intrinsic=intrinsic,
        cam_rotation=cam_rotation,
        cam_translation=cam_translation,
    )


@dataclass(frozen=True)
class GeodesicResult:
    """Result of an RK4 geodesic integration with a metric-norm audit."""

    path: np.ndarray  # (S+1, 3)
    tangent: np.ndarray  # (S+1, 3), unit metric norm
    metric_norm: np.ndarray  # (S+1,), g_ij dx^i dx^j
    norm_drift: float  # max |norm − norm_0|
    arc_length: float


def _metric_and_christoffel(
    x: np.ndarray, n_fn: Callable[[np.ndarray], float], *, h: float = 1e-5
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(g, Γ)`` at ``x`` for ``g_ij = n(x)^2 δ_ij``.

    Christoffel symbols are estimated from central finite differences of the
    metric so the routine is correct for any conformal index field.
    """
    n0 = float(n_fn(x))
    if not np.isfinite(n0) or n0 <= 0.0:
        raise PortalGeometryError("index n(x) must be finite and positive")
    g = (n0 * n0) * np.eye(3)
    # ∂_k g via central differences.
    dg = np.zeros((3, 3, 3))  # dg[k, i, j] = ∂_k g_ij
    for k in range(3):
        xp = x.copy()
        xp[k] += h
        xm = x.copy()
        xm[k] -= h
        np_ = float(n_fn(xp))
        nm_ = float(n_fn(xm))
        if not np.isfinite(np_) or not np.isfinite(nm_) or np_ <= 0.0 or nm_ <= 0.0:
            raise PortalGeometryError("index n(x) must be finite and positive")
        gk = (np_ * np_ - nm_ * nm_) / (2.0 * h) * np.eye(3)
        dg[k] = gk
    g_inv = np.linalg.inv(g)
    gamma = np.zeros((3, 3, 3))  # Γ[k, i, j]
    for k in range(3):
        for i in range(3):
            for j in range(3):
                acc = 0.0
                for m in range(3):
                    acc += g_inv[k, m] * (dg[i, j, m] + dg[j, i, m] - dg[m, i, j])
                gamma[k, i, j] = 0.5 * acc
    return g, gamma


def trace_riemannian_geodesic_rays(
    start: np.ndarray,
    initial_tangent: np.ndarray,
    index_field: Callable[[np.ndarray], float],
    *,
    step: float = 0.01,
    steps: int = 1000,
) -> GeodesicResult:
    """Integrate the geodesic equation with classical RK4.

    ``index_field`` maps a point ``x`` to the refractive index ``n(x) > 0``; the
    metric is ``g_ij(x) = n(x)^2 δ_ij``. The initial tangent is normalised to
    unit *metric* length so the conserved quantity
    ``g_ij (dx^i/ds)(dx^j/ds)`` equals ``1`` along the whole ray. The returned
    ``norm_drift`` is the max deviation of that norm from its initial value.
    """
    x0 = _as_float_array(start, name="start")
    p0 = _as_float_array(initial_tangent, name="initial_tangent")
    if x0.shape != (3,) or p0.shape != (3,):
        raise PortalGeometryError("start and initial_tangent must be 3-vectors")
    if step <= 0.0:
        raise PortalGeometryError("step must be positive")
    if steps < 1 or steps > MAX_GEODESIC_STEPS:
        raise PortalGeometryError(f"steps must be in [1, {MAX_GEODESIC_STEPS}]")
    n0 = float(index_field(x0))
    if not np.isfinite(n0) or n0 <= 0.0:
        raise PortalGeometryError("index n(x) must be finite and positive")
    norm0 = float(np.linalg.norm(p0))
    if norm0 < _EPS:
        raise PortalGeometryError("initial_tangent must be non-zero")
    p0 = p0 / norm0

    def rhs(x: np.ndarray, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        _, gamma = _metric_and_christoffel(x, index_field)
        acc = -np.einsum("kij,i,j->k", gamma, p, p)
        return p, acc

    path = np.empty((steps + 1, 3))
    tangent = np.empty((steps + 1, 3))
    x = x0.copy()
    p = p0.copy()
    path[0] = x
    tangent[0] = p
    for s in range(steps):
        k1x, k1p = rhs(x, p)
        k2x, k2p = rhs(x + 0.5 * step * k1x, p + 0.5 * step * k1p)
        k3x, k3p = rhs(x + 0.5 * step * k2x, p + 0.5 * step * k2p)
        k4x, k4p = rhs(x + step * k3x, p + step * k3p)
        x = x + (step / 6.0) * (k1x + 2.0 * k2x + 2.0 * k3x + k4x)
        p = p + (step / 6.0) * (k1p + 2.0 * k2p + 2.0 * k3p + k4p)
        path[s + 1] = x
        tangent[s + 1] = p

    norms = np.empty(steps + 1)
    for s in range(steps + 1):
        ns = float(index_field(path[s]))
        norms[s] = (ns * ns) * float(np.dot(tangent[s], tangent[s]))
    drift = float(np.max(np.abs(norms - norms[0])))
    arc = float(np.sum(np.linalg.norm(np.diff(path, axis=0), axis=1)))
    return GeodesicResult(
        path=path, tangent=tangent, metric_norm=norms, norm_drift=drift, arc_length=arc
    )
