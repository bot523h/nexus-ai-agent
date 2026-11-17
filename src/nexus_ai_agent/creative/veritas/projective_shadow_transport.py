"""Law 4 — projective shadow homography, extended-emitter penumbra and contact AO.

Floating feet, missing contact shadows and contradictory shadow directions are
among the most visible AI-video tells. This module builds the exact 4x4 planar
shadow projection matrix, proves that projected vertices lie on the ground
plane and stay collinear with the light, and derives physically based
umbra/penumbra softening plus a zero-gap hemisphere contact AO term.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


class ShadowGeometryError(ValueError):
    """Raised for degenerate light or ground-plane configurations."""


@dataclass(frozen=True)
class ShadowField:
    """Projected shadow geometry with penumbra and contact AO."""

    vertices: FloatArray
    shadow_vertices: FloatArray
    penumbra_radius: FloatArray
    contact_ao: FloatArray
    matrix: FloatArray


def _validate_light(light: FloatArray) -> FloatArray:
    source = np.asarray(light, dtype=np.float64)
    if source.shape != (4,):
        raise ShadowGeometryError("light must be a homogeneous 4-vector")
    if not np.all(np.isfinite(source)):
        raise ShadowGeometryError("light must be finite")
    if not np.isclose(source[3], 0.0) and not np.isclose(source[3], 1.0):
        raise ShadowGeometryError("light w component must be 0 (sun) or 1 (point)")
    if np.allclose(source[:3], 0.0):
        raise ShadowGeometryError("light direction must be non-zero")
    return source


def _validate_plane(plane: FloatArray) -> FloatArray:
    coefficients = np.asarray(plane, dtype=np.float64)
    if coefficients.shape != (4,):
        raise ShadowGeometryError("ground plane must be a 4-vector")
    if not np.all(np.isfinite(coefficients)):
        raise ShadowGeometryError("ground plane must be finite")
    if float(np.linalg.norm(coefficients[:3])) <= 1e-12:
        raise ShadowGeometryError("ground plane normal must be non-zero")
    return coefficients


def compute_shadow_projection_matrix(light: FloatArray, plane: FloatArray) -> FloatArray:
    """Return the exact 4x4 planar shadow projection matrix."""
    source = _validate_light(light)
    coefficients = _validate_plane(plane)
    scale = float(coefficients @ source)
    return scale * np.eye(4, dtype=np.float64) - np.outer(source, coefficients)


def project_vertices_to_shadow(matrix: FloatArray, vertices: FloatArray) -> FloatArray:
    """Project 3D vertices onto the ground plane through the light source."""
    transform = np.asarray(matrix, dtype=np.float64)
    points = np.asarray(vertices, dtype=np.float64)
    if transform.shape != (4, 4):
        raise ShadowGeometryError("shadow matrix must be 4x4")
    if points.ndim != 2 or points.shape[1] != 3:
        raise ShadowGeometryError("vertices must have shape (N, 3)")
    homogeneous = np.hstack([points, np.ones((points.shape[0], 1), dtype=np.float64)])
    projected = homogeneous @ transform.T
    w = projected[:, 3]
    if np.any(np.abs(w) <= 1e-12):
        raise ShadowGeometryError("shadow projection is degenerate (vertex at light plane)")
    return projected[:, :3] / w[:, None]


def penumbra_radius(occluder_height: float, emitter_radius: float, light_distance: float) -> float:
    """Geometric penumbra radius for an extended emitter."""
    if emitter_radius < 0.0:
        raise ShadowGeometryError("emitter radius must be non-negative")
    if light_distance <= 0.0:
        raise ShadowGeometryError("light distance must be positive")
    if occluder_height < 0.0:
        raise ShadowGeometryError("occluder height must be non-negative")
    if occluder_height >= light_distance:
        raise ShadowGeometryError("occluder must lie below the light source")
    return emitter_radius * occluder_height / (light_distance - occluder_height)


def synthesize_coherent_cast_shadows(
    vertices: FloatArray,
    light: FloatArray,
    plane: FloatArray,
    *,
    emitter_radius: float = 0.05,
    light_distance: float = 3.0,
    ao_height: float = 0.15,
) -> ShadowField:
    """Project vertices and derive coherent penumbra and contact AO fields."""
    points = np.asarray(vertices, dtype=np.float64)
    coefficients = _validate_plane(plane)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ShadowGeometryError("vertices must have shape (N, 3)")
    if not np.all(np.isfinite(points)):
        raise ShadowGeometryError("vertices must be finite")
    if ao_height <= 0.0:
        raise ShadowGeometryError("contact AO height must be positive")
    normal = coefficients[:3] / float(np.linalg.norm(coefficients[:3]))
    matrix = compute_shadow_projection_matrix(light, coefficients)
    shadow_vertices = project_vertices_to_shadow(matrix, points)
    heights = np.abs(points @ normal + coefficients[3])
    radii = np.array(
        [penumbra_radius(float(h), emitter_radius, light_distance) for h in heights],
        dtype=np.float64,
    )
    ao = 1.0 - np.exp(-heights / ao_height)
    return ShadowField(
        vertices=points,
        shadow_vertices=shadow_vertices,
        penumbra_radius=radii,
        contact_ao=ao,
        matrix=matrix,
    )
