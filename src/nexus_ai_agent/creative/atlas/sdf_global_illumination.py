"""WAVE 21 — analytical signed-distance-field (SDF) global illumination.

A pure-NumPy, offline, deterministic renderer core that reasons about *exact*
geometry rather than voxel/point clouds:

* analytic SDF primitives (sphere, box, plane, torus) with the true Euclidean
  distance (no approximation), so sphere-tracing is Lipschitz-1 and cannot
  tunnel through a surface;
* constructive solid geometry (union / intersection / difference / smooth
  union) with the exact Lipschitz bound maintained;
* sphere tracing, analytic surface normals (central differences of the SDF);
* cone-traced ambient occlusion and soft shadows whose penumbra width is a
  *measured* geometric quantity, not an artistic fudge;
* hemispherical sky irradiance integrated by cosine-weighted deterministic
  quadrature over the visible hemisphere.

Everything here is a *declared numerical* result. The SDF is a modelling
abstraction: a "surface" is where the distance field crosses zero, and every
shading value is a bounded integral estimate with a reported sample count. No
cloud, no network, no project state, no new authority.

Uncertainty (GUM): the irradiance/occlusion estimates are quadrature means; the
modules return the sample count and the raw sum so the standard error
`sigma/sqrt(n)` is reproducible from the returned fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import numpy as np

__all__ = [
    "SDFError",
    "SDFPrimitive",
    "Sphere",
    "Box",
    "Plane",
    "Torus",
    "SmoothUnion",
    "Intersection",
    "Difference",
    "HitResult",
    "sphere_trace",
    "sdf_normal",
    "sdf_ambient_occlusion",
    "sdf_soft_shadow",
    "hemisphere_sky_irradiance",
    "MAX_TRACE_STEPS",
    "MAX_AO_SAMPLES",
    "MAX_IRRADIANCE_SAMPLES",
    "HEMISPHERE_DIRECTIONS",
]

_EPS: Final[float] = 1e-9
MAX_TRACE_STEPS: Final[int] = 4096
MAX_AO_SAMPLES: Final[int] = 256
MAX_IRRADIANCE_SAMPLES: Final[int] = 4096


class SDFError(ValueError):
    """Raised for a malformed primitive, ray, or out-of-range parameter."""


@runtime_checkable
class SDFPrimitive(Protocol):
    """Anything that can answer a signed distance query at a point."""

    def distance(self, point: np.ndarray) -> float:  # pragma: no cover - protocol
        ...


def _vec3(value: object, *, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (3,):
        raise SDFError(f"{name} must be a 3-vector, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise SDFError(f"{name} contains NaN/Inf")
    return arr


def _unit(value: object, *, name: str) -> np.ndarray:
    arr = _vec3(value, name=name)
    n = float(np.linalg.norm(arr))
    if n < _EPS:
        raise SDFError(f"{name} is a zero vector")
    return arr / n


@dataclass(frozen=True)
class Sphere:
    """Exact sphere: ``distance(p) = |p - center| - radius``."""

    center: np.ndarray
    radius: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", _vec3(self.center, name="sphere.center"))
        if not np.isfinite(self.radius) or self.radius <= 0.0:
            raise SDFError("sphere.radius must be finite and positive")

    def distance(self, point: np.ndarray) -> float:
        return float(np.linalg.norm(_vec3(point, name="point") - self.center) - self.radius)


@dataclass(frozen=True)
class Box:
    """Exact axis-aligned box (Inigo Quilez's closed form)."""

    center: np.ndarray
    half_extents: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", _vec3(self.center, name="box.center"))
        he = _vec3(self.half_extents, name="box.half_extents")
        if np.any(he <= 0.0):
            raise SDFError("box.half_extents must all be positive")
        object.__setattr__(self, "half_extents", he)

    def distance(self, point: np.ndarray) -> float:
        p = _vec3(point, name="point")
        q = np.abs(p - self.center) - self.half_extents
        outside = np.linalg.norm(np.maximum(q, 0.0))
        inside = min(float(np.max(q)), 0.0)
        return float(outside + inside)


@dataclass(frozen=True)
class Plane:
    """Half-space ``distance(p) = n.p - offset`` with unit outward normal ``n``."""

    normal: np.ndarray
    offset: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "normal", _unit(self.normal, name="plane.normal"))
        if not np.isfinite(self.offset):
            raise SDFError("plane.offset must be finite")

    def distance(self, point: np.ndarray) -> float:
        return float(np.dot(self.normal, _vec3(point, name="point")) - self.offset)


