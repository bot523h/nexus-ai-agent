"""WAVE 17 — tests for third-order Seidel optics and anamorphic PSF."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import anamorphic_seidel_optics as ao

# --- Seidel wavefront ------------------------------------------------------------


def test_seidel_zero_coefficients_is_flat() -> None:
    c = ao.SeidelCoefficients()
    rho = np.linspace(0.0, 1.0, 7)
    theta = np.linspace(0.0, 2 * np.pi, 7)
    assert np.allclose(ao.seidel_wavefront(c, rho, theta), 0.0)


def test_seidel_wavefront_analytic_value() -> None:
    c = ao.SeidelCoefficients(
        spherical=1.0, coma=0.5, astigmatism=0.3, field_curvature=0.2, distortion=0.1
    )
    # rho=1, theta=0 -> 1 + 0.5 + 0.3 + 0.2 + 0.1 = 2.1
    w = ao.seidel_wavefront(c, np.array([1.0]), np.array([0.0]))
    assert float(w[0]) == pytest.approx(2.1)


def test_seidel_wavefront_is_zero_at_pupil_center() -> None:
    c = ao.SeidelCoefficients(spherical=2.0, coma=1.0, distortion=3.0)
    assert float(ao.seidel_wavefront(c, np.array([0.0]), np.array([1.0]))[0]) == 0.0


def test_seidel_spherical_is_rotation_invariant() -> None:
    c = ao.SeidelCoefficients(spherical=1.0)
    w0 = float(ao.seidel_wavefront(c, np.array([0.7]), np.array([0.0]))[0])
    w1 = float(ao.seidel_wavefront(c, np.array([0.7]), np.array([np.pi / 3]))[0])
    assert w0 == pytest.approx(w1)


def test_seidel_rejects_bad_rho_range() -> None:
    c = ao.SeidelCoefficients()
    with pytest.raises(ao.OpticsError):
        ao.seidel_wavefront(c, np.array([1.5]), np.array([0.0]))


def test_seidel_rejects_nan() -> None:
    c = ao.SeidelCoefficients()
    with pytest.raises(ao.OpticsError):
        ao.seidel_wavefront(c, np.array([np.nan]), np.array([0.0]))


def test_seidel_coefficients_reject_nan() -> None:
    with pytest.raises(ao.OpticsError):
        ao.SeidelCoefficients(coma=np.nan)


# --- anamorphic lens -------------------------------------------------------------


def test_lens_squeeze_factor() -> None:
    lens = ao.AnamorphicLens(focal_x_mm=40.0, focal_y_mm=30.0)
    assert lens.squeeze_factor == pytest.approx(40.0 / 30.0)


def test_lens_blur_sigmas_anamorphic_ratio() -> None:
    lens = ao.AnamorphicLens(focal_x_mm=40.0, focal_y_mm=20.0)  # squeeze = 2
    sx, sy = lens.blur_sigmas(defocus_mm=0.1, pixel_pitch_mm=0.01)
    assert sy == pytest.approx((0.1 / (2 * 2.8)) / 0.01)
    assert sx == pytest.approx(sy / 2.0)


def test_lens_rejects_nonpositive_focal() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens(focal_x_mm=0.0)


def test_lens_rejects_bad_tstop() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens(t_stop=0.0)


def test_lens_rejects_bad_squeeze() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens(anamorphic_squeeze=0.0)


def test_lens_rejects_bad_chromatic_shift() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens(chromatic_shift_px=(0.0, 0.0))


def test_blur_sigmas_rejects_negative_defocus() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens().blur_sigmas(defocus_mm=-1.0, pixel_pitch_mm=0.01)


def test_blur_sigmas_rejects_bad_pitch() -> None:
    with pytest.raises(ao.OpticsError):
        ao.AnamorphicLens().blur_sigmas(defocus_mm=0.1, pixel_pitch_mm=0.0)


# --- PSF -------------------------------------------------------------------------


def test_psf_integrates_to_one() -> None:
    psf = ao.anamorphic_psf(ao.AnamorphicLens(), size=65)
    assert np.allclose(psf.sum(axis=(0, 1)), 1.0, atol=1e-9)


def test_psf_shape() -> None:
    psf = ao.anamorphic_psf(ao.AnamorphicLens(), size=33)
    assert psf.shape == (33, 33, 3)


def test_psf_is_nonnegative() -> None:
    psf = ao.anamorphic_psf(ao.AnamorphicLens(), size=33, streak_length=5, streak_weight=0.3)
    assert np.all(psf >= 0.0)


def test_psf_rejects_even_size() -> None:
    with pytest.raises(ao.OpticsError):
        ao.anamorphic_psf(ao.AnamorphicLens(), size=32)


def test_psf_rejects_too_small_size() -> None:
    with pytest.raises(ao.OpticsError):
        ao.anamorphic_psf(ao.AnamorphicLens(), size=1)


def test_psf_rejects_over_budget() -> None:
    with pytest.raises(ao.OpticsError):
        ao.anamorphic_psf(ao.AnamorphicLens(), size=ao.MAX_PSF_SIDE + 1)


def test_psf_rejects_bad_streak_length() -> None:
    with pytest.raises(ao.OpticsError):
        ao.anamorphic_psf(ao.AnamorphicLens(), size=33, streak_length=33)


def test_psf_rejects_bad_streak_weight() -> None:
    with pytest.raises(ao.OpticsError):
        ao.anamorphic_psf(ao.AnamorphicLens(), size=33, streak_weight=1.5)


def test_psf_chromatic_shift_moves_channel() -> None:
    lens = ao.AnamorphicLens(chromatic_shift_px=(3.0, 0.0, 0.0))
    psf = ao.anamorphic_psf(lens, size=33, defocus_mm=0.0)
    # Red channel (0) shifted right relative to green (1).
    red_cx = float(np.argmax(psf[..., 0].sum(axis=0)))
    green_cx = float(np.argmax(psf[..., 1].sum(axis=0)))
    assert red_cx > green_cx


# --- apply optics ----------------------------------------------------------------


def test_apply_optics_preserves_energy() -> None:
    img = np.zeros((32, 32, 3))
    img[16, 16] = 1.0
    res = ao.apply_anamorphic_optics(
        img, ao.AnamorphicLens(), size=33, defocus_mm=0.05, pixel_pitch_mm=0.01
    )
    assert res.energy_residual < 1e-9


def test_apply_optics_psf_energy_is_one() -> None:
    img = np.ones((16, 16, 3))
    res = ao.apply_anamorphic_optics(img, ao.AnamorphicLens(), size=17)
    assert res.psf_energy == pytest.approx(1.0, abs=1e-9)


def test_apply_optics_blurs_point() -> None:
    img = np.zeros((32, 32, 3))
    img[16, 16] = 1.0
    res = ao.apply_anamorphic_optics(
        img, ao.AnamorphicLens(), size=33, defocus_mm=0.1, pixel_pitch_mm=0.01
    )
    # The peak is lower than 1 and neighbouring pixels receive energy.
    assert float(res.image.max()) < 1.0
    assert float(res.image[16, 17].sum()) > 0.0


def test_apply_optics_flat_field_stays_flat() -> None:
    img = np.full((16, 16, 3), 0.5)
    res = ao.apply_anamorphic_optics(img, ao.AnamorphicLens(), size=17)
    assert np.allclose(res.image, 0.5, atol=1e-9)


def test_apply_optics_with_streak() -> None:
    img = np.zeros((32, 32, 3))
    img[16, 16] = 1.0
    res = ao.apply_anamorphic_optics(
        img, ao.AnamorphicLens(), size=33, streak_length=10, streak_weight=0.5
    )
    assert res.energy_residual < 1e-9
    # Horizontal neighbours along the streak row receive energy.
    assert float(res.image[16, 20].sum()) > 0.0


def test_apply_optics_rejects_bad_shape() -> None:
    with pytest.raises(ao.OpticsError):
        ao.apply_anamorphic_optics(np.zeros((8, 8)), ao.AnamorphicLens())


def test_apply_optics_rejects_nan() -> None:
    img = np.zeros((8, 8, 3))
    img[0, 0, 0] = np.nan
    with pytest.raises(ao.OpticsError):
        ao.apply_anamorphic_optics(img, ao.AnamorphicLens())
