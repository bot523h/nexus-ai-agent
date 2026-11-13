"""Unit tests for Law 6 — homography-locked diegetic typography."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    TypographyError,
    compute_planar_quad_homography,
    procedural_glyph_bitmap,
    render_homography_locked_diegetic_text,
)

UNIT_QUAD = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
SCREEN_QUAD = np.array([[10.0, 10.0], [90.0, 15.0], [85.0, 90.0], [15.0, 85.0]])


def test_glyph_bitmap_is_deterministic() -> None:
    first = procedural_glyph_bitmap("HELLO")
    second = procedural_glyph_bitmap("HELLO")
    assert np.array_equal(first, second)
    assert first.shape == (7, 29)
    assert set(np.unique(first)) == {0.0, 1.0}
    assert procedural_glyph_bitmap("").shape == (7, 1)


def test_glyph_bitmap_rejects_unsupported_characters() -> None:
    with pytest.raises(TypographyError):
        procedural_glyph_bitmap("HELLO!")
    with pytest.raises(TypographyError):
        procedural_glyph_bitmap(123)  # type: ignore[arg-type]


def test_homography_maps_quad_and_is_sl3() -> None:
    homography = compute_planar_quad_homography(UNIT_QUAD, SCREEN_QUAD)
    assert np.linalg.det(homography) == pytest.approx(1.0)
    for source, target in zip(UNIT_QUAD, SCREEN_QUAD, strict=True):
        projected = homography @ np.array([source[0], source[1], 1.0])
        projected = projected[:2] / projected[2]
        assert np.allclose(projected, target, atol=1e-9)


def test_homography_validation() -> None:
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(np.zeros((3, 2)), SCREEN_QUAD)
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(np.full((4, 2), np.nan), SCREEN_QUAD)
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(np.zeros((4, 2)), SCREEN_QUAD)
    collinear = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(UNIT_QUAD, collinear)


def test_render_is_visible_and_spelling_is_locked() -> None:
    render = render_homography_locked_diegetic_text("HELLO", SCREEN_QUAD, (100, 100))
    assert render.rendered_text == "HELLO"
    assert float(np.max(render.mask)) > 0.0
    assert render.mask.shape == (100, 100)
    assert np.linalg.det(render.homography) == pytest.approx(1.0)
    assert render.mean_footprint > 0.0


def test_render_illumination_scalar_and_array() -> None:
    scalar = render_homography_locked_diegetic_text("SIGN", SCREEN_QUAD, (64, 64), illumination=0.5)
    array = render_homography_locked_diegetic_text(
        "SIGN", SCREEN_QUAD, (64, 64), illumination=np.full((64, 64), 0.5)
    )
    assert np.allclose(scalar.image, array.image)


def test_render_validation() -> None:
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("SIGN", SCREEN_QUAD, (0, 64))
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text(
            "SIGN", SCREEN_QUAD, (64, 64), illumination=np.ones((10, 10))
        )
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("SIGN", SCREEN_QUAD, (64, 64), illumination=-1.0)
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("SIGN", SCREEN_QUAD, (64, 64), texture_shape=(4, 4))
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text(
            "A VERY LONG SIGN", SCREEN_QUAD, (64, 64), texture_shape=(32, 20)
        )
