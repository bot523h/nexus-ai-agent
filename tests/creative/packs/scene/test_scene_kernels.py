"""Tests for the ten scene-imaging kernels (Wave 3)."""

from __future__ import annotations

import numpy as np
import pytest
from scene_fixtures import depth, image, mask

from nexus_ai_agent.creative.composition import scene_imaging as k
from nexus_ai_agent.creative.packs.scene.errors import (
    DegenerateGeometryError,
    InvalidImageError,
    InvalidMaskError,
)


def test_validate_image_rejects_bad_shape() -> None:
    with pytest.raises(InvalidImageError):
        k.validate_image(np.zeros((4, 4), dtype=np.float32))
    with pytest.raises(InvalidImageError):
        k.validate_image(np.zeros((4, 4, 2), dtype=np.float32))


def test_validate_image_rejects_nan() -> None:
    bad = np.zeros((4, 4, 3), dtype=np.float32)
    bad[0, 0, 0] = np.nan
    with pytest.raises(InvalidImageError):
        k.validate_image(bad)


def test_validate_mask_rejects_mismatch_and_constant() -> None:
    with pytest.raises(InvalidMaskError):
        k.validate_mask(np.zeros((5, 5), dtype=np.float32), (4, 4))
    with pytest.raises(InvalidMaskError):
        k.validate_mask(np.ones((4, 4), dtype=np.float32), (4, 4))


def test_validate_mask_rejects_nan() -> None:
    bad = np.ones((4, 4), dtype=np.float32)
    bad[0, 0] = np.nan
    with pytest.raises(InvalidMaskError):
        k.validate_mask(bad, (4, 4))


def test_gaussian_blur_preserves_mean_and_zero_sigma() -> None:
    img = image()
    assert np.allclose(k.gaussian_blur(img, 0.0), img)
    blurred = k.gaussian_blur(img, 2.0)
    assert blurred.shape == img.shape
    assert abs(float(blurred.mean()) - float(img.mean())) < 0.05


def test_horizon_stabilization_deterministic() -> None:
    img = image(3)
    a, angle_a = k.horizon_stabilization(img)
    b, angle_b = k.horizon_stabilization(img)
    assert angle_a == angle_b
    assert np.array_equal(a, b)


def test_homography_identity_round_trip() -> None:
    img = image(5, size=32)
    corners = [[0, 0], [31, 0], [31, 31], [0, 31]]
    rect, hmat = k.homography_perspective_rectification(img, corners)
    assert rect.shape == img.shape
    assert hmat.shape == (3, 3)
    assert np.allclose(rect, img, atol=1e-4)


def test_homography_degenerate_refused() -> None:
    img = image(5, size=16)
    degenerate = [[8, 8], [8, 8], [8, 8], [8, 8]]
    with pytest.raises(DegenerateGeometryError):
        k.homography_perspective_rectification(img, degenerate)


def test_perlin_camera_shake_deterministic_and_bounded() -> None:
    a = k.perlin_camera_shake((16, 16), seed=7, amplitude=1.0)
    b = k.perlin_camera_shake((16, 16), seed=7, amplitude=1.0)
    assert np.array_equal(a, b)
    assert a.shape == (16, 16, 2)
    with pytest.raises(InvalidImageError):
        k.perlin_camera_shake((16, 16), octaves=0)


def test_fraunhofer_lens_flare_adds_light() -> None:
    img = image(2, size=32)
    out = k.fraunhofer_lens_flare(img, light_xy=(16, 16))
    assert out.shape == img.shape
    assert float(out.sum()) >= float(img.sum())


def test_wire_and_rig_removal_inpaints() -> None:
    img = image(4)
    m = mask()
    out = k.wire_and_rig_removal(img, m, iters=50)
    assert out.shape == img.shape
    # the hole is smoothed towards its surroundings
    assert not np.array_equal(out, img)


def test_wire_and_rig_removal_empty_mask_noop() -> None:
    img = image(4)
    m = np.zeros((24, 24), dtype=np.float32)
    m[0, 0] = 1.0
    m[1, 1] = 0.5
    out = k.wire_and_rig_removal(img, m, iters=5)
    assert out.shape == img.shape


