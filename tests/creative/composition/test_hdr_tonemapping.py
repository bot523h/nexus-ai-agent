"""Tests for Wave 11 — HDR tone mapping and colour science.

Real computation only: closed-form curve values, analytic chromatic-adaptation
identities, and adversarial/refusal inputs. No mocks.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.composition import hdr_tonemapping as tm


def _hdr(n: int = 16, peak: float = 4.0) -> np.ndarray:
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float64)
    img = np.stack([xs / (n - 1), ys / (n - 1), np.full((n, n), 0.5)], axis=-1)
    return img * peak


def _luma_image(n: int = 8) -> np.ndarray:
    return np.full((n, n, 3), 0.5)


# --- luminance -------------------------------------------------------------------


def test_luminance_rec709_weights() -> None:
    img = np.zeros((2, 2, 3))
    img[..., 0] = 1.0
    assert np.allclose(tm.luminance(img), 0.2126)


def test_luminance_2d_passthrough() -> None:
    a = np.arange(9.0).reshape(3, 3)
    assert np.array_equal(tm.luminance(a), a)


def test_luminance_single_channel() -> None:
    a = np.arange(4.0).reshape(2, 2, 1)
    assert np.array_equal(tm.luminance(a), a[..., 0])


def test_luminance_rejects_nan() -> None:
    img = np.zeros((2, 2, 3))
    img[0, 0, 0] = np.nan
    with pytest.raises(tm.TonemapError):
        tm.luminance(img)


def test_luminance_rejects_negative() -> None:
    img = np.zeros((2, 2, 3))
    img[0, 0, 0] = -0.1
    with pytest.raises(tm.TonemapError):
        tm.luminance(img)


def test_luminance_rejects_bad_channels() -> None:
    with pytest.raises(tm.TonemapError):
        tm.luminance(np.zeros((2, 2, 4)))


def test_luminance_rejects_over_budget() -> None:
    with pytest.raises(tm.TonemapError):
        tm.luminance(np.zeros((2049, 2049)))


# --- auto exposure ---------------------------------------------------------------


def test_auto_exposure_scale_maps_geometric_mean_to_key() -> None:
    img = _luma_image() * 2.0  # constant 1.0 radiance
    scale = tm.auto_exposure_scale(img, key=0.18, epsilon=1e-4)
    assert scale * 1.0 == pytest.approx(0.18 / (1.0 + 1e-4), rel=1e-6)


def test_auto_exposure_scale_is_positive_and_finite_on_black() -> None:
    scale = tm.auto_exposure_scale(np.zeros((4, 4, 3)))
    assert np.isfinite(scale)
    assert scale > 0.0


def test_auto_exposure_rejects_bad_key() -> None:
    with pytest.raises(tm.TonemapError):
        tm.auto_exposure_scale(_luma_image(), key=0.0)


def test_auto_exposure_rejects_bad_epsilon() -> None:
    with pytest.raises(tm.TonemapError):
        tm.auto_exposure_scale(_luma_image(), epsilon=0.0)


# --- tone curves -----------------------------------------------------------------


def _curves() -> list[tuple[str, object]]:
    return [
        ("reinhard", tm.reinhard_tonemap),
        ("aces", tm.aces_filmic_tonemap),
        ("hejl", tm.hejl_burgess_tonemap),
        ("uncharted", tm.uncharted2_tonemap),
    ]


@pytest.mark.parametrize("name,fn", _curves())
def test_curve_maps_zero_to_zero(name: str, fn: object) -> None:
    out = fn(np.zeros((3, 3, 3)))  # type: ignore[operator]
    assert float(out.max()) == 0.0


@pytest.mark.parametrize("name,fn", _curves())
def test_curve_is_bounded_and_monotone(name: str, fn: object) -> None:
    x = np.linspace(0.0, 20.0, 512).reshape(-1, 1, 1)
    y = fn(x)  # type: ignore[operator]
    flat = y.reshape(-1)
    assert float(flat.min()) >= 0.0
    assert float(flat.max()) <= 1.0
    assert np.all(np.diff(flat) >= -1e-6)


@pytest.mark.parametrize("name,fn", _curves())
def test_curve_rejects_nonpositive_exposure(name: str, fn: object) -> None:
    with pytest.raises(tm.TonemapError):
        fn(_hdr(), exposure=0.0)  # type: ignore[call-arg]


def test_reinhard_white_point_maps_to_one() -> None:
    # Extended Reinhard: L_d(white) = white (1 + 1) / (1 + white) = 1 for white=1.
    out = tm.reinhard_tonemap(np.array([[[1.0, 1.0, 1.0]]]), white=1.0)
    assert float(out[0, 0, 0]) == pytest.approx(1.0)


def test_reinhard_closed_form_value() -> None:
    # white = 2, L = 2 -> 2 (1 + 2/4) / (1 + 2) = 2 * 1.5 / 3 = 1.0
    out = tm.reinhard_tonemap(np.array([[[2.0, 2.0, 2.0]]]), white=2.0)
    assert float(out[0, 0, 0]) == pytest.approx(1.0)


def test_reinhard_rejects_bad_white() -> None:
    with pytest.raises(tm.TonemapError):
        tm.reinhard_tonemap(_hdr(), white=0.0)


def test_aces_closed_form_value() -> None:
    x = 0.5
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    expected = (x * (a * x + b)) / (x * (c * x + d) + e)
    out = tm.aces_filmic_tonemap(np.array([[[x, x, x]]]))
    assert float(out[0, 0, 0]) == pytest.approx(expected, rel=1e-6)


def test_hejl_closed_form_value() -> None:
    x = 1.0 - 0.004
    expected = (x * (6.2 * x + 0.5)) / (x * (6.2 * x + 1.7) + 0.06)
    out = tm.hejl_burgess_tonemap(np.array([[[1.0, 1.0, 1.0]]]))
    assert float(out[0, 0, 0]) == pytest.approx(expected, rel=1e-6)


def test_uncharted_closed_form_value() -> None:
    a, b, c, d, e, f = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
    x = 1.0

    def curve(v: float) -> float:
        return (v * (a * v + c * b) + d * e) / (v * (a * v + b) + d * f) - e / f

    expected = curve(x) / curve(11.2)
    out = tm.uncharted2_tonemap(np.array([[[x, x, x]]]))
    assert float(out[0, 0, 0]) == pytest.approx(expected, rel=1e-5)


def test_tonemap_preserves_shape_and_dtype() -> None:
    out = tm.aces_filmic_tonemap(_hdr(12))
    assert out.shape == (12, 12, 3)
    assert out.dtype == np.float32


# --- gamma -----------------------------------------------------------------------


def test_gamma_roundtrip_within_unit_range() -> None:
    img = _hdr(16, peak=1.0)
    assert np.allclose(tm.gamma_decode(tm.gamma_encode(img)), img, atol=1e-5)


def test_gamma_encode_known_value() -> None:
    out = tm.gamma_encode(np.array([[[0.25, 0.25, 0.25]]]), gamma=2.0)
    assert float(out[0, 0, 0]) == pytest.approx(0.5)


def test_gamma_decode_known_value() -> None:
    out = tm.gamma_decode(np.array([[[0.5, 0.5, 0.5]]]), gamma=2.0)
    assert float(out[0, 0, 0]) == pytest.approx(0.25)


def test_gamma_rejects_bad_gamma() -> None:
    with pytest.raises(tm.TonemapError):
        tm.gamma_encode(_hdr(8), gamma=0.0)
    with pytest.raises(tm.TonemapError):
        tm.gamma_decode(_hdr(8), gamma=-1.0)


# --- colour temperature and adaptation -------------------------------------------


def test_kelvin_to_xy_d65_is_near_reference() -> None:
    x, y = tm.kelvin_to_xy(6500)
    assert x == pytest.approx(0.3127, abs=0.02)
    assert y == pytest.approx(0.3290, abs=0.02)


def test_kelvin_to_xy_x_decreases_with_temperature() -> None:
    # Along the Planckian locus a hotter black body is bluer, so x falls.
    xs = [tm.kelvin_to_xy(t)[0] for t in (2000, 4000, 6500, 10000)]
    assert xs == sorted(xs, reverse=True)


def test_kelvin_to_xy_rejects_out_of_range() -> None:
    with pytest.raises(tm.TonemapError):
        tm.kelvin_to_xy(1000)
    with pytest.raises(tm.TonemapError):
        tm.kelvin_to_xy(30000)


def test_xy_to_xyz_normalises_luminance() -> None:
    xyz = tm.xy_to_xyz(0.3127, 0.3290)
    assert xyz[1] == pytest.approx(1.0)


def test_xy_to_xyz_rejects_bad_inputs() -> None:
    with pytest.raises(tm.TonemapError):
        tm.xy_to_xyz(0.3, 0.0)
    with pytest.raises(tm.TonemapError):
        tm.xy_to_xyz(0.3, 0.3, luminance_y=0.0)


def test_bradford_identity_is_a_noop() -> None:
    img = _hdr(12)
    out = tm.bradford_adapt(img, src_white_xy=(0.3127, 0.3290), dst_white_xy=(0.3127, 0.3290))
    assert np.allclose(out, img, atol=1e-5)


def test_bradford_warms_a_neutral_grey() -> None:
    img = _luma_image(4)
    out = tm.bradford_adapt(img, src_white_xy=(0.3127, 0.3290), dst_white_xy=tm.kelvin_to_xy(3000))
    # Adapting a neutral patch toward a warm white raises R relative to B.
    assert float(out[..., 0].mean()) > float(out[..., 2].mean())


def test_bradford_rejects_degenerate_source() -> None:
    with pytest.raises(tm.TonemapError):
        tm.bradford_adapt(_luma_image(4), src_white_xy=(0.0, 0.0), dst_white_xy=(0.3127, 0.3290))


# --- colour-space conversion -----------------------------------------------------


def test_srgb_xyz_roundtrip() -> None:
    img = _hdr(12, peak=1.0)
    back = tm.xyz_to_srgb(tm.srgb_to_xyz(img))
    assert np.allclose(back, img, atol=1e-4)


def test_srgb_to_xyz_rejects_nan() -> None:
    img = np.zeros((2, 2, 3))
    img[0, 0, 0] = np.inf
    with pytest.raises(tm.TonemapError):
        tm.srgb_to_xyz(img)


def test_xyz_to_srgb_rejects_bad_shape() -> None:
    with pytest.raises(tm.TonemapError):
        tm.xyz_to_srgb(np.zeros((2, 2, 2)))


def test_xyz_to_srgb_rejects_nan() -> None:
    xyz = np.zeros((2, 2, 3))
    xyz[0, 0, 0] = np.nan
    with pytest.raises(tm.TonemapError):
        tm.xyz_to_srgb(xyz)


# --- vignette --------------------------------------------------------------------


def test_vignette_zero_strength_is_identity() -> None:
    img = _hdr(12)
    out = tm.apply_vignette(img, strength=0.0)
    assert np.allclose(out, img, atol=1e-5)


def test_vignette_darkens_corners_not_centre() -> None:
    img = np.ones((32, 32, 3))
    out = tm.apply_vignette(img, strength=0.5, radius=0.5)
    assert float(out[16, 16, 0]) > float(out[0, 0, 0])
    assert float(out[0, 0, 0]) >= 0.0


def test_vignette_2d_input() -> None:
    img = np.ones((16, 16))
    out = tm.apply_vignette(img, strength=0.4)
    assert out.shape == (16, 16)


def test_vignette_rejects_bad_strength() -> None:
    with pytest.raises(tm.TonemapError):
        tm.apply_vignette(_hdr(8), strength=1.5)


def test_vignette_rejects_bad_radius() -> None:
    with pytest.raises(tm.TonemapError):
        tm.apply_vignette(_hdr(8), radius=0.0)


def test_degenerate_curve_error_is_a_tonemap_error() -> None:
    assert issubclass(tm.DegenerateToneCurveError, tm.TonemapError)


def test_luminance_rejects_empty_image() -> None:
    with pytest.raises(tm.TonemapError):
        tm.luminance(np.zeros((0, 0, 3)))


def test_auto_exposure_rejects_nonpositive_scale_on_overflow() -> None:
    # A vanishing epsilon drives exp(mean log) to +inf, so the scale underflows to 0.
    with pytest.raises(tm.TonemapError):
        tm.auto_exposure_scale(np.zeros((4, 4, 3)), epsilon=np.nextafter(0.0, 1.0))


def test_bradford_rejects_chromaticity_on_the_cone_null() -> None:
    # y ~ 0.667 places the source white where the Bradford B cone response is ~0.
    with pytest.raises(tm.TonemapError):
        tm.bradford_adapt(
            _luma_image(4), src_white_xy=(0.3, 0.6669611146525818), dst_white_xy=(0.3127, 0.3290)
        )
