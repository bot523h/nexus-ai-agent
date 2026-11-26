"""The dependency-free loudness model, exercised on its own contracts."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.verification.loudness import (
    DECIBEL_PER_NEPER,
    LUFS_OFFSET,
    loudness_budget,
    mean_square_deviation,
    mean_square_from_lkfs,
    quantisation_step,
)
from nexus_ai_agent.creative.verification.uncertainty_gum import GUMInputError


def test_the_quantisation_step_halves_per_extra_bit() -> None:
    assert quantisation_step(8) == pytest.approx(2.0 / 256.0, rel=1e-15)
    assert quantisation_step(16) == pytest.approx(quantisation_step(8) / 256.0, rel=1e-15)
    assert quantisation_step(1) == 1.0


@pytest.mark.parametrize("bad", [0, -8])
def test_the_bit_depth_must_be_positive(bad: int) -> None:
    with pytest.raises(GUMInputError, match="bit_depth must be positive"):
        quantisation_step(bad)


@pytest.mark.parametrize("bad", [True, 16.0, "16"])
def test_the_bit_depth_must_be_an_int_not_a_bool_or_float(bad: object) -> None:
    with pytest.raises(GUMInputError, match="bit_depth must be an int"):
        quantisation_step(bad)  # type: ignore[arg-type]


def test_mean_square_inverts_the_bs1770_curve() -> None:
    for lkfs in (-40.0, -23.0, -14.0, -5.0):
        mean_square = mean_square_from_lkfs(lkfs)
        assert LUFS_OFFSET + 10.0 * math.log10(mean_square) == pytest.approx(lkfs, abs=1e-12)


def test_digital_silence_has_no_mean_square() -> None:
    with pytest.raises(GUMInputError, match="positive mean square"):
        mean_square_from_lkfs(-math.inf)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_non_finite_loudness_is_refused(bad: float) -> None:
    with pytest.raises(GUMInputError, match="must be finite"):
        mean_square_from_lkfs(bad)


@pytest.mark.parametrize("bad", [True, "loud", None])
def test_a_non_numeric_loudness_is_refused(bad: object) -> None:
    with pytest.raises(GUMInputError, match="must be a real number"):
        mean_square_from_lkfs(bad)  # type: ignore[arg-type]


def test_the_mean_square_deviation_shrinks_as_one_over_sqrt_samples() -> None:
    deviation = mean_square_deviation(step=quantisation_step(16), mean_square=0.05, samples=1_000)
    more = mean_square_deviation(step=quantisation_step(16), mean_square=0.05, samples=4_000)
    assert deviation / more == pytest.approx(2.0, rel=1e-12)


def test_the_mean_square_deviation_has_the_level_term_the_sample_model_lacked() -> None:
    """``q^2 M/(3N)`` scales with sqrt(M); ``sqrt(q^2/(12N))`` ignored the level."""
    step = quantisation_step(16)
    quiet = mean_square_deviation(step=step, mean_square=1e-4, samples=48_000)
    loud = mean_square_deviation(step=step, mean_square=1e-2, samples=48_000)
    # 100x the mean square is ~10x the deviation once the head term dominates.
    assert loud > 9.0 * quiet > 0.0
    head = step**2 * 1e-2 / (3.0 * 48_000)
    tail = step**4 / (180.0 * 48_000)
    assert loud == pytest.approx(math.sqrt(head + tail), rel=1e-12)


@pytest.mark.parametrize("step", [0.0, -1e-6])
def test_a_non_positive_step_is_refused(step: float) -> None:
    with pytest.raises(GUMInputError, match="step must be positive"):
        mean_square_deviation(step=step, mean_square=0.05, samples=10)


@pytest.mark.parametrize("step", [math.nan, math.inf, True, "1"])
def test_a_non_finite_step_is_refused(step: object) -> None:
    with pytest.raises(GUMInputError, match="step must be a finite real number"):
        mean_square_deviation(step=step, mean_square=0.05, samples=10)  # type: ignore[arg-type]


@pytest.mark.parametrize("power", [0.0, -0.5, math.nan, True, "0.5"])
def test_a_non_positive_or_non_finite_mean_square_is_refused(power: object) -> None:
    with pytest.raises(GUMInputError, match="mean_square must be"):
        mean_square_deviation(step=1e-3, mean_square=power, samples=10)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_a_non_positive_or_non_int_sample_count_is_refused(bad: object) -> None:
    with pytest.raises(GUMInputError, match="samples must be"):
        mean_square_deviation(step=1e-3, mean_square=0.05, samples=bad)  # type: ignore[arg-type]


def test_the_budget_is_the_propagation_of_the_mean_square_deviation() -> None:
    lkfs, mean_square, step, combined = loudness_budget(-14.0, bit_depth=16, sample_count=48_000)
    assert lkfs == -14.0
    assert step == quantisation_step(16)
    expected = (
        DECIBEL_PER_NEPER
        / mean_square
        * mean_square_deviation(step=step, mean_square=mean_square, samples=48_000)
    )
    assert combined.standard == pytest.approx(expected, rel=1e-12)
    assert combined.effective_dof == math.inf


def test_the_budget_grows_as_the_programme_gets_quieter() -> None:
    quiet = loudness_budget(-40.0)[3].standard
    loud = loudness_budget(-10.0)[3].standard
    assert quiet > 30.0 * loud > 0.0


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_the_budget_refuses_a_non_finite_level(bad: float) -> None:
    with pytest.raises(GUMInputError, match="must be finite"):
        loudness_budget(bad)


@pytest.mark.parametrize("bad", [True, "loud"])
def test_the_budget_refuses_a_non_numeric_level(bad: object) -> None:
    with pytest.raises(GUMInputError, match="must be a real number"):
        loudness_budget(bad)  # type: ignore[arg-type]


def test_the_budget_refuses_digital_silence() -> None:
    with pytest.raises(GUMInputError, match="positive mean square"):
        loudness_budget(-math.inf)
