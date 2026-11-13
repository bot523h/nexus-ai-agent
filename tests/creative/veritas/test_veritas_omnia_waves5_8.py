"""Regression gates for temporal continuity and independent facial channels."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    madelung_continuity_residual,
    stabilize_madelung_phase_coherence,
)
from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    audit_and_heal_viseme_formant_sync,
    enforce_helmholtz_fluid_conservation,
)


def test_temporal_continuity_is_measured_and_reduced() -> None:
    previous = np.ones((9, 5))
    density = previous * 2.0
    phase = np.zeros_like(density)
    result = stabilize_madelung_phase_coherence(density, phase, previous_density=previous)
    assert result.continuity_residual_before == pytest.approx(24.0)
    assert result.continuity_residual_after < result.continuity_residual_before
    assert result.continuity_residual_after <= 0.25
    assert "madelung_continuity_residual_exceeded" in result.violations
    assert not result.violations_after
    assert result.continuity_residual_after == pytest.approx(
        madelung_continuity_residual(
            result.density, result.phase, previous_density=previous, dt=1 / 24
        )
    )
    np.testing.assert_array_equal(density, previous * 2.0)
    np.testing.assert_allclose(np.abs(result.wavefunction) ** 2, result.density)


def test_zero_iteration_budget_reports_remaining_continuity_and_boiling() -> None:
    density = np.ones((8, 8)) * 2
    phase = np.indices((8, 8)).sum(axis=0) % 2
    result = stabilize_madelung_phase_coherence(
        density, phase.astype(float), previous_density=np.ones((8, 8)), max_iterations=0
    )
    assert "madelung_continuity_residual_exceeded" in result.violations_after
    assert "temporal_phase_boiling" in result.violations_after


@pytest.mark.parametrize("case", ["dimension", "finite", "limit", "dt", "negative_previous"])
def test_phase_rejects_invalid_inputs(case: str) -> None:
    density = np.ones((4, 4))
    phase = np.zeros_like(density)
    if case == "dimension":
        with pytest.raises(ValueError, match="at least 2-D"):
            stabilize_madelung_phase_coherence(np.ones(4), np.zeros(4))
    elif case == "finite":
        phase[0, 0] = np.nan
        with pytest.raises(ValueError, match="finite"):
            stabilize_madelung_phase_coherence(density, phase)
    elif case == "limit":
        with pytest.raises(ValueError, match="continuity_residual_max"):
            stabilize_madelung_phase_coherence(density, phase, continuity_residual_max=0)
    elif case == "dt":
        with pytest.raises(ValueError, match="finite"):
            stabilize_madelung_phase_coherence(density, phase, dt=np.nan)
    else:
        with pytest.raises(ValueError, match="non-negative"):
            stabilize_madelung_phase_coherence(density, phase, previous_density=-density)


@pytest.mark.parametrize("case", ["finite", "previous_shape", "previous_finite"])
def test_continuity_rejects_invalid_inputs(case: str) -> None:
    density = np.ones((4, 4))
    phase = np.zeros_like(density)
    with pytest.raises(ValueError):
        if case == "finite":
            madelung_continuity_residual(density * np.nan, phase)
        elif case == "previous_shape":
            madelung_continuity_residual(density, phase, previous_density=np.ones((3, 3)))
        else:
            madelung_continuity_residual(density, phase, previous_density=density * np.nan)


def test_au18_healing_does_not_overwrite_au20() -> None:
    au20 = np.full(4, 0.2)
    au18 = np.zeros(4)
    result = audit_and_heal_viseme_formant_sync(
        np.ones(4),
        au20,
        f1_hz=np.full(4, 700.0),
        au18_lip_pucker=au18,
        f2_hz=np.full(4, 800.0),
    )
    np.testing.assert_array_equal(result.au20_healed, au20)
    assert result.au18_lip_pucker_healed is not None
    np.testing.assert_allclose(result.au18_lip_pucker_healed, 0.48)
    np.testing.assert_array_equal(au18, np.zeros(4))
    assert "lip_rounding_underarticulation" in result.violations_before
    assert not result.violations_after


@pytest.mark.parametrize("case", ["shape", "finite", "range"])
def test_au18_rejects_invalid_input(case: str) -> None:
    pucker = {"shape": np.zeros(3), "finite": np.full(4, np.nan), "range": np.full(4, 2)}[case]
    with pytest.raises(ValueError):
        audit_and_heal_viseme_formant_sync(
            np.ones(4), np.zeros(4), f1_hz=np.full(4, 700.0), au18_lip_pucker=pucker
        )


def test_rectangular_odd_grid_projection() -> None:
    rng = np.random.default_rng(23)
    result = enforce_helmholtz_fluid_conservation(rng.normal(size=(2, 9, 5, 2)), np.ones((2, 9, 5)))
    assert result.divergence_after < 1e-12


def test_auditor_uses_temporal_density_and_independent_pucker() -> None:
    from nexus_ai_agent.creative.veritas.reality_auditor import CinemaShot, VeritasRealityAuditor

    density = np.ones((9, 5))
    shot = CinemaShot(
        phase=(density * 2, np.zeros_like(density), density),
        viseme=(
            np.ones(4),
            np.full(4, 0.2),
            np.full(4, 700.0),
            np.ones(4),
            np.zeros(4),
            np.full(4, 800.0),
        ),
    )
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    assert "madelung_continuity_residual_exceeded" in passport.details_before[11]
    assert "lip_rounding_underarticulation" in passport.details_before[13]
    assert not passport.details_after[11]
    assert not passport.details_after[13]
