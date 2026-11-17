"""Unit tests for Law 3 — catoptric reflections and Fresnel optics."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.catoptric_reflection_physics import (
    CatoptricGeometryError,
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
    verify_reflection_epipolar_consistency,
    warp_ripple_reflection,
)

NORMAL = np.array([0.0, 0.0, 1.0])
OFFSET = -2.0


def test_householder_matrix_is_involutive_and_orientation_reversing() -> None:
    matrix = compute_householder_reflection_pose(NORMAL, OFFSET)
    assert np.linalg.det(matrix[:3, :3]) == pytest.approx(-1.0)
    assert np.allclose(matrix @ matrix, np.eye(4))
    assert np.allclose(matrix[:3, 3], np.array([0.0, 0.0, 4.0]))


def test_householder_rejects_non_unit_normal() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_householder_reflection_pose(np.array([0.0, 0.0, 2.0]), OFFSET)
    with pytest.raises(CatoptricGeometryError):
        compute_householder_reflection_pose(np.zeros(2), OFFSET)
    with pytest.raises(CatoptricGeometryError):
        compute_householder_reflection_pose(np.array([np.nan, 0.0, 1.0]), OFFSET)


def test_reflection_epipolar_consistency() -> None:
    result = verify_reflection_epipolar_consistency(
        NORMAL, OFFSET, np.array([0.0, 0.0, 3.0]), np.array([1.0, 1.0, 1.0])
    )
    assert result.consistent
    assert result.plane_residual < 1e-9
    assert np.isclose(result.intersection[2], 2.0)


def test_parallel_virtual_ray_raises() -> None:
    with pytest.raises(CatoptricGeometryError):
        verify_reflection_epipolar_consistency(
            NORMAL, OFFSET, np.array([0.0, 0.0, 2.0]), np.array([1.0, 0.0, 2.0])
        )
    with pytest.raises(CatoptricGeometryError):
        verify_reflection_epipolar_consistency(
            NORMAL, OFFSET, np.zeros(2), np.array([1.0, 0.0, 2.0])
        )
    with pytest.raises(CatoptricGeometryError):
        verify_reflection_epipolar_consistency(
            NORMAL, OFFSET, np.array([np.nan, 0.0, 2.0]), np.array([1.0, 0.0, 2.0])
        )


def test_fresnel_brewster_angle_zeroes_p_reflectance() -> None:
    result = compute_fresnel_reflectance_rgb(np.array([56.3]), 1.0, 1.333)
    assert result.brewster_deg == pytest.approx(np.degrees(np.arctan2(1.333, 1.0)))
    assert result.reflectance_p[0] < 1e-3
    brewster = compute_fresnel_reflectance_rgb(result.brewster_deg, 1.0, 1.333)
    assert brewster.reflectance_p[0] == pytest.approx(0.0, abs=1e-9)
    assert brewster.reflectance_s[0] > 0.0


def test_fresnel_validation() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(45.0, 0.0, 1.333)
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(45.0, 1.0, -1.0)
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(-1.0, 1.0, 1.333)
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(91.0, 1.0, 1.333)


def test_ripple_warp_2d_and_3d() -> None:
    image = np.arange(64, dtype=np.float64).reshape(8, 8)
    warped = warp_ripple_reflection(image, 0.25)
    assert warped.shape == image.shape
    colour = np.stack([image, image, image], axis=-1)
    assert warp_ripple_reflection(colour, 0.5).shape == colour.shape


def test_ripple_validation() -> None:
    image = np.zeros((8, 8))
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(np.zeros(8), 0.0)
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(np.full((8, 8), np.nan), 0.0)
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(image, float("nan"))
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(image, 0.0, gravity=0.0)
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(image, 0.0, wavelength_m=0.0)
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(image, 0.0, amplitude_px=-1.0)
