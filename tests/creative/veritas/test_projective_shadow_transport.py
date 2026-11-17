"""Unit tests for Law 4 — projective shadow transport."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    ShadowGeometryError,
    compute_shadow_projection_matrix,
    penumbra_radius,
    project_vertices_to_shadow,
    synthesize_coherent_cast_shadows,
)

LIGHT = np.array([0.0, 0.0, 5.0, 1.0])
PLANE = np.array([0.0, 0.0, 1.0, 0.0])
VERTICES = np.array([[0.0, 0.0, 2.0], [1.0, 0.0, 1.0]])


def test_shadow_matrix_projects_onto_plane_and_is_collinear() -> None:
    matrix = compute_shadow_projection_matrix(LIGHT, PLANE)
    shadow = project_vertices_to_shadow(matrix, VERTICES)
    assert np.allclose(shadow[:, 2], 0.0)
    for original, projected in zip(VERTICES, shadow, strict=True):
        cross = np.cross(original - LIGHT[:3], projected - LIGHT[:3])
        assert np.linalg.norm(cross) < 1e-9


def test_directional_light_is_supported() -> None:
    light = np.array([0.0, 0.0, 1.0, 0.0])
    matrix = compute_shadow_projection_matrix(light, PLANE)
    shadow = project_vertices_to_shadow(matrix, VERTICES)
    assert np.allclose(shadow[:, 2], 0.0)


def test_light_validation() -> None:
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.zeros(3), PLANE)
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.array([0.0, 0.0, 1.0, 2.0]), PLANE)
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.zeros(4), PLANE)
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.array([np.nan, 0.0, 1.0, 1.0]), PLANE)


def test_plane_validation() -> None:
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(LIGHT, np.zeros(3))
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(LIGHT, np.array([0.0, 0.0, 0.0, 0.0]))
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(LIGHT, np.array([0.0, 0.0, np.nan, 0.0]))


def test_projection_validation() -> None:
    matrix = compute_shadow_projection_matrix(LIGHT, PLANE)
    with pytest.raises(ShadowGeometryError):
        project_vertices_to_shadow(np.eye(3), VERTICES)
    with pytest.raises(ShadowGeometryError):
        project_vertices_to_shadow(matrix, np.zeros((2, 2)))
    # A vertex on the light plane makes the homogeneous divide singular.
    with pytest.raises(ShadowGeometryError):
        project_vertices_to_shadow(matrix, np.array([[0.0, 0.0, 5.0]]))


def test_penumbra_radius_softens_with_height() -> None:
    assert penumbra_radius(0.0, 0.05, 3.0) == pytest.approx(0.0)
    assert penumbra_radius(1.0, 0.05, 3.0) > penumbra_radius(0.5, 0.05, 3.0)
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(-1.0, 0.05, 3.0)
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(1.0, -0.05, 3.0)
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(1.0, 0.05, 0.0)
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(4.0, 0.05, 3.0)


def test_coherent_shadow_field() -> None:
    field = synthesize_coherent_cast_shadows(VERTICES, LIGHT, PLANE)
    assert field.shadow_vertices.shape == VERTICES.shape
    assert field.penumbra_radius[0] > field.penumbra_radius[1]
    assert field.contact_ao[0] > field.contact_ao[1]
    assert np.all((field.contact_ao >= 0.0) & (field.contact_ao <= 1.0))


def test_coherent_shadow_validation() -> None:
    with pytest.raises(ShadowGeometryError):
        synthesize_coherent_cast_shadows(np.zeros((2, 2)), LIGHT, PLANE)
    with pytest.raises(ShadowGeometryError):
        synthesize_coherent_cast_shadows(np.full((1, 3), np.nan), LIGHT, PLANE)
    with pytest.raises(ShadowGeometryError):
        synthesize_coherent_cast_shadows(VERTICES, LIGHT, PLANE, ao_height=0.0)