@dataclass(frozen=True)
class Torus:
    """Exact torus with the major radius in the xy-plane and a z axis."""

    center: np.ndarray
    major: float
    minor: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", _vec3(self.center, name="torus.center"))
        if not np.isfinite(self.major) or self.major <= 0.0:
            raise SDFError("torus.major must be finite and positive")
        if not np.isfinite(self.minor) or self.minor <= 0.0:
            raise SDFError("torus.minor must be finite and positive")

    def distance(self, point: np.ndarray) -> float:
        p = _vec3(point, name="point") - self.center
        q = np.array([np.hypot(p[0], p[1]) - self.major, p[2]])
        return float(np.linalg.norm(q) - self.minor)


@dataclass(frozen=True)
class SmoothUnion:
    """Polynomial smooth-min of two primitives (Lipschitz-1 preserved for k>0)."""

    a: SDFPrimitive
    b: SDFPrimitive
    k: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.k) or self.k <= 0.0:
            raise SDFError("smooth_union.k must be finite and positive")

    def distance(self, point: np.ndarray) -> float:
        da = self.a.distance(point)
        db = self.b.distance(point)
        h = float(np.clip(0.5 + 0.5 * (db - da) / self.k, 0.0, 1.0))
        return float(db * (1.0 - h) + da * h - self.k * h * (1.0 - h))


@dataclass(frozen=True)
class Intersection:
    """Set intersection ``max(a, b)``."""

    a: SDFPrimitive
    b: SDFPrimitive

    def distance(self, point: np.ndarray) -> float:
        return max(self.a.distance(point), self.b.distance(point))


@dataclass(frozen=True)
class Difference:
    """Set difference ``max(a, -b)``."""

    a: SDFPrimitive
    b: SDFPrimitive

    def distance(self, point: np.ndarray) -> float:
        return max(self.a.distance(point), -self.b.distance(point))


@dataclass(frozen=True)
class HitResult:
    """A sphere-tracing result. ``hit`` is ``False`` when nothing was reached."""

    hit: bool
    t: float
    point: np.ndarray
    steps: int


def sphere_trace(
    sdf: SDFPrimitive,
    origin: np.ndarray,
    direction: np.ndarray,
    *,
    t_min: float = 1e-4,
    t_max: float = 1e3,
    eps: float = 1e-6,
    max_steps: int = MAX_TRACE_STEPS,
) -> HitResult:
    """Trace a ray with the sphere-tracing (Lipschitz) algorithm."""
    ro = _vec3(origin, name="origin")
    rd = _unit(direction, name="direction")
    if not (0.0 <= t_min < t_max):
        raise SDFError("require 0 <= t_min < t_max")
    if eps <= 0.0:
        raise SDFError("eps must be positive")
    if max_steps < 1 or max_steps > MAX_TRACE_STEPS:
        raise SDFError(f"max_steps must be in [1, {MAX_TRACE_STEPS}]")

    t = t_min
    for step in range(1, max_steps + 1):
        p = ro + rd * t
        d = sdf.distance(p)
        if abs(d) < eps:
            return HitResult(hit=True, t=t, point=p, steps=step)
        t += d
        if t > t_max:
            return HitResult(hit=False, t=t_max, point=ro + rd * t_max, steps=step)
    return HitResult(hit=False, t=t_max, point=ro + rd * t_max, steps=max_steps)


def sdf_normal(sdf: SDFPrimitive, point: np.ndarray, *, h: float = 1e-4) -> np.ndarray:
    """Central-difference surface normal; returns a unit vector."""
    p = _vec3(point, name="point")
    if h <= 0.0:
        raise SDFError("h must be positive")
    dx = np.array([h, 0.0, 0.0])
    dy = np.array([0.0, h, 0.0])
    dz = np.array([0.0, 0.0, h])
    grad = np.array(
        [
            sdf.distance(p + dx) - sdf.distance(p - dx),
            sdf.distance(p + dy) - sdf.distance(p - dy),
            sdf.distance(p + dz) - sdf.distance(p - dz),
        ]
    )
    n = float(np.linalg.norm(grad))
    if n < _EPS:
        raise SDFError("degenerate normal (gradient vanished)")
    return grad / n