def test_dynamic_range_expansion_normalised() -> None:
    img = image(6)
    out = k.dynamic_range_expansion(img, gain=2.0)
    assert 0.0 <= float(out.max()) <= 1.0 + 1e-6
    with pytest.raises(InvalidImageError):
        k.dynamic_range_expansion(img, gain=0.0)


def test_optical_shadow_synthesis_darkens() -> None:
    img = np.ones((24, 24, 3), dtype=np.float32)
    a = mask()
    out = k.optical_shadow_synthesis(img, a, light_vec=(1.0, 1.0))
    assert float(out.mean()) < 1.0


def test_defocus_depth_map_in_unit_range() -> None:
    img = image(8)
    out = k.defocus_depth_map_estimation(img)
    assert out.shape == (24, 24)
    assert 0.0 <= float(out.min()) and float(out.max()) <= 1.0


def test_sky_replacement_harmonization_changes_sky() -> None:
    img = image(9)
    m = mask()
    sky = np.zeros((12, 12, 3), dtype=np.float32)
    sky[..., 2] = 0.8
    out = k.sky_replacement_harmonization(img, m, sky)
    assert out.shape == img.shape
    assert float(out[m > 0.5].mean()) != pytest.approx(float(img[m > 0.5].mean()))


def test_motion_vector_flow_zero_for_identical() -> None:
    img = image(10, size=16)
    flow = k.motion_vector_flow(img, img, window=5, max_shift=2)
    assert flow.shape == (16, 16, 2)
    assert np.allclose(flow, 0.0)


def test_motion_vector_flow_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        k.motion_vector_flow(image(size=16), image(size=20))


def test_insert_object_with_shadow() -> None:
    img = image(11)
    obj = np.ones((24, 24, 3), dtype=np.float32) * 0.9
    m = mask()
    out = k.insert_object_with_shadow(img, obj, m, offset=(2, 2))
    assert out.shape == img.shape
    with pytest.raises(InvalidImageError):
        k.insert_object_with_shadow(img, np.ones((10, 10, 3), np.float32), m)


def test_remove_object_inpaint() -> None:
    img = image(12, size=32)
    m = mask(size=32, box=(8, 8, 24, 24))
    out = k.remove_object_inpaint(img, m, iters=40)
    assert out.shape == img.shape


def test_outpaint_canvas_expand() -> None:
    img = image(13, size=16)
    out = k.outpaint_canvas_expand(img, pad=4)
    assert out.shape == (24, 24, 3)
    assert np.allclose(out[4:20, 4:20], img)
    with pytest.raises(InvalidImageError):
        k.outpaint_canvas_expand(img, pad=0)


def test_synthesize_depth_of_field() -> None:
    img = image(14, size=32)
    d = depth(size=32)
    out = k.synthesize_depth_of_field(img, d, focus=0.5)
    assert out.shape == img.shape
    with pytest.raises(InvalidImageError):
        k.synthesize_depth_of_field(img, np.zeros((10, 10), np.float32))


def test_add_volumetric_fog_haze() -> None:
    img = image(15, size=32)
    d = depth(size=32)
    out = k.add_volumetric_fog_haze(img, d, beta=0.5, g=0.6)
    assert out.shape == img.shape
    assert 0.0 <= float(out.min()) and float(out.max()) <= 1.0
    with pytest.raises(InvalidImageError):
        k.add_volumetric_fog_haze(img, d, beta=-1.0)
    with pytest.raises(InvalidImageError):
        k.add_volumetric_fog_haze(img, d, g=2.0)


def test_lens_distortion_identity_and_correction() -> None:
    img = image(16, size=32)
    identity = k.lens_distortion_correct(img, k1=0.0)
    assert np.allclose(identity, img, atol=1e-4)
    corrected = k.lens_distortion_correct(img, k1=0.1)
    assert corrected.shape == img.shape


def test_chromatic_aberration_identity_and_shift() -> None:
    img = image(17, size=32)
    identity = k.chromatic_aberration_control(img)
    assert np.allclose(identity, img, atol=1e-4)
    shifted = k.chromatic_aberration_control(img, scales=(1.02, 1.0, 0.98))
    assert shifted.shape == img.shape


