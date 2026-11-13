"""Exact CIE D65 is the default Bradford destination for the Hyperion colour kernels."""

from __future__ import annotations

import numpy as np

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci


def test_d65_xyz_reproduces_its_published_chromaticity() -> None:
    total = float(ci._D65.sum())
    x, y = float(ci._D65[0]) / total, float(ci._D65[1]) / total
    assert abs(x - ci.D65_XY[0]) < 1e-5
    assert abs(y - ci.D65_XY[1]) < 1e-5


def test_default_destination_is_exact_d65_not_the_black_body_point() -> None:
    source = ci._temperature_white_point(3200.0)
    to_d65 = ci._adaptation_rgb(source, ci._D65)
    to_planck_6500 = ci._adaptation_rgb(source, ci._temperature_white_point(6500.0))
    image = np.random.default_rng(7).random((3, 3, 3)) + 0.05

    out = ci.white_balance_chromatic_adaptation(image, kelvin=3200.0)

    assert np.allclose(out, image @ to_d65.T, atol=1e-12)
    # The old default (black-body 6500 K) must be a measurably different destination.
    assert float(np.max(np.abs(to_d65 - to_planck_6500))) > 1e-3


def test_source_white_maps_to_neutral_under_the_default_target() -> None:
    source_rgb = ci._XYZ_TO_RGB @ ci._temperature_white_point(3200.0)
    pixel = np.array([[source_rgb]], dtype=np.float64)

    out = ci.white_balance_chromatic_adaptation(pixel, kelvin=3200.0)[0, 0]

    assert float(np.max(out) - np.min(out)) < 1e-9
    assert np.allclose(out, 1.0, atol=1e-9)


def test_bradford_adapt_defaults_to_d65_and_explicit_target_still_selects_black_body() -> None:
    image = np.random.default_rng(11).random((2, 2, 3)) + 0.1
    default = ci.bradford_adapt(image, 3200.0)
    explicit_d65_matrix = image @ ci._adaptation_rgb(ci._temperature_white_point(3200.0), ci._D65).T
    explicit_black_body = ci.bradford_adapt(image, 3200.0, 6500.0)

    assert np.allclose(default, explicit_d65_matrix, atol=1e-12)
    assert not np.allclose(default, explicit_black_body, atol=1e-6)