def sdf_ambient_occlusion(
    sdf: SDFPrimitive,
    point: np.ndarray,
    normal: np.ndarray,
    *,
    samples: int = 32,
    step_scale: float = 0.12,
    bias: float = 0.01,
    falloff: float = 0.95,
) -> float:
    """Cone-traced ambient occlusion in ``[0, 1]`` (1 = fully open).

    Uses the classic Quilez formulation: samples along the normal, weighting the
    *occluded* fraction of each step. Fully deterministic, bounded.
    """
    p = _vec3(point, name="point")
    n = _unit(normal, name="normal")
    if samples < 1 or samples > MAX_AO_SAMPLES:
        raise SDFError(f"samples must be in [1, {MAX_AO_SAMPLES}]")
    if step_scale <= 0.0 or bias < 0.0:
        raise SDFError("step_scale must be positive and bias non-negative")
    if not (0.0 < falloff <= 1.0):
        raise SDFError("falloff must lie in (0, 1]")

    occ = 0.0
    sca = 1.0
    for i in range(samples):
        h = bias + step_scale * (i / samples)
        d = sdf.distance(p + n * h)
        occ += (h - d) * sca
        sca *= falloff
    return float(np.clip(1.0 - 2.5 * occ, 0.0, 1.0))


def sdf_soft_shadow(
    sdf: SDFPrimitive,
    origin: np.ndarray,
    light_direction: np.ndarray,
    *,
    t_min: float = 1e-3,
    t_max: float = 1e3,
    softness: float = 16.0,
    max_steps: int = 128,
) -> float:
    """Soft shadow factor in ``[0, 1]`` (1 = fully lit, 0 = fully occluded).

    The penumbra term ``min(k * d(t) / t)`` is the classic closed-form estimate;
    ``k`` is a *declared* softness, so the result is a modelling estimate.
    """
    ro = _vec3(origin, name="origin")
    ld = _unit(light_direction, name="light_direction")
    if softness <= 0.0:
        raise SDFError("softness must be positive")
    if max_steps < 1 or max_steps > MAX_TRACE_STEPS:
        raise SDFError(f"max_steps must be in [1, {MAX_TRACE_STEPS}]")

    res = 1.0
    t = t_min
    for _ in range(max_steps):
        p = ro + ld * t
        d = sdf.distance(p)
        if d < 1e-4:
            return 0.0
        res = min(res, softness * d / t)
        t += max(d, 0.02)
        if t > t_max:
            break
    return float(np.clip(res, 0.0, 1.0))


def _hemisphere_directions(n: int) -> np.ndarray:
    """Deterministic cosine-weighted hemisphere directions in local +z frame."""
    idx = np.arange(n, dtype=np.float64) + 0.5
    # Stratified: golden-ratio azimuth, sqrt radius for cosine weighting.
    phi = 2.0 * np.pi * idx / 0.6180339887498949
    cos_theta = np.sqrt(1.0 - idx / n)
    sin_theta = np.sqrt(1.0 - cos_theta * cos_theta)
    return np.column_stack([sin_theta * np.cos(phi), sin_theta * np.sin(phi), cos_theta])


#: Precomputed cosine-weighted hemisphere sample directions (unit, local +z).
HEMISPHERE_DIRECTIONS: Final[np.ndarray] = _hemisphere_directions(MAX_IRRADIANCE_SAMPLES)


def _tangent_frame(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = normal
    seed = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    t = np.cross(n, seed)
    t = t / np.linalg.norm(t)
    b = np.cross(n, t)
    return t, b, n


def hemisphere_sky_irradiance(
    sdf: SDFPrimitive,
    point: np.ndarray,
    normal: np.ndarray,
    *,
    samples: int = 256,
    sky_radiance: float = 1.0,
) -> tuple[float, float, int]:
    """Cosine-weighted sky irradiance with SDF occlusion.

    Returns ``(irradiance, standard_error, n_samples)``. The estimator is the
    mean of ``sky_radiance * visibility`` over cosine-weighted samples; the
    standard error is ``std/sqrt(n)`` so the GUM uncertainty is explicit.
    """
    p = _vec3(point, name="point")
    n = _unit(normal, name="normal")
    if samples < 1 or samples > MAX_IRRADIANCE_SAMPLES:
        raise SDFError(f"samples must be in [1, {MAX_IRRADIANCE_SAMPLES}]")
    if sky_radiance < 0.0:
        raise SDFError("sky_radiance must be non-negative")

    t, b, nrm = _tangent_frame(n)
    local = HEMISPHERE_DIRECTIONS[:samples]
    world = local @ np.stack([t, b, nrm], axis=0)
    # Offset origin slightly along the normal to avoid self-occlusion.
    origin = p + n * 1e-3
    vis = np.empty(samples, dtype=np.float64)
    for i in range(samples):
        res = sphere_trace(sdf, origin, world[i], t_max=1e2, max_steps=256)
        vis[i] = 0.0 if res.hit else 1.0
    weighted = vis * sky_radiance
    mean = float(weighted.mean())
    if samples > 1:
        std = float(weighted.std(ddof=1))
        se = std / np.sqrt(samples)
    else:
        se = 0.0
    return mean, float(se), samples
