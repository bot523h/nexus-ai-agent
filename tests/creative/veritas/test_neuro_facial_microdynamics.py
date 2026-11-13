"""Unit tests for Law 10 — FACS synergy, blinks and dermal scattering."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import (
    FacialDynamicsError,
    apply_dermal_subsurface_scattering,
    enforce_facs_synergy_manifold,
    synthesize_asymmetric_blink_curve,
)


def test_duchenne_smile_is_enforced() -> None:
    au = np.zeros((2, 12), dtype=np.float64)
    au[:, 11] = 1.0
    result = enforce_facs_synergy_manifold(au)
    assert result.violations_before == ("duchenne_smile",)
    assert result.violations_after == ()
    assert result.action_units[0, 5] >= 0.5 * (1.0 - 0.1)


def test_f0_prosody_and_jaw_lip_conservation() -> None:
    au = np.zeros((3, 26), dtype=np.float64)
    au[:, 24] = 0.2
    au[:, 25] = 0.9
    result = enforce_facs_synergy_manifold(au, f0_envelope=np.array([0.0, 0.5, 1.0]))
    assert "jaw_lip_volume" in result.violations_before
    assert result.violations_after == ()
    assert result.action_units[2, 0] == pytest.approx(0.5)
    assert result.action_units[2, 1] == pytest.approx(0.3)
    assert np.all(result.action_units[:, 25] <= result.action_units[:, 24])


def test_facs_validation() -> None:
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.zeros((2, 5)))
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.full((2, 12), np.nan))
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.full((2, 12), -1.0))
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.zeros((2, 12)), duchenne_kappa=-1.0)
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.zeros((2, 12)), f0_envelope=np.zeros(3))
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.zeros((2, 12)), f0_envelope=np.array([-1.0, 0.0]))


def test_asymmetric_blink_shape() -> None:
    blink = synthesize_asymmetric_blink_curve(0.5)
    assert blink.peak_closure == pytest.approx(1.0)
    assert blink.closure[0] == pytest.approx(0.0)
    assert blink.closure[-1] < 0.1
    peak_index = int(np.argmax(blink.closure))
    assert blink.time_s[peak_index] <= 0.075 + 1e-9


def test_blink_validation() -> None:
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.0)
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.5, onset_s=-1.0)
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.5, tau_down_s=0.0)
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.5, tau_up_s=0.05)
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.5, sample_rate_hz=0.0)


def test_subsurface_scattering_preserves_specular() -> None:
    rng = np.random.default_rng(7)
    diffuse = rng.random((16, 16, 3))
    specular = np.full_like(diffuse, 0.1)
    mask = np.ones((16, 16))
    composite = apply_dermal_subsurface_scattering(diffuse, specular, mask)
    assert composite.shape == diffuse.shape
    assert np.all(composite >= specular - 1e-9)
    assert not np.allclose(composite, diffuse + specular)


def test_subsurface_scattering_zero_sigma_and_partial_mask() -> None:
    diffuse = np.ones((8, 8, 3))
    specular = np.zeros_like(diffuse)
    mask = np.zeros((8, 8))
    mask[2:5, 2:5] = 1.0
    composite = apply_dermal_subsurface_scattering(
        diffuse, specular, mask, sigma_mm=(0.0, 0.0, 0.0)
    )
    assert np.allclose(composite, diffuse)


def test_subsurface_scattering_validation() -> None:
    good = np.zeros((8, 8, 3))
    mask = np.zeros((8, 8))
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(np.zeros((8, 8)), good, mask)
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(good, np.zeros((8, 8)), mask)
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(good, good, np.zeros((4, 4)))
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(good, good, mask, pixel_scale_mm=0.0)
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(good, good, mask, sigma_mm=(-1.0, 1.0, 1.0))
    bad = np.array(good, copy=True)
    bad[0, 0, 0] = np.nan
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(bad, good, mask)
