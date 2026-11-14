"""Adversarial input/refusal cases for the portrait pack (Wave 4, Milestone F)."""

from __future__ import annotations

import numpy as np
import pytest
from portrait_fixtures import image, region

from nexus_ai_agent.creative.composition import portrait_imaging as p
from nexus_ai_agent.creative.packs.portrait.errors import (
    DegenerateWarpError,
    InvalidImageError,
    InvalidRegionError,
)


def test_refuse_non_3d_image() -> None:
    with pytest.raises(InvalidImageError):
        p.validate_image(np.zeros((8, 8), np.float32))


def test_refuse_nan_image() -> None:
    img = np.zeros((8, 8, 3), np.float32)
    img[0, 0, 0] = np.nan
    with pytest.raises(InvalidImageError):
        p.validate_image(img)


def test_refuse_region_shape_mismatch() -> None:
    with pytest.raises(InvalidRegionError):
        p.validate_region(np.ones((5, 5), np.float32), (8, 8))


def test_refuse_constant_region() -> None:
    with pytest.raises(InvalidRegionError):
        p.validate_region(np.zeros((8, 8), np.float32), (8, 8))


def test_refuse_nan_region() -> None:
    m = np.ones((8, 8), np.float32)
    m[0, 0] = np.nan
    with pytest.raises(InvalidRegionError):
        p.validate_region(m, (8, 8))


def test_refuse_gaze_strength_out_of_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.gaze_redirection(image(1), region(), target=(0.0, 0.0), strength=1.5)


def test_refuse_blink_phase_out_of_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.eyeblink_synthesis(image(1), region(), phase=-0.1)


def test_refuse_ambient_out_of_range() -> None:
    with pytest.raises(InvalidImageError):
        p.facial_relighting_gradient(image(1), ambient=-0.5)


def test_refuse_skintone_strength_out_of_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.skintone_harmonization(image(1), region(), strength=1.2)


def test_refuse_empty_control_points() -> None:
    with pytest.raises(InvalidRegionError):
        p.micro_expression_morph(image(1), ())


def test_refuse_foldover_warp() -> None:
    pts = (
        (8.36, 1.27, 0.51, 25.21),
        (28.30, 18.81, 22.61, 16.85),
        (28.99, 25.29, 0.08, 26.58),
        (1.04, 22.62, 5.45, 26.76),
        (16.79, 9.29, 13.10, 0.88),
    )
    with pytest.raises(DegenerateWarpError):
        p.micro_expression_morph(image(1, size=32), pts)


def test_refuse_teeth_strength_out_of_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.teeth_whitening(image(1), region(), strength=-0.1)


def test_refuse_beard_density_out_of_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.facial_hair_continuity(image(1), region(), density=1.5)


def test_refuse_head_repose_beyond_range() -> None:
    with pytest.raises(InvalidRegionError):
        p.head_repose_3d(image(1), pitch_deg=20.0)


def test_refuse_bokeh_depth_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        p.depth_of_field_bokeh(image(1), np.zeros((5, 5), np.float32))


def test_refuse_bokeh_too_few_blades() -> None:
    with pytest.raises(InvalidImageError):
        p.depth_of_field_bokeh(image(1), np.zeros((24, 24), np.float32), blades=2)


def test_refuse_aging_strength_out_of_range() -> None:
    with pytest.raises(InvalidImageError):
        p.aging_deaging_geometry(image(1), strength=2.0)


def test_kernels_do_not_mutate_input() -> None:
    img = image(1)
    before = img.copy()
    p.facial_relighting_gradient(img)
    p.teeth_whitening(img, region())
    assert np.array_equal(img, before)


def test_lab_round_trip_extremes() -> None:
    extremes = np.array([[[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]], dtype=np.float32)
    out = p.lab_to_srgb(p.srgb_to_lab(extremes))
    assert np.all(np.isfinite(out))
    assert 0.0 <= float(out.min()) and float(out.max()) <= 1.0


def test_zero_years_aging_is_identity() -> None:
    img = image(2)
    assert np.allclose(p.aging_deaging_geometry(img, years=0.0), img)
