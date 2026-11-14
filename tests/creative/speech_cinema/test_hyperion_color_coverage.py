"""Branch coverage for color_imaging and the color_ops registrar."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci
from nexus_ai_agent.creative.speech_cinema import color_ops
from nexus_ai_agent.creative.speech_cinema.color_contract import OPERATIONS


def test_rgb_guard_rejects_non_finite_and_wrong_channel_count() -> None:
    with pytest.raises(ValueError, match="finite"):
        ci.white_balance_chromatic_adaptation(np.array([[[np.nan, 0.1, 0.1]]]))
    with pytest.raises(ValueError, match="RGB dimension"):
        ci.hdr_to_sdr_filmic_tonemap(np.ones((2, 2, 4)))


def test_white_balance_rejects_unknown_method_and_missing_kelvin() -> None:
    img = np.ones((2, 2, 3))
    with pytest.raises(ValueError, match="method must be"):
        ci.white_balance_chromatic_adaptation(img, method="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="kelvin is required"):
        ci.white_balance_chromatic_adaptation(img, method="kelvin")


def test_gray_world_refuses_non_positive_channel_means() -> None:
    img = np.zeros((2, 2, 3))
    with pytest.raises(ValueError, match="strictly positive"):
        ci.white_balance_chromatic_adaptation(img)


def test_explicit_kelvin_selects_the_black_body_path_under_gray_world() -> None:
    img = np.random.default_rng(1).random((4, 4, 3)) + 0.1
    out = ci.white_balance_chromatic_adaptation(img, kelvin=3200.0)
    assert out.shape == img.shape
    assert np.isfinite(out).all()


def test_black_body_range_boundaries_are_inclusive() -> None:
    ci._temperature_white_point(2000.0)
    ci._temperature_white_point(2222.0)
    ci._temperature_white_point(4000.0)
    ci._temperature_white_point(15000.0)
    with pytest.raises(ValueError):
        ci._temperature_white_point(float("nan"))


def test_filmic_tonemap_rejects_bad_exposure_and_white_point() -> None:
    hdr = np.ones((1, 1, 3))
    with pytest.raises(ValueError, match="exposure"):
        ci.hdr_to_sdr_filmic_tonemap(hdr, exposure=float("inf"))
    with pytest.raises(ValueError, match="white"):
        ci.hdr_to_sdr_filmic_tonemap(hdr, white=0.0)


def test_filmic_tonemap_keeps_zero_luminance_pixels_at_zero() -> None:
    out = ci.hdr_to_sdr_filmic_tonemap(np.zeros((2, 2, 3)))
    assert np.array_equal(out, np.zeros((2, 2, 3)))


def test_spd_validation_rejects_bad_shape_and_non_finite_entries() -> None:
    with pytest.raises(ValueError, match="3x3"):
        ci.validate_spd_covariance(np.eye(2))
    with pytest.raises(ValueError, match="3x3"):
        ci.validate_spd_covariance(np.full((3, 3), np.nan))


def test_matrix_sqrt_round_trip_and_inverse_square_root() -> None:
    cov = np.array([[2.0, 0.3, 0.1], [0.3, 1.5, 0.2], [0.1, 0.2, 1.0]])
    root = ci.matrix_sqrt_spd(cov)
    inv_root = ci.matrix_sqrt_spd(cov, inverse=True)
    np.testing.assert_allclose(root @ root, cov, atol=1e-10)
    np.testing.assert_allclose(root @ inv_root, np.eye(3), atol=1e-10)


def test_ot_match_requires_at_least_two_samples_per_side() -> None:
    with pytest.raises(ValueError, match="at least two"):
        ci.shot_to_shot_color_match_ot(np.ones((1, 1, 3)), np.ones((4, 4, 3)))


def test_png_round_trip_and_verification(tmp_path) -> None:
    img = np.random.default_rng(3).random((8, 8, 3))
    path = tmp_path / "color.png"
    ci.save_color_png(img, path)
    assert ci.verify_color_png(path)
    loaded = ci.load_color_png(path)
    assert loaded.shape == img.shape
    assert np.max(np.abs(loaded - np.rint(img * 255) / 255)) < 1e-9


def test_png_save_refuses_values_outside_the_normalised_range(tmp_path) -> None:
    with pytest.raises(ValueError, match="0..1"):
        ci.save_color_png(np.full((2, 2, 3), 1.5), tmp_path / "bad.png")


def test_verify_png_returns_false_for_non_images(tmp_path) -> None:
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    assert ci.verify_color_png(junk) is False
    assert ci.verify_color_png(tmp_path / "missing.png") is False


def test_compatibility_aliases_point_at_the_canonical_kernels() -> None:
    assert ci.white_balance is ci.white_balance_chromatic_adaptation
    assert ci.tonemap_hdr_to_sdr is ci.hdr_to_sdr_filmic_tonemap
    assert ci.color_match_ot is ci.shot_to_shot_color_match_ot
    assert ci.save_png is ci.save_color_png
    assert ci.load_png is ci.load_color_png
    assert ci.verify_png is ci.verify_color_png


def test_registrar_binds_every_declared_operation() -> None:
    table = color_ops.bind_operations()
    assert tuple(table) == OPERATIONS
    assert all(callable(fn) for fn in table.values())


def test_registrar_fails_closed_on_a_missing_or_extra_kernel() -> None:
    kernels = dict(color_ops.HANDLERS)
    kernels.pop("save_color_png")
    kernels["rogue"] = lambda: None
    with pytest.raises(ValueError, match="missing=.*save_color_png.*extra=.*rogue"):
        color_ops.bind_operations(kernels=kernels)
