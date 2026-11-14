"""Counterexamples separating physical information from numerical quadrature.

These do not relax validators. They prevent geometry-only warps or a changed
integration sample count from being misreported as a physical zero-residual
repair of an intensity-gain error or acquisition aliasing respectively.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    _fringe_metric,
    _radial_resample,
    rectify_stroboscopic_wagon_wheel,
)
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import _read_png_rgb, _write_png
from nexus_ai_agent.creative.veritas.reality_auditor import build_corrupted_15_law_cinema_shot


def test_existing_fixture_is_color_gain_not_radial_displacement() -> None:
    image = build_corrupted_15_law_cinema_shot().anamorphic
    assert image is not None
    np.testing.assert_allclose(image[..., 0], image[..., 1] * 1.02, atol=1e-14)
    np.testing.assert_allclose(image[..., 2], image[..., 1] * 0.98, atol=1e-14)
    assert _fringe_metric(image) > 1e-6
    # Removing only the known gains leaves exactly the same edge geometry.
    for channel, gain in ((0, 1.02), (2, 0.98)):
        for axis in (0, 1):
            np.testing.assert_allclose(
                np.gradient(image[..., channel] / gain, axis=axis),
                np.gradient(image[..., 1], axis=axis),
                atol=1e-14,
            )


@pytest.mark.parametrize("red_scale,blue_scale", [(0.95, 1.05), (1.05, 0.95), (1.0, 1.0)])
def test_radial_warp_cannot_remove_constant_chromatic_gain(
    tmp_path: Path,
    red_scale: float,
    blue_scale: float,
) -> None:
    rgb = np.empty((8, 8, 3))
    rgb[:] = [0.6, 0.5, 0.4]
    warped = rgb.copy()
    warped[..., 0] = _radial_resample(rgb[..., 0], red_scale)
    warped[..., 2] = _radial_resample(rgb[..., 2], blue_scale)
    assert _fringe_metric(warped) == pytest.approx(0.2)
    path = tmp_path / "registered.png"
    _write_png(path, np.round(warped * 255).astype(np.uint8))
    decoded = _read_png_rgb(path).astype(np.float64) / 255
    assert _fringe_metric(decoded) > 1e-6


def test_distinct_rotations_share_point_samples_and_integrated_exposures() -> None:
    fps = 24.0
    omega_fast = 200.0
    omega_alias = omega_fast - 2 * np.pi * fps
    timestamps = np.arange(48) / fps
    np.testing.assert_allclose(
        np.cos(omega_fast * timestamps),
        np.cos(omega_alias * timestamps),
        atol=3e-13,
    )
    # A finite symmetric exposure is a sinc filter, not additional captured
    # frames. With unknown scene contrast, even the integrated observations can
    # coincide exactly for different physical angular velocities.
    exposure = 0.5 / fps
    gain_fast = np.sinc(omega_fast * exposure / (2 * np.pi))
    gain_alias = np.sinc(omega_alias * exposure / (2 * np.pi))
    contrast_fast = 0.2
    contrast_alias = contrast_fast * gain_fast / gain_alias
    fast = 0.5 + contrast_fast * gain_fast * np.cos(omega_fast * timestamps)
    aliased = 0.5 + contrast_alias * gain_alias * np.cos(omega_alias * timestamps)
    np.testing.assert_allclose(fast, aliased, atol=1e-13)
    assert abs(omega_fast) / fps > np.pi
    assert abs(omega_alias) / fps < np.pi


@pytest.mark.parametrize("samples", [4, 16, 128])
def test_more_quadrature_nodes_do_not_create_hfr_observations(samples: int) -> None:
    patch = np.zeros((16, 16))
    patch[:, 7:9] = 1
    result = rectify_stroboscopic_wagon_wheel(
        patch,
        angular_velocity_rad_s=200,
        fps=24,
        samples=samples,
    )
    assert result.delta_phi == pytest.approx(200 / 24)
    assert result.aliased
    assert "nyquist_rotary_aliasing" in result.violations
    assert result.healed_patch.shape == patch.shape
