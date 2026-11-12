"""Tests for the ten portrait-imaging kernels (Wave 4)."""

from __future__ import annotations

import numpy as np
import pytest
from portrait_fixtures import depth, image, region

from nexus_ai_agent.creative.composition import portrait_imaging as p
from nexus_ai_agent.creative.packs.portrait.errors import (
    DegenerateWarpError,
    InvalidImageError,
    InvalidRegionError,
)


def test_validate_image_rejects_shape_and_nan() -> None:
    with pytest.raises(InvalidImageError):
        p.validate_image(np.zeros((4, 4), np.float32))
    bad = np.zeros((4, 4, 3), np.float32)
    bad[0, 0, 0] = np.nan
    with pytest.raises(InvalidImageError):
        p.validate_image(bad)


def test_validate_region_rejects_mismatch_constant_nan() -> None:
    with pytest.raises(InvalidRegionError):
        p.validate_region(np.zeros((5, 5), np.float32), (4, 4))
    with pytest.raises(InvalidRegionError):
        p.validate_region(np.ones((4, 4), np.float32), (4, 4))
    bad = np.ones((4, 4), np.float32)
    bad[0, 0] = np.nan
    with pytest.raises(InvalidRegionError):
        p.validate_region(bad, (4, 4))


def test_validate_region_flattens_3d() -> None:
    m = np.zeros((4, 4, 1), np.float32)
    m[0, 0, 0] = 1.0
    assert p.validate_region(m, (4, 4)).shape == (4, 4)


def test_lab_round_trip() -> None:
    img = image(0)
    assert np.allclose(p.lab_to_srgb(p.srgb_to_lab(img)), img, atol=2e-3)


def test_gaussian_blur_zero_sigma() -> None:
    img = image(1)
    assert np.allclose(p.gaussian_blur(img, 0.0), img)
    assert p.gaussian_blur(img, 2.0).shape == img.shape


def test_gaze_redirection_and_strength_bounds() -> None:
    img = image(2)
    m = region()
    out = p.gaze_redirection(img, m, target=(0.3, 0.2), strength=0.5)
    assert out.shape == img.shape
    with pytest.raises(InvalidRegionError):
        p.gaze_redirection(img, m, target=(0, 0), strength=2.0)


def test_eyeblink_zero_phase_noop_and_closure() -> None:
    img = image(3)
    m = region()
    assert np.allclose(p.eyeblink_synthesis(img, m, phase=0.0), img)
    closed = p.eyeblink_synthesis(img, m, phase=0.5)
    assert not np.allclose(closed, img)
    with pytest.raises(InvalidRegionError):
        p.eyeblink_synthesis(img, m, phase=1.5)


def test_facial_relighting_changes_with_light() -> None:
    img = image(4)
    a = p.facial_relighting_gradient(img, light_azimuth_deg=-60)
    b = p.facial_relighting_gradient(img, light_azimuth_deg=60)
    assert not np.allclose(a, b)
    with pytest.raises(InvalidImageError):
        p.facial_relighting_gradient(img, ambient=2.0)


def test_skintone_harmonization() -> None:
    img = image(5)
    m = region()
    out = p.skintone_harmonization(img, m, strength=0.5)
    assert out.shape == img.shape
    with pytest.raises(InvalidRegionError):
        p.skintone_harmonization(img, m, strength=-0.1)


def test_micro_expression_morph_tps_and_affine_fallback() -> None:
    img = image(6)
    tps = p.micro_expression_morph(img, ((5, 5, 6, 6), (15, 5, 14, 6), (10, 15, 10, 16)))
    assert tps.shape == img.shape
    fallback = p.micro_expression_morph(img, ((5, 5, 6, 6),))
    assert fallback.shape == img.shape
    with pytest.raises(InvalidRegionError):
        p.micro_expression_morph(img, ())


def test_micro_expression_foldover_refused() -> None:
    img = image(6, size=32)
    # extreme opposing displacement -> non-positive Jacobian
    pts = (
        (8.36, 1.27, 0.51, 25.21),
        (28.30, 18.81, 22.61, 16.85),
        (28.99, 25.29, 0.08, 26.58),
        (1.04, 22.62, 5.45, 26.76),
        (16.79, 9.29, 13.10, 0.88),
    )
    with pytest.raises(DegenerateWarpError):
        p.micro_expression_morph(img, pts)


def test_teeth_whitening() -> None:
    img = image(7)
    m = region()
    out = p.teeth_whitening(img, m, strength=0.5)
    assert out.shape == img.shape
    assert float(out.mean()) >= float(img.mean()) - 1e-6
    with pytest.raises(InvalidRegionError):
        p.teeth_whitening(img, m, strength=2.0)


def test_facial_hair_continuity_density() -> None:
    img = np.full((24, 24, 3), 0.6, np.float32)
    m = region()
    low = p.facial_hair_continuity(img, m, density=0.1)
    high = p.facial_hair_continuity(img, m, density=0.9)
    assert not np.allclose(low, high)
    with pytest.raises(InvalidRegionError):
        p.facial_hair_continuity(img, m, density=2.0)


def test_head_repose_range() -> None:
    img = image(8)
    out = p.head_repose_3d(img, yaw_deg=10, pitch_deg=-5)
    assert out.shape == img.shape
    with pytest.raises(InvalidRegionError):
        p.head_repose_3d(img, yaw_deg=30)


def test_depth_of_field_bokeh() -> None:
    img = image(9, size=32)
    d = depth(size=32)
    out = p.depth_of_field_bokeh(img, d, focus=0.5, blades=6)
    assert out.shape == img.shape
    with pytest.raises(InvalidImageError):
        p.depth_of_field_bokeh(img, np.zeros((5, 5), np.float32))
    with pytest.raises(InvalidImageError):
        p.depth_of_field_bokeh(img, d, blades=2)


def test_aging_deaging_direction() -> None:
    img = image(10)
    aged = p.aging_deaging_geometry(img, years=40, strength=0.5)
    young = p.aging_deaging_geometry(img, years=-40, strength=0.5)
    assert not np.allclose(aged, young)
    assert np.allclose(p.aging_deaging_geometry(img, years=0.0), img)
    with pytest.raises(InvalidImageError):
        p.aging_deaging_geometry(img, strength=3.0)


def test_bokeh_kernel_shape() -> None:
    k = p._bokeh_kernel(6, 6)
    assert k.shape == (13, 13)
    assert float(k.max()) == 1.0
