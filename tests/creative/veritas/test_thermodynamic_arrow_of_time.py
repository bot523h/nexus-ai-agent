"""Unit tests for Law 9 — thermodynamic arrow of time and retarded causality."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    ThermodynamicError,
    audit_and_enforce_thermodynamic_arrow,
    fourier_advection_diffusion,
    retarded_wavefront,
    spatial_entropy,
    verify_retarded_causality,
)


def test_spatial_entropy_extremes() -> None:
    uniform = np.ones((4, 4))
    assert spatial_entropy(uniform) == pytest.approx(np.log(16.0))
    delta = np.zeros((4, 4))
    delta[0, 0] = 1.0
    assert spatial_entropy(delta) == pytest.approx(0.0)


def test_spatial_entropy_batch_and_validation() -> None:
    batch = np.stack([np.ones((3, 3)), np.eye(3)])
    entropies = spatial_entropy(batch)
    assert entropies.shape == (2,)
    assert entropies[0] > entropies[1]
    with pytest.raises(ThermodynamicError):
        spatial_entropy(np.ones(4))
    with pytest.raises(ThermodynamicError):
        spatial_entropy(-np.ones((2, 2)))


def test_fourier_diffusion_conserves_mass_and_smooths() -> None:
    field = np.zeros((16, 16))
    field[8, 8] = 1.0
    diffused = fourier_advection_diffusion(field, (0.0, 0.0), 0.1, 0.01, 1.0)
    assert diffused.sum() == pytest.approx(field.sum(), abs=1e-9)
    assert diffused.max() < field.max()
    assert spatial_entropy(np.clip(diffused, 0.0, None)) > spatial_entropy(field)


def test_fourier_advection_shifts_peak() -> None:
    field = np.zeros((16, 16))
    field[8, 8] = 1.0
    shifted = fourier_advection_diffusion(field, (1.0, 0.0), 0.0, 1.0, 1.0)
    assert int(np.argmax(shifted)) != 8 * 16 + 8


def test_fourier_validation() -> None:
    field = np.ones((4, 4))
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(np.ones(4), (0.0, 0.0), 0.1, 0.01, 1.0)
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(field, (0.0,), 0.1, 0.01, 1.0)
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(field, (0.0, 0.0), -0.1, 0.01, 1.0)
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(field, (0.0, 0.0), 0.1, 0.0, 1.0)


def test_retarded_wavefront_is_causal() -> None:
    wave = retarded_wavefront(
        (12, 12), (5.0, 5.0), impact_time_s=2.0, c_max=2.0, frames=6, dx=1.0, dt=1.0
    )
    assert wave.shape == (6, 12, 12)
    # Before impact the field is exactly zero.
    assert np.allclose(wave[:2], 0.0)
    assert verify_retarded_causality(wave, (5.0, 5.0), 2.0, 2.0)


def test_acausal_wavefront_is_rejected() -> None:
    wave = retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
    wave[0, 0, 0] = 5.0  # signal before the impact -> acausal
    assert not verify_retarded_causality(wave, (4.0, 4.0), 1.0, 1.5)


def test_retarded_validation() -> None:
    with pytest.raises(ThermodynamicError):
        retarded_wavefront((0, 4), (0.0, 0.0), 0.0, 1.0)
    with pytest.raises(ThermodynamicError):
        retarded_wavefront((4, 4), (0.0, 0.0), 0.0, -1.0)
    with pytest.raises(ThermodynamicError):
        verify_retarded_causality(np.zeros((2, 2)), (0.0, 0.0), 0.0, 1.0)


def test_audit_heals_entropy_reversal() -> None:
    frames = np.zeros((3, 8, 8))
    frames[0] = 1.0  # uniform -> maximum entropy
    frames[1, 0, 0] = 1.0  # concentrated -> low entropy
    frames[2, 0, 0] = 1.0
    audit = audit_and_enforce_thermodynamic_arrow(frames)
    assert "entropy_decrease" in audit.violations_before
    assert audit.violations_after == ()
    assert audit.healed
    assert audit.healed_frames.shape == frames.shape
    assert np.all(np.diff(audit.entropy_after) >= -1e-9)


def test_audit_validation() -> None:
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.ones((2, 2)))
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.ones((2, 2, 2)), dx=0.0)
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.ones((2, 2, 2)), diffusivity=-1.0)
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.ones((2, 2, 2)), buoyancy=-1.0)
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.ones((2, 2, 2)), tolerance=-1.0)


def test_frame_validation_branches() -> None:
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.zeros((0, 2, 2)))
    bad = np.ones((2, 2, 2))
    bad[0, 0, 0] = np.nan
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(bad)
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(-np.ones((2, 2, 2)))


def test_fourier_and_wave_validation_branches() -> None:
    bad = np.ones((4, 4))
    bad[0, 0] = np.nan
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(bad, (0.0, 0.0), 0.1, 0.01, 1.0)
    with pytest.raises(ThermodynamicError):
        retarded_wavefront((4, 4), (0.0, 0.0), 0.0, 1.0, frames=0)
    with pytest.raises(ThermodynamicError):
        retarded_wavefront((4, 4), (0.0, 0.0), -1.0, 1.0)
    with pytest.raises(ThermodynamicError):
        verify_retarded_causality(np.zeros((2, 4, 4)), (0.0, 0.0), 0.0, -1.0)


def test_acausal_before_impact_is_rejected() -> None:
    wave = np.zeros((3, 6, 6))
    wave[0, 0, 0] = 1.0  # amplitude before impact time
    assert not verify_retarded_causality(wave, (0.0, 0.0), 5.0, 1.0)


def test_buoyancy_reversal_and_shift() -> None:
    frames = np.zeros((3, 6, 6))
    frames[0, 0, 0] = 1.0  # blob at the top
    frames[1, 4, 0] = 1.0  # blob sinks downward -> centre of mass rises
    frames[2, 5, 0] = 1.0
    audit = audit_and_enforce_thermodynamic_arrow(frames, buoyancy=1.0)
    assert "buoyancy_reversal" in audit.violations_before
    assert audit.healed_frames.shape == frames.shape
