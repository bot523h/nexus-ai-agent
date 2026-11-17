"""Law 3 — catoptric Householder reflections and dielectric Fresnel polarisation.

AI reflections in mirrors, windows, puddles and eyeglasses move the wrong way or
show impossible angles. This module constructs the exact affine Householder
reflection operator, proves virtual-camera epipolar consistency, evaluates the
Snell-Descartes / Fresnel s- and p-polarised reflectances (including Brewster's
angle), and warps planar reflections across dispersive capillary-gravity ripples.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

NORMAL_TOLERANCE = 1e-6


class CatoptricGeometryError(ValueError):
    """Raised for non-unit mirror normals or invalid optical media."""


@dataclass(frozen=True)
class ReflectionConsistency:
    """Epipolar consistency of a planar mirror reflection."""

    matrix: FloatArray
    virtual_camera: FloatArray
    virtual_landmark: FloatArray
    intersection: FloatArray
    plane_residual: float
    collinear: bool

    @property
    def consistent(self) -> bool:
        return self.plane_residual <= 1e-9 and self.collinear


@dataclass(frozen=True)
class FresnelResult:
    """Polarised dielectric reflectances at an interface."""

    theta_i_deg: FloatArray
    reflectance_s: FloatArray
    reflectance_p: FloatArray
    reflectance_unpolarised: FloatArray
    brewster_deg: float


def _validate_plane(normal: FloatArray, offset: float) -> FloatArray:
    n = np.asarray(normal, dtype=np.float64)
    if n.shape != (3,):
        raise CatoptricGeometryError("mirror normal must be a 3-vector")
    if not np.all(np.isfinite(n)) or not np.isfinite(offset):
        raise CatoptricGeometryError("mirror plane must be finite")
    norm = float(np.linalg.norm(n))
    if abs(norm - 1.0) > NORMAL_TOLERANCE:
        raise CatoptricGeometryError(f"mirror normal must be unit length, got {norm:.6f}")
    return n


def compute_householder_reflection_pose(normal: FloatArray, offset: float) -> FloatArray:
    """Return the exact 4x4 affine Householder reflection matrix."""
    n = _validate_plane(normal, offset)
    linear = np.eye(3, dtype=np.float64) - 2.0 * np.outer(n, n)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = linear
    matrix[:3, 3] = -2.0 * offset * n
    return matrix


def _apply(matrix: FloatArray, point: FloatArray) -> FloatArray:
    homogeneous = np.array([point[0], point[1], point[2], 1.0], dtype=np.float64)
    transformed = matrix @ homogeneous
    return np.asarray(transformed[:3] / transformed[3], dtype=np.float64)


def verify_reflection_epipolar_consistency(
    normal: FloatArray,
    offset: float,
    camera: FloatArray,
    landmark: FloatArray,
) -> ReflectionConsistency:
    """Verify that the virtual ray through a reflected landmark hits the mirror plane."""
    matrix = compute_householder_reflection_pose(normal, offset)
    cam = np.asarray(camera, dtype=np.float64)
    point = np.asarray(landmark, dtype=np.float64)
    if cam.shape != (3,) or point.shape != (3,):
        raise CatoptricGeometryError("camera and landmark must be 3-vectors")
    if not (np.all(np.isfinite(cam)) and np.all(np.isfinite(point))):
        raise CatoptricGeometryError("camera and landmark must be finite")
    virtual_camera = _apply(matrix, cam)
    virtual_landmark = _apply(matrix, point)
    direction = virtual_landmark - virtual_camera
    n = _validate_plane(normal, offset)
    denom = float(n @ direction)
    if abs(denom) < 1e-12:
        raise CatoptricGeometryError("virtual ray is parallel to the mirror plane")
    t = -(float(n @ virtual_camera) + offset) / denom
    intersection = virtual_camera + t * direction
    residual = abs(float(n @ intersection) + offset)
    cross = np.cross(direction, intersection - virtual_camera)
    collinear = float(np.linalg.norm(cross)) <= 1e-9 * max(1.0, float(np.linalg.norm(direction)))
    return ReflectionConsistency(
        matrix=matrix,
        virtual_camera=virtual_camera,
        virtual_landmark=virtual_landmark,
        intersection=intersection,
        plane_residual=residual,
        collinear=collinear,
    )


def _fresnel_amplitudes(
    theta_i_deg: FloatArray, n1: float, n2: float
) -> tuple[FloatArray, FloatArray]:
    theta_i = np.deg2rad(theta_i_deg)
    sin_t = (n1 / n2) * np.sin(theta_i)
    cos_t = np.sqrt(np.maximum(0.0, 1.0 - sin_t**2))
    cos_i = np.cos(theta_i)
    rs = (n1 * cos_i - n2 * cos_t) / (n1 * cos_i + n2 * cos_t)
    rp = (n2 * cos_i - n1 * cos_t) / (n2 * cos_i + n1 * cos_t)
    return rs, rp


def compute_fresnel_reflectance_rgb(
    theta_i_deg: FloatArray | float,
    n1: float,
    n2: float,
) -> FresnelResult:
    """Compute exact s/p Fresnel reflectances and the Brewster angle."""
    if not np.isfinite(n1) or not np.isfinite(n2) or n1 <= 0.0 or n2 <= 0.0:
        raise CatoptricGeometryError("refractive indices must be positive and finite")
    theta = np.atleast_1d(np.asarray(theta_i_deg, dtype=np.float64))
    if np.any(~np.isfinite(theta)) or np.any(theta < 0.0) or np.any(theta > 90.0):
        raise CatoptricGeometryError("incident angle must lie in [0, 90] degrees")
    rs, rp = _fresnel_amplitudes(theta, n1, n2)
    reflectance_s = rs**2
    reflectance_p = rp**2
    unpolarised = 0.5 * (reflectance_s + reflectance_p)
    brewster = float(np.degrees(np.arctan2(n2, n1)))
    return FresnelResult(
        theta_i_deg=theta,
        reflectance_s=reflectance_s,
        reflectance_p=reflectance_p,
        reflectance_unpolarised=unpolarised,
        brewster_deg=brewster,
    )


def _dispersion_omega(
    k: FloatArray, gravity: float, surface_tension: float, density: float, depth: float
) -> FloatArray:
    return np.sqrt((gravity * k + surface_tension / density * k**3) * np.tanh(k * depth))


def warp_ripple_reflection(
    image: FloatArray,
    time_s: float,
    *,
    gravity: float = 9.80665,
    surface_tension: float = 0.072,
    density: float = 1000.0,
    depth: float = 1.0,
    amplitude_px: float = 2.0,
    wavelength_m: float = 0.05,
) -> FloatArray:
    """Warp a planar reflection with dispersive capillary-gravity ripples."""
    source = np.asarray(image, dtype=np.float64)
    if source.ndim not in (2, 3):
        raise CatoptricGeometryError("image must be 2D or 3D")
    if not np.all(np.isfinite(source)):
        raise CatoptricGeometryError("image must be finite")
    if not np.isfinite(time_s):
        raise CatoptricGeometryError("time must be finite")
    if gravity <= 0.0 or surface_tension < 0.0 or density <= 0.0 or depth <= 0.0:
        raise CatoptricGeometryError("dispersion parameters must be positive")
    if wavelength_m <= 0.0 or amplitude_px < 0.0:
        raise CatoptricGeometryError("wavelength must be positive and amplitude non-negative")
    rows = source.shape[0]
    k = 2.0 * np.pi / wavelength_m
    omega = float(_dispersion_omega(np.array([k]), gravity, surface_tension, density, depth)[0])
    phase = omega * time_s
    row_index = np.arange(rows, dtype=np.float64)
    shift = amplitude_px * np.sin(k * row_index * 0.01 - phase)
    sample = np.clip(np.rint(row_index + shift), 0, rows - 1).astype(np.intp)
    return np.take(source, sample, axis=0)
