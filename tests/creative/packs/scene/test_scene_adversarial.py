"""Adversarial input/refusal cases for the scene pack (Wave 3, Milestone F).

Every case asserts a typed refusal or a documented degenerate-but-bounded
result; none of them is a code-mutation test.
"""

from __future__ import annotations

import numpy as np
import pytest
from scene_fixtures import image, mask

from nexus_ai_agent.creative.composition import scene_imaging as k
from nexus_ai_agent.creative.packs.scene.errors import (
    DegenerateGeometryError,
    InvalidImageError,
    InvalidMaskError,
)


def test_refuse_non_3d_image() -> None:
    with pytest.raises(InvalidImageError):
        k.validate_image(np.zeros((8, 8), np.float32))


def test_refuse_two_channel_image() -> None:
    with pytest.raises(InvalidImageError):
        k.validate_image(np.zeros((8, 8, 2), np.float32))


def test_refuse_nan_image() -> None:
    img = np.zeros((8, 8, 3), np.float32)
    img[1, 1, 1] = np.inf
    with pytest.raises(InvalidImageError):
        k.validate_image(img)


def test_refuse_mask_shape_mismatch() -> None:
    with pytest.raises(InvalidMaskError):
        k.validate_mask(np.ones((5, 5), np.float32), (8, 8))


def test_refuse_constant_mask() -> None:
    with pytest.raises(InvalidMaskError):
        k.validate_mask(np.zeros((8, 8), np.float32), (8, 8))


def test_refuse_nan_mask() -> None:
    m = np.ones((8, 8), np.float32)
    m[0, 0] = np.nan
    with pytest.raises(InvalidMaskError):
        k.validate_mask(m, (8, 8))


def test_refuse_negative_gain_dr_expansion() -> None:
    with pytest.raises(InvalidImageError):
        k.dynamic_range_expansion(image(1), gain=-1.0)


def test_refuse_negative_fog_beta() -> None:
    with pytest.raises(InvalidImageError):
        k.add_volumetric_fog_haze(image(1), np.zeros((24, 24), np.float32), beta=-0.1)


def test_refuse_out_of_range_henyey_greenstein() -> None:
    with pytest.raises(InvalidImageError):
        k.add_volumetric_fog_haze(image(1), np.zeros((24, 24), np.float32), g=1.5)


def test_refuse_depth_shape_mismatch_dof() -> None:
    with pytest.raises(InvalidImageError):
        k.synthesize_depth_of_field(image(1), np.zeros((5, 5), np.float32))


def test_refuse_object_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        k.insert_object_with_shadow(image(1), np.ones((8, 8, 3), np.float32), mask())


def test_refuse_zero_pad_outpaint() -> None:
    with pytest.raises(InvalidImageError):
        k.outpaint_canvas_expand(image(1), pad=0)


def test_refuse_zero_octaves_shake() -> None:
    with pytest.raises(InvalidImageError):
        k.perlin_camera_shake((8, 8), octaves=0)


def test_refuse_flow_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        k.motion_vector_flow(image(1, size=16), image(1, size=24))


def test_refuse_singular_homography() -> None:
    with pytest.raises(DegenerateGeometryError):
        k.homography_perspective_rectification(image(1, size=8), [[0, 0], [1, 1], [2, 2], [3, 3]])


def test_refuse_steep_horizon() -> None:
    yy, xx = np.mgrid[0:32, 0:32]
    ramp = (2.0 * xx + yy).astype(np.float32)
    with pytest.raises(DegenerateGeometryError):
        k.perspective_horizon_level(np.stack([ramp] * 3, axis=-1))


def test_flow_clamped_when_subject_leaves_frame() -> None:
    a = image(1, size=16)
    b = np.roll(a, 8, axis=1)
    flow = k.motion_vector_flow(a, b, window=5, max_shift=4)
    assert np.all(np.abs(flow) <= 4)


def test_blur_is_bounded_on_constant_input() -> None:
    flat = np.full((16, 16, 3), 0.5, np.float32)
    out = k.gaussian_blur(flat, 4.0)
    assert np.allclose(out, flat, atol=1e-5)


def test_masks_are_clipped_not_rejected_in_range() -> None:
    m = np.full((16, 16), 2.0, np.float32)
    m[0, 0] = 0.0
    assert float(k.validate_mask(m, (16, 16)).max()) == 1.0


def test_kernels_do_not_mutate_input() -> None:
    img = image(1)
    before = img.copy()
    k.horizon_stabilization(img)
    k.dynamic_range_expansion(img)
    assert np.array_equal(img, before)
