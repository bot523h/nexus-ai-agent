"""WAVE 26 — tests for the ACES/HALD film optic module."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import aces_hald_film_optic as f

# --- ACES colour science ----------------------------------------------------------


def test_ap0_to_ap1_white_point_is_preserved() -> None:
    out = f.aces_ap0_to_acescg(np.array([1.0, 1.0, 1.0]))
    np.testing.assert_allclose(out, [1.0, 1.0, 1.0], atol=1e-6)


def test_ap0_to_ap1_matrix_rows_sum_to_one() -> None:
    np.testing.assert_allclose(f.AP0_TO_AP1.sum(axis=1), np.ones(3), atol=1e-6)


def test_aces_roundtrip_is_identity() -> None:
    rgb = np.array([0.2, 0.4, 0.6])
    back = f.acescg_to_ap0(f.aces_ap0_to_acescg(rgb))
    np.testing.assert_allclose(back, rgb, atol=1e-9)


def test_aces_batched_conversion() -> None:
    rgb = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    out = f.aces_ap0_to_acescg(rgb)
    assert out.shape == (3, 3)
    back = f.acescg_to_ap0(out)
    np.testing.assert_allclose(back, rgb, atol=1e-9)


def test_ap1_to_ap0_is_exact_inverse_of_matrix() -> None:
    np.testing.assert_allclose(f.AP1_TO_AP0 @ f.AP0_TO_AP1, np.eye(3), atol=1e-12)


# --- tone mapping -----------------------------------------------------------------


def test_tone_map_endpoints() -> None:
    assert f.aces_tone_map(np.array([0.0]))[0] == pytest.approx(0.0)
    assert f.aces_tone_map(np.array([1e6]))[0] == pytest.approx(1.0)


def test_tone_map_is_monotonic() -> None:
    x = np.linspace(0.0, 20.0, 1000)
    y = f.aces_tone_map(x)
    assert np.all(np.diff(y) >= -1e-12)


def test_tone_map_is_bounded() -> None:
    y = f.aces_tone_map(np.linspace(0.0, 100.0, 1000))
    assert y.min() >= 0.0 and y.max() <= 1.0


def test_tone_map_midpoint_value() -> None:
    # The Narkowicz fit at x=1 gives ~0.8038 for the canonical constants.
    assert f.aces_tone_map(np.array([1.0]))[0] == pytest.approx(0.8038, abs=1e-4)


# --- HALD CLUT --------------------------------------------------------------------


def test_identity_clut_reproduces_image() -> None:
    clut = f.hald_identity_clut(8)
    rng = np.random.default_rng(0)
    img = rng.random((24, 24, 3))
    out = f.apply_hald_clut(img, clut, 8)
    np.testing.assert_allclose(out, img, atol=1e-12)


def test_identity_clut_shape() -> None:
    clut = f.hald_identity_clut(4)
    assert clut.shape == (64, 3)
    np.testing.assert_allclose(clut[0], [0.0, 0.0, 0.0])
    np.testing.assert_allclose(clut[-1], [1.0, 1.0, 1.0])


def test_identity_clut_layout_order() -> None:
    # index (b*n+g)*n+r maps to (r, g, b)/(n-1).
    clut = f.hald_identity_clut(3)
    n = 3
    idx = (1 * n + 2) * n + 0  # b=1, g=2, r=0
    np.testing.assert_allclose(clut[idx], [0.0, 1.0, 0.5])


def test_clut_lerps_between_nodes() -> None:
    clut = f.hald_identity_clut(2)
    img = np.array([[[0.25, 0.5, 0.75]]])
    out = f.apply_hald_clut(img, clut, 2)
    np.testing.assert_allclose(out, img, atol=1e-12)


# --- halation ---------------------------------------------------------------------


def test_halation_preserves_shape_and_increases_energy() -> None:
    img = np.zeros((32, 32, 3))
    img[16, 16] = 1.0
    out = f.halation(img, radius=2.0, strength=0.3)
    assert out.shape == img.shape
    assert out.sum() > img.sum()  # light spread adds energy


def test_halation_red_channel_gains_most() -> None:
    img = np.zeros((32, 32, 3))
    img[16, 16] = 1.0
    out = f.halation(img, radius=2.0, strength=0.5)
    # Total added energy per channel should be ordered red > green > blue.
    added = out.sum(axis=(0, 1)) - img.sum(axis=(0, 1))
    assert added[0] > added[1] > added[2]


def test_halation_zero_strength_is_identity() -> None:
    rng = np.random.default_rng(0)
    img = rng.random((16, 16, 3))
    out = f.halation(img, strength=0.0)
    np.testing.assert_allclose(out, img, atol=1e-12)


# --- grain ------------------------------------------------------------------------


def test_grain_is_deterministic() -> None:
    img = np.ones((32, 32, 3)) * 0.5
    a = f.film_grain(img, amount=0.1, seed=1)
    b = f.film_grain(img, amount=0.1, seed=1)
    assert np.array_equal(a, b)


def test_grain_changes_with_seed() -> None:
    img = np.ones((32, 32, 3)) * 0.5
    a = f.film_grain(img, amount=0.1, seed=1)
    b = f.film_grain(img, amount=0.1, seed=2)
    assert not np.array_equal(a, b)


def test_grain_amount_controls_variance() -> None:
    img = np.ones((128, 128, 3)) * 0.5
    low = f.film_grain(img, amount=0.02, seed=1)
    high = f.film_grain(img, amount=0.2, seed=1)
    assert high.std() > low.std()


def test_grain_zero_amount_is_identity() -> None:
    img = np.ones((16, 16, 3)) * 0.5
    out = f.film_grain(img, amount=0.0, seed=1)
    np.testing.assert_allclose(out, img, atol=1e-12)


def test_grain_low_pass_smooths() -> None:
    img = np.ones((64, 64, 3)) * 0.5
    sharp = f.film_grain(img, amount=0.2, seed=1, low_pass=1)
    smooth = f.film_grain(img, amount=0.2, seed=1, low_pass=4)
    # A low-passed grain field has less high-frequency energy (smaller gradient).
    g_sharp = np.abs(np.diff(sharp[..., 0], axis=0)).mean()
    g_smooth = np.abs(np.diff(smooth[..., 0], axis=0)).mean()
    assert g_smooth < g_sharp


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_bad_colour_shape() -> None:
    with pytest.raises(f.FilmOpticError):
        f.aces_ap0_to_acescg(np.array([1.0, 2.0]))


def test_refuse_nan_colour() -> None:
    with pytest.raises(f.FilmOpticError):
        f.aces_ap0_to_acescg(np.array([1.0, np.nan, 0.0]))


def test_refuse_nan_tone_map_input() -> None:
    with pytest.raises(f.FilmOpticError):
        f.aces_tone_map(np.array([np.nan]))


def test_refuse_negative_tone_map_input() -> None:
    with pytest.raises(f.FilmOpticError):
        f.aces_tone_map(np.array([-0.1]))


def test_refuse_bad_tone_map_constants() -> None:
    with pytest.raises(f.FilmOpticError):
        f.aces_tone_map(np.array([1.0]), a=0.0)
    with pytest.raises(f.FilmOpticError):
        f.aces_tone_map(np.array([1.0]), e=0.0)


def test_refuse_bad_clut_level() -> None:
    with pytest.raises(f.FilmOpticError):
        f.hald_identity_clut(1)
    with pytest.raises(f.FilmOpticError):
        f.hald_identity_clut(f.MAX_LEVEL + 1)


def test_refuse_wrong_clut_shape() -> None:
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.zeros((4, 4, 3)), np.zeros((10, 3)), 2)


def test_refuse_bad_clut_level_in_apply() -> None:
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.zeros((4, 4, 3)), np.zeros((1, 3)), 1)


def test_refuse_image_out_of_unit_range() -> None:
    clut = f.hald_identity_clut(2)
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.full((4, 4, 3), 1.5), clut, 2)
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.full((4, 4, 3), -0.5), clut, 2)


def test_refuse_bad_halation_radius() -> None:
    with pytest.raises(f.FilmOpticError):
        f.halation(np.zeros((8, 8, 3)), radius=0.0)


def test_refuse_bad_halation_strength() -> None:
    with pytest.raises(f.FilmOpticError):
        f.halation(np.zeros((8, 8, 3)), strength=1.5)


def test_refuse_bad_halation_bias() -> None:
    with pytest.raises(f.FilmOpticError):
        f.halation(np.zeros((8, 8, 3)), red_bias=(1.0, -1.0, 0.0))


def test_refuse_2d_halation_image() -> None:
    with pytest.raises(f.FilmOpticError):
        f.halation(np.zeros((8, 8, 8, 3)))


def test_refuse_oversized_halation_image() -> None:
    with pytest.raises(f.FilmOpticError):
        f.halation(np.zeros((4097, 4, 3)))


def test_refuse_bad_grain_amount() -> None:
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 3)), amount=1.5)


def test_refuse_bad_grain_seed() -> None:
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 3)), seed=-1)
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 3)), seed=True)  # type: ignore[arg-type]


def test_refuse_bad_grain_low_pass() -> None:
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 3)), low_pass=0)
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 3)), low_pass=9)


def test_refuse_2d_grain_image() -> None:
    with pytest.raises(f.FilmOpticError):
        f.film_grain(np.zeros((8, 8, 8, 3)))


def test_refuse_oversized_image_in_clut() -> None:
    clut = f.hald_identity_clut(2)
    big = np.zeros((1, f.MAX_IMAGE_PIXELS + 3, 3))
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(big, clut, 2)


def test_refuse_2d_clut_image() -> None:
    clut = f.hald_identity_clut(2)
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.zeros((4, 4)), clut, 2)


def test_halation_skips_zero_weight_bins() -> None:
    # A tiny radius produces weights that round to zero far from the centre;
    # the guard must skip them without error.
    img = np.zeros((16, 16, 3))
    img[8, 8] = 1.0
    out = f.halation(img, radius=0.3, strength=0.5)
    assert np.isfinite(out).all()


def test_halation_small_radius_path() -> None:
    img = np.ones((16, 16, 3)) * 0.5
    out = f.halation(img, radius=0.25, strength=0.1)
    assert out.shape == img.shape


def test_refuse_bad_clut_level_upper() -> None:
    with pytest.raises(f.FilmOpticError):
        f.apply_hald_clut(np.zeros((4, 4, 3)), np.zeros((2**9, 3)), f.MAX_LEVEL + 1)


def test_halation_radius_half_gives_odd_kernel() -> None:
    img = np.ones((16, 16, 3)) * 0.5
    out = f.halation(img, radius=1.5, strength=0.2)
    assert out.shape == img.shape


def test_halation_tiny_radius_zero_weights() -> None:
    # A tiny radius underflows the kernel to exact zeros far from the centre,
    # exercising the zero-weight skip branch.
    img = np.zeros((16, 16, 3))
    img[8, 8] = 1.0
    out = f.halation(img, radius=0.01, strength=0.5)
    assert np.isfinite(out).all()