def test_perspective_horizon_level_refuses_steep() -> None:
    # a steep ramp (gradient ratio 2:1) yields a horizon tilt beyond the 45deg limit
    yy, xx = np.mgrid[0:32, 0:32]
    ramp = (2.0 * xx + yy).astype(np.float32)
    img = np.stack([ramp, ramp, ramp], axis=-1)
    with pytest.raises(DegenerateGeometryError):
        k.perspective_horizon_level(img)


def test_sky_replacement_relit() -> None:
    img = image(18)
    m = mask()
    sky = np.full((24, 24, 3), 0.5, dtype=np.float32)
    warm = k.sky_replacement_relit(img, m, sky, sky_temp_k=3000.0)
    cool = k.sky_replacement_relit(img, m, sky, sky_temp_k=9000.0)
    assert warm.shape == img.shape
    assert not np.allclose(warm, cool)


def test_reflection_water_glass() -> None:
    img = image(19, size=16)
    out = k.reflection_water_glass_synth(img, axis=0, fresnel=0.1, waves=1.0)
    assert out.shape == img.shape
    flat = k.reflection_water_glass_synth(img, fresnel=0.0)
    assert np.allclose(flat, img, atol=1e-6)


def test_validate_image_channel_count() -> None:
    with pytest.raises(InvalidImageError):
        k.validate_image(np.zeros((4, 4, 3), np.float32), channels=4)


def test_validate_mask_accepts_3d_and_flattens() -> None:
    m = np.zeros((4, 4, 1), np.float32)
    m[0, 0, 0] = 1.0
    out = k.validate_mask(m, (4, 4))
    assert out.shape == (4, 4)


def test_validate_mask_rejects_1d() -> None:
    with pytest.raises(InvalidMaskError):
        k.validate_mask(np.array([1.0, 0.0], np.float32), (2,))


def test_flat_image_has_zero_gradient_angle() -> None:
    flat = np.full((8, 8, 3), 0.5, np.float32)
    out, tilt = k.horizon_stabilization(flat)
    assert tilt == 0.0
    assert np.allclose(out, flat, atol=1e-5)


def test_homography_self_intersecting_quad() -> None:
    # a bow-tie quad is a valid input; the result stays finite and shaped
    img = image(1, size=8)
    quad = [[0, 0], [7, 0], [0, 7], [7, 7]]
    out, hmat = k.homography_perspective_rectification(img, quad)
    assert out.shape == img.shape
    assert hmat.shape == (3, 3)


def test_aperture_kernel_circular_when_few_sides() -> None:
    ap = k._aperture_kernel(4, 2)
    assert ap.shape == (9, 9)
    assert float(ap.max()) == 1.0


def test_wire_and_rig_removal_no_hole_noop() -> None:
    img = image(4)
    m = np.full((24, 24), 0.3, np.float32)
    m[0, 0] = 0.4  # max > min, but nothing above 0.5 -> no hole
    out = k.wire_and_rig_removal(img, m, iters=3)
    assert np.array_equal(out, img)


def test_defocus_depth_map_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        k.synthesize_depth_of_field(image(1), np.zeros((5, 5), np.float32))


def test_sky_relit_tiles_smaller_sky() -> None:
    img = image(3)
    m = mask()
    sky = np.full((8, 8, 3), 0.4, np.float32)
    out = k.sky_replacement_relit(img, m, sky)
    assert out.shape == img.shape


def test_sky_harmonization_tiles_smaller_sky() -> None:
    img = image(3)
    m = mask()
    sky = np.full((8, 8, 3), 0.4, np.float32)
    out = k.sky_replacement_harmonization(img, m, sky)
    assert out.shape == img.shape


def test_homography_singular_det_refused() -> None:
    img = image(1, size=8)
    # all four corners on one line -> the DLT solution is rank-deficient
    quad = [[0, 0], [1, 1], [2, 2], [3, 3]]
    with pytest.raises(DegenerateGeometryError):
        k.homography_perspective_rectification(img, quad)


def test_fog_depth_shape_mismatch() -> None:
    with pytest.raises(InvalidImageError):
        k.add_volumetric_fog_haze(image(1), np.zeros((5, 5), np.float32))
