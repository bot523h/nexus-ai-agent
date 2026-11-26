"""JCGM 100:2008 propagation, Welch-Satterthwaite and the guard-band decision."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.verification.uncertainty_gum import (
    CombinedUncertainty,
    GuardBandDecision,
    GUMCovarianceError,
    GUMInputError,
    SpecificationLimits,
    UncertaintyInput,
    VerificationOutcome,
    combine,
    evaluate_guard_band,
    evaluate_threshold,
    welch_satterthwaite,
)


def _independent(value: float, deviation: float) -> CombinedUncertainty:
    return combine((1.0,), UncertaintyInput.from_standard((value,), (deviation,)))


# --------------------------------------------------------------- input guards


@pytest.mark.parametrize("bad", [True, False])
def test_boolean_estimates_are_rejected(bad: bool) -> None:
    with pytest.raises(GUMInputError, match="must be a real number"):
        UncertaintyInput.from_standard((bad,), (0.1,))


@pytest.mark.parametrize("bad", ["1.0", None, object()])
def test_non_numeric_estimates_are_rejected(bad: object) -> None:
    with pytest.raises(GUMInputError, match="must be a real number"):
        UncertaintyInput.from_standard((bad,), (0.1,))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_estimates_are_rejected(bad: float) -> None:
    with pytest.raises(GUMInputError, match="must be finite"):
        UncertaintyInput.from_standard((bad,), (0.1,))


def test_negative_standard_uncertainty_is_rejected() -> None:
    with pytest.raises(GUMInputError, match="non-negative"):
        UncertaintyInput.from_standard((1.0,), (-0.1,))


def test_empty_input_vector_is_rejected() -> None:
    with pytest.raises(GUMInputError, match="at least one input estimate"):
        UncertaintyInput.from_standard((), ())


def test_covariance_row_count_must_match_estimates() -> None:
    with pytest.raises(GUMInputError, match="rows for"):
        UncertaintyInput.from_covariance((1.0, 2.0), ((1.0, 0.0),))


def test_covariance_row_width_must_match_estimates() -> None:
    with pytest.raises(GUMInputError, match="entries for"):
        UncertaintyInput.from_covariance((1.0, 2.0), ((1.0, 0.0), (0.0,)))


def test_asymmetric_covariance_is_rejected() -> None:
    with pytest.raises(GUMCovarianceError, match="must be symmetric"):
        UncertaintyInput.from_covariance((1.0, 2.0), ((1.0, 0.5), (0.4, 4.0)))


def test_negative_variance_diagonal_is_rejected() -> None:
    with pytest.raises(GUMCovarianceError, match="non-negative"):
        UncertaintyInput.from_covariance((1.0,), ((-1.0,),))


def test_cauchy_schwarz_violation_is_rejected() -> None:
    with pytest.raises(GUMCovarianceError, match="Cauchy-Schwarz"):
        UncertaintyInput.from_covariance((1.0, 2.0), ((1.0, 5.0), (5.0, 4.0)))


def test_from_covariance_derives_deviations_from_the_diagonal() -> None:
    budget = UncertaintyInput.from_covariance((1.0, 2.0), ((4.0, 1.0), (1.0, 9.0)))
    assert budget.standard_uncertainties == (2.0, 3.0)
    assert budget.values == (1.0, 2.0)


# ----------------------------------------------------------------- propagation


def test_independent_propagation_matches_the_textbook_sum() -> None:
    budget = UncertaintyInput.from_standard((1.0, 2.0), (0.1, 0.2))
    combined = combine((3.0, 4.0), budget)
    expected = math.sqrt((3.0 * 0.1) ** 2 + (4.0 * 0.2) ** 2)
    assert combined.variance == pytest.approx(expected**2)
    assert combined.standard == pytest.approx(expected)
    assert combined.has_correlation is False
    assert combined.linearisation_penalty == 0.0


def test_correlated_propagation_includes_the_cross_terms() -> None:
    budget = UncertaintyInput.from_covariance((1.0, 2.0), ((0.04, 0.02), (0.02, 0.09)))
    combined = combine((3.0, 4.0), budget)
    expected = (3.0 * 0.2) ** 2 + (4.0 * 0.3) ** 2 + 2 * 3.0 * 4.0 * 0.02
    assert combined.variance == pytest.approx(expected)
    assert combined.has_correlation is True
    assert combined.linearisation_penalty > 0.0


def test_jacobian_length_must_match_the_inputs() -> None:
    budget = UncertaintyInput.from_standard((1.0, 2.0), (0.1, 0.2))
    with pytest.raises(GUMInputError, match="entries for"):
        combine((1.0,), budget)


def test_non_finite_sensitivity_is_rejected() -> None:
    budget = UncertaintyInput.from_standard((1.0,), (0.1,))
    with pytest.raises(GUMInputError, match="jacobian"):
        combine((math.nan,), budget)


def test_tiny_negative_variance_is_clamped_to_zero() -> None:
    # Pairwise Cauchy-Schwarz holds (|1 - 1e-13| <= 1) but g^T Sigma g is -2e-13.
    budget = UncertaintyInput.from_covariance(
        (1.0, 1.0), ((1.0, -1.0 - 1e-13), (-1.0 - 1e-13, 1.0))
    )
    combined = combine((1.0, 1.0), budget)
    assert combined.variance == 0.0
    assert combined.standard == 0.0


def test_pairwise_cauchy_schwarz_is_not_enough_for_three_inputs() -> None:
    """``|r_ij| <= 1`` everywhere and still not PSD: lambda = (1.6, 1.6, -0.2)."""
    with pytest.raises(GUMCovarianceError, match="not positive semi-definite"):
        UncertaintyInput.from_covariance(
            (1.0, 1.0, 1.0),
            ((1.0, -0.6, -0.6), (-0.6, 1.0, -0.6), (-0.6, -0.6, 1.0)),
        )


def test_a_singular_psd_covariance_is_accepted_and_propagates_to_zero() -> None:
    """Rank-deficient is legal (identical inputs); only a negative eigenvalue is not."""
    budget = UncertaintyInput.from_covariance(
        (1.0, 1.0, 1.0), ((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0))
    )
    assert budget.standard_uncertainties == (1.0, 1.0, 1.0)
    combined = combine((1.0, -1.0, 0.0), budget)
    assert combined.variance == 0.0
    assert combined.standard == 0.0


def test_a_materially_negative_propagation_cannot_be_built() -> None:
    """The defect is refused where the matrix enters, not where it is used."""
    with pytest.raises(GUMCovarianceError):
        UncertaintyInput.from_covariance(
            (1.0, 1.0, 1.0), ((1.0, 1.0, 1.0), (1.0, 1.0, -1.0), (1.0, -1.0, 1.0))
        )


def test_expanded_uncertainty_scales_with_the_coverage_factor() -> None:
    combined = _independent(10.0, 0.5)
    assert combined.expanded(2.0) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_expanded_uncertainty_rejects_unusable_coverage_factors(bad: float) -> None:
    combined = _independent(10.0, 0.5)
    with pytest.raises(GUMInputError):
        combined.expanded(bad)


# ------------------------------------------------------ Welch-Satterthwaite


def test_welch_satterthwaite_without_freedoms_is_infinite() -> None:
    assert welch_satterthwaite((1.0, 1.0)) == math.inf


def test_welch_satterthwaite_matches_the_closed_form() -> None:
    contributions = (0.25, 0.25)
    freedoms = (10.0, 40.0)
    expected = (0.5**2) / (0.25**2 / 10.0 + 0.25**2 / 40.0)
    assert welch_satterthwaite(contributions, freedoms) == pytest.approx(expected)


def test_welch_satterthwaite_is_capped_by_the_freedom_sum() -> None:
    # Two equal contributions, one freedom finite and one exact: the uncapped
    # value is 4.0, which exceeds the single finite freedom, so GUM G.4.1 caps it.
    assert welch_satterthwaite((1.0, 1.0), (1.0, math.inf)) == pytest.approx(1.0)


def test_welch_satterthwaite_all_infinite_freedoms_is_infinite() -> None:
    assert welch_satterthwaite((1.0,), (math.inf,)) == math.inf


def test_welch_satterthwaite_zero_denominator_is_infinite() -> None:
    assert welch_satterthwaite((0.0,), (4.0,)) == math.inf


def test_welch_satterthwaite_length_mismatch_is_rejected() -> None:
    with pytest.raises(GUMInputError, match="degrees of freedom"):
        welch_satterthwaite((1.0, 2.0), (4.0,))


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_welch_satterthwaite_rejects_non_positive_freedoms(bad: float) -> None:
    with pytest.raises(GUMInputError, match="must be positive"):
        welch_satterthwaite((1.0,), (bad,))


def test_welch_satterthwaite_rejects_negative_contributions() -> None:
    with pytest.raises(GUMInputError, match="contributions"):
        welch_satterthwaite((-1.0,), (4.0,))


def test_combine_reports_the_classical_dof_for_correlated_budgets() -> None:
    budget = UncertaintyInput.from_covariance((1.0, 2.0), ((0.04, 0.02), (0.02, 0.09)))
    combined = combine((3.0, 4.0), budget, degrees_of_freedom=(5.0, 7.0))
    assert combined.effective_dof > 0.0
    assert combined.has_correlation is True


# ------------------------------------------------------------------ limits


def test_limits_require_at_least_one_bound() -> None:
    with pytest.raises(GUMInputError, match="at least one"):
        SpecificationLimits()


@pytest.mark.parametrize("lower,upper", [(1.0, 1.0), (2.0, 1.0)])
def test_limits_require_a_strict_order(lower: float, upper: float) -> None:
    with pytest.raises(GUMInputError, match="strictly below"):
        SpecificationLimits(lower=lower, upper=upper)


def test_limits_reject_non_finite_bounds() -> None:
    with pytest.raises(GUMInputError, match="must be finite"):
        SpecificationLimits(upper=math.inf)


# ------------------------------------------------------- three-valued gating


def test_interval_inside_the_region_passes() -> None:
    decision = evaluate_guard_band(
        10.0, _independent(10.0, 0.5), SpecificationLimits(upper=12.0), 2.0
    )
    assert decision.outcome is VerificationOutcome.PASS
    assert decision.is_conclusive is True
    assert (decision.interval_low, decision.interval_high) == pytest.approx((9.0, 11.0))


def test_interval_outside_the_region_fails() -> None:
    decision = evaluate_guard_band(
        20.0, _independent(20.0, 0.5), SpecificationLimits(upper=12.0), 2.0
    )
    assert decision.outcome is VerificationOutcome.FAIL
    assert decision.is_conclusive is True


def test_interval_straddling_the_limit_abstains() -> None:
    decision = evaluate_guard_band(
        12.0, _independent(12.0, 0.5), SpecificationLimits(upper=12.0), 2.0
    )
    assert decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    assert decision.is_conclusive is False


def test_interval_touching_the_limit_abstains_instead_of_passing() -> None:
    # high == upper exactly: containment is not strict, so the verdict must abstain.
    decision = evaluate_guard_band(
        11.0, _independent(11.0, 0.5), SpecificationLimits(upper=12.0), 2.0
    )
    assert decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE


def test_interval_touching_the_limit_abstains_instead_of_failing() -> None:
    # high == lower exactly: separation is not strict, so the verdict must abstain.
    decision = evaluate_guard_band(
        9.0, _independent(9.0, 0.5), SpecificationLimits(lower=10.0), 2.0
    )
    assert decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE


def test_two_sided_region_passes_only_when_fully_inside() -> None:
    limits = SpecificationLimits(lower=0.0, upper=10.0)
    assert evaluate_guard_band(5.0, _independent(5.0, 1.0), limits, 2.0).outcome is (
        VerificationOutcome.PASS
    )
    assert evaluate_guard_band(9.5, _independent(9.5, 1.0), limits, 2.0).outcome is (
        VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    )


def test_zero_uncertainty_is_a_point_decision() -> None:
    limits = SpecificationLimits(upper=1.0)
    assert evaluate_guard_band(0.5, _independent(0.5, 0.0), limits, 2.0).outcome is (
        VerificationOutcome.PASS
    )
    assert evaluate_guard_band(2.0, _independent(2.0, 0.0), limits, 2.0).outcome is (
        VerificationOutcome.FAIL
    )


def test_guard_band_rejects_wrong_types() -> None:
    with pytest.raises(GUMInputError, match="CombinedUncertainty"):
        evaluate_guard_band(1.0, "nope", SpecificationLimits(upper=2.0), 2.0)  # type: ignore[arg-type]
    with pytest.raises(GUMInputError, match="SpecificationLimits"):
        evaluate_guard_band(1.0, _independent(1.0, 0.1), "nope", 2.0)  # type: ignore[arg-type]


def test_guard_band_rejects_a_non_finite_value() -> None:
    with pytest.raises(GUMInputError, match="value"):
        evaluate_guard_band(math.nan, _independent(1.0, 0.1), SpecificationLimits(upper=2.0), 2.0)


def test_guard_band_records_its_inputs() -> None:
    combined = _independent(3.0, 0.25)
    limits = SpecificationLimits(upper=4.0)
    decision = evaluate_guard_band(3.0, combined, limits, 3.0)
    assert isinstance(decision, GuardBandDecision)
    assert decision.limits is limits
    assert decision.coverage_factor == 3.0
    assert decision.expanded_uncertainty == pytest.approx(0.75)
    assert decision.effective_dof == math.inf


# ------------------------------------------------------------- threshold API


def test_threshold_upper_direction_passes_and_abstains() -> None:
    assert evaluate_threshold(1.0, 0.1, 1.5, 2.0, "max").outcome is VerificationOutcome.PASS
    assert (
        evaluate_threshold(1.5, 0.1, 1.5, 2.0, "max").outcome
        is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    )


def test_threshold_lower_direction_passes_and_fails() -> None:
    assert evaluate_threshold(10.0, 0.1, 9.0, 2.0, "min").outcome is VerificationOutcome.PASS
    assert evaluate_threshold(8.0, 0.1, 9.0, 2.0, "min").outcome is VerificationOutcome.FAIL


def test_threshold_rejects_an_unknown_direction() -> None:
    with pytest.raises(GUMInputError, match="direction"):
        evaluate_threshold(1.0, 0.1, 1.0, 2.0, "sideways")


def test_threshold_rejects_a_negative_uncertainty() -> None:
    with pytest.raises(GUMInputError, match="non-negative"):
        evaluate_threshold(1.0, -0.1, 1.0, 2.0, "max")


def test_threshold_rejects_a_non_finite_uncertainty() -> None:
    with pytest.raises(GUMInputError, match="standard_uncertainty"):
        evaluate_threshold(1.0, math.inf, 1.0, 2.0, "max")
