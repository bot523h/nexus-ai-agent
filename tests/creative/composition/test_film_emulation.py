"""Tests for Wave 12 — photochemical film emulation and objective metrics.

Real computation only: determinism, closed-form identities, statistical grain
properties and every refusal path. No mocks.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.composition import film_emulation as fe


def _img(n: int = 48, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).random((n, n, 3)).astype(np.float32)


def _bright_spot(n: int = 48) -> np.ndarray:
    img = np.zeros((n, n, 3), dtype=np.float32)
    img[10:14, 10:14] = 1.0
    return img


# --- gaussian kernel -------------------------------------------------------------


def test_gaussian_kernel_is_normalised_and_symmetric() -> None:
    k = fe.gaussian_kernel_1d(1.5)
    assert float(k.sum()) == pytest.approx(1.0)
    assert np.allclose(k, k[::-1])


def test_gaussian_kernel_rejects_bad_sigma() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gaussian_kernel_1d(0.0)


def test_gaussian_kernel_rejects_bad_truncate() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gaussian_kernel_1d(1.0, truncate=0.0)


# --- halation --------------------------------------------------------------------


def test_halation_zero_strength_is_identity() -> None:
    img = _img()
    assert np.array_equal(fe.halation(img, strength=0.0), img.astype(np.float32))


def test_halation_bleeds_red_from_bright_regions() -> None:
    img = _bright_spot()
    out = fe.halation(img, strength=0.5, sigma=4.0)
    # The halo just outside the bright patch is red-dominant.
    assert float(out[6, 6, 0]) > float(out[6, 6, 2])
    assert float(out[6, 6, 0]) > 0.0


def test_halation_preserves_shape_and_bounds() -> None:
    out = fe.halation(_img(32))
    assert out.shape == (32, 32, 3)
    assert np.isfinite(out).all()
    assert float(out.min()) >= 0.0


def test_halation_2d_input() -> None:
    out = fe.halation(_bright_spot(32)[..., 0], strength=0.4)
    assert out.shape == (32, 32)


def test_halation_single_channel_input() -> None:
    img = _bright_spot(32)[..., :1]
    out = fe.halation(img, strength=0.4)
    assert out.shape == (32, 32, 3)


def test_halation_rejects_negative_strength() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.halation(_img(), strength=-0.1)


def test_halation_rejects_bad_red_bias() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.halation(_img(), red_bias=0.0)


# --- film grain ------------------------------------------------------------------


def test_grain_is_deterministic_for_a_seed() -> None:
    img = _img()
    assert np.array_equal(fe.film_grain(img, seed=7), fe.film_grain(img, seed=7))


def test_grain_differs_across_seeds() -> None:
    img = _img()
    assert not np.array_equal(fe.film_grain(img, seed=1), fe.film_grain(img, seed=2))


def test_grain_zero_sigma_is_identity() -> None:
    img = _img()
    assert np.array_equal(fe.film_grain(img, sigma=0.0), img.astype(np.float32))


def test_grain_rms_scales_as_inverse_sqrt_area() -> None:
    flat = np.full((64, 64, 3), 0.5, dtype=np.float32)
    r1 = float(np.std(fe.film_grain(flat, seed=1, sigma=0.1, area=1)))
    r4 = float(np.std(fe.film_grain(flat, seed=1, sigma=0.1, area=4)))
    assert r1 / r4 == pytest.approx(2.0, rel=0.05)


def test_grain_is_strongest_in_midtones() -> None:
    dark = np.full((64, 64, 3), 0.02, dtype=np.float32)
    mid = np.full((64, 64, 3), 0.5, dtype=np.float32)
    assert float(np.std(fe.film_grain(mid, seed=3, sigma=0.1))) > float(
        np.std(fe.film_grain(dark, seed=3, sigma=0.1))
    )


def test_grain_preserves_shape_and_bounds() -> None:
    out = fe.film_grain(_img(24), seed=2)
    assert out.shape == (24, 24, 3)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_grain_2d_input() -> None:
    out = fe.film_grain(np.full((16, 16), 0.5, dtype=np.float32), seed=1)
    assert out.shape == (16, 16)


def test_grain_rejects_negative_seed() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.film_grain(_img(), seed=-1)


def test_grain_rejects_bad_area() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.film_grain(_img(), area=0)


def test_grain_rejects_negative_sigma() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.film_grain(_img(), sigma=-0.1)


# --- gate weave ------------------------------------------------------------------


def test_gate_weave_zero_amplitude_is_identity() -> None:
    img = _img()
    assert np.array_equal(fe.gate_weave(img, frame_index=5, amplitude=0.0), img.astype(np.float32))


def test_gate_weave_is_deterministic() -> None:
    img = _img()
    a = fe.gate_weave(img, frame_index=5, seed=3)
    b = fe.gate_weave(img, frame_index=5, seed=3)
    assert np.array_equal(a, b)


def test_gate_weave_varies_between_frames() -> None:
    img = _img()
    a = fe.gate_weave(img, frame_index=1, seed=3)
    b = fe.gate_weave(img, frame_index=2, seed=3)
    assert not np.array_equal(a, b)


def test_gate_weave_preserves_shape() -> None:
    out = fe.gate_weave(_img(32), frame_index=0)
    assert out.shape == (32, 32, 3)


def test_gate_weave_rejects_negative_frame() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gate_weave(_img(), frame_index=-1)


def test_gate_weave_rejects_negative_amplitude() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gate_weave(_img(), frame_index=0, amplitude=-1.0)


def test_gate_weave_rejects_negative_seed() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gate_weave(_img(), frame_index=0, seed=-1)


# --- full pipeline ---------------------------------------------------------------


def test_apply_film_emulation_is_deterministic() -> None:
    img = _img()
    a = fe.apply_film_emulation(img, frame_index=2, seed=5)
    b = fe.apply_film_emulation(img, frame_index=2, seed=5)
    assert np.array_equal(a, b)


def test_apply_film_emulation_preserves_shape_and_bounds() -> None:
    out = fe.apply_film_emulation(_img(32), frame_index=1, seed=1)
    assert out.shape == (32, 32, 3)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


# --- metrics ---------------------------------------------------------------------


def test_psnr_identical_is_infinite() -> None:
    img = _img()
    assert fe.psnr(img, img) == float("inf")


def test_psnr_closed_form_value() -> None:
    ref = np.zeros((4, 4, 3), dtype=np.float32)
    test = np.full((4, 4, 3), 0.1, dtype=np.float32)
    # MSE = 0.01 -> PSNR = 10 log10(1 / 0.01) = 20 dB
    assert fe.psnr(ref, test) == pytest.approx(20.0)


def test_psnr_rejects_shape_mismatch() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.psnr(_img(8), _img(16))


def test_psnr_rejects_bad_peak() -> None:
    img = _img(8)
    with pytest.raises(fe.FilmEmulationError):
        fe.psnr(img, img, peak=0.0)


def test_ssim_identical_is_one() -> None:
    img = _img()
    assert fe.ssim(img, img) == pytest.approx(1.0, abs=1e-9)


def test_ssim_decreases_with_noise() -> None:
    img = _img()
    rng = np.random.default_rng(9)
    noisy = np.clip(img + 0.15 * rng.standard_normal(img.shape), 0.0, 1.0).astype(np.float32)
    assert fe.ssim(img, noisy) < 1.0
    assert fe.ssim(img, noisy) > -1.0


def test_ssim_is_symmetric() -> None:
    a = _img(32, seed=1)
    b = _img(32, seed=2)
    assert fe.ssim(a, b) == pytest.approx(fe.ssim(b, a), rel=1e-9)


def test_ssim_on_small_image_falls_back_to_global_mean() -> None:
    a = np.full((4, 4, 3), 0.5, dtype=np.float32)
    assert fe.ssim(a, a) == pytest.approx(1.0)


def test_ssim_rejects_shape_mismatch() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.ssim(_img(8), _img(16))


def test_ssim_rejects_bad_peak() -> None:
    img = _img(8)
    with pytest.raises(fe.FilmEmulationError):
        fe.ssim(img, img, peak=-1.0)


def test_ssim_2d_input() -> None:
    a = _img(32)[..., 0]
    assert fe.ssim(a, a) == pytest.approx(1.0)


def test_ssim_single_channel_input() -> None:
    a = _img(32)[..., :1]
    assert fe.ssim(a, a) == pytest.approx(1.0)


# --- shared validation -----------------------------------------------------------


def test_validate_rejects_nan() -> None:
    img = _img()
    img[0, 0, 0] = np.nan
    with pytest.raises(fe.FilmEmulationError):
        fe.halation(img)


def test_validate_rejects_negative() -> None:
    img = _img()
    img[0, 0, 0] = -0.1
    with pytest.raises(fe.FilmEmulationError):
        fe.film_grain(img, seed=0)


def test_validate_rejects_bad_channels() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.gate_weave(np.zeros((4, 4, 2), dtype=np.float32), frame_index=0)


def test_validate_rejects_empty() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.halation(np.zeros((0, 0, 3), dtype=np.float32))


def test_validate_rejects_over_budget() -> None:
    with pytest.raises(fe.FilmEmulationError):
        fe.psnr(np.zeros((2049, 2049)), np.zeros((2049, 2049)))


def test_blur_handles_2d_and_3d_consistently() -> None:
    g2 = _img(32)[..., 0]
    g3 = np.repeat(g2[..., None], 3, axis=-1)
    b2 = fe._blur(g2, 2.0)
    b3 = fe._blur(g3, 2.0)
    assert b2.shape == g2.shape
    assert np.allclose(b2, b3[..., 0], atol=1e-9)


def test_gate_weave_2d_input() -> None:
    img = _img(32)[..., 0]
    out = fe.gate_weave(img, frame_index=4)
    assert out.shape == (32, 32)
