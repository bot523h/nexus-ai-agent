"""Uncertainty adapters over the W10X scientific engines."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.perception.analyzer import AudioPerception
from nexus_ai_agent.creative.perception.radiometric_scene_cut import (
    FrameTransition,
    TransitionKind,
)
from nexus_ai_agent.creative.temporal.biophysical_portrait import SkinChromophoreEstimate
from nexus_ai_agent.creative.verification.adapters import (
    ScientificVerificationBridge,
    _normal_quantile,
    chromophore_uncertainty,
    loudness_uncertainty,
    scene_cut_uncertainty,
    silence_uncertainty,
    slo_burn_uncertainty,
)
from nexus_ai_agent.creative.verification.uncertainty_gum import (
    GUMInputError,
    SpecificationLimits,
    VerificationOutcome,
)
from nexus_ai_agent.observability.slo_burn_calculus import BurnWindowResult


def _audio(integrated_lkfs: float = -14.0) -> AudioPerception:
    return AudioPerception(
        integrated_lkfs=integrated_lkfs,
        momentary_lkfs=(-14.0,),
        short_term_lkfs=(-14.0,),
        loudness_range_lu=5.0,
        true_peak_dbtp=-1.0,
        bark_centroid=120.0,
        silences=(),
    )


def _chromophores(melanin: float = 0.30, hemoglobin: float = 0.20) -> SkinChromophoreEstimate:
    return SkinChromophoreEstimate(
        melanin=melanin,
        hemoglobin=hemoglobin,
        residual_norm=0.01,
        reconstructed_optical_density=(0.10, 0.20, 0.30),
        clamped=False,
    )


def _window(successes: int = 990, failures: int = 10, *, target: float = 0.01) -> BurnWindowResult:
    total = successes + failures
    error_rate = failures / total if total else 0.0
    return BurnWindowResult(
        label="5m",
        successes=successes,
        failures=failures,
        total=total,
        error_rate=error_rate,
        burn_rate=error_rate / (1.0 - target),
        remaining_budget_fraction=0.5,
    )


# ------------------------------------------------------------- loudness


def test_loudness_uncertainty_is_positive_and_shrinks_with_more_samples() -> None:
    few = loudness_uncertainty(_audio(), sample_count=48_000)
    many = loudness_uncertainty(_audio(), sample_count=4_800_000)
    assert few.standard > 0.0
    assert many.standard < few.standard
    assert many.standard == pytest.approx(few.standard / math.sqrt(100.0))


def test_loudness_uncertainty_scales_with_quantisation_depth() -> None:
    sixteen = loudness_uncertainty(_audio(), bit_depth=16)
    twenty_four = loudness_uncertainty(_audio(), bit_depth=24)
    assert twenty_four.quantisation_step < sixteen.quantisation_step
    assert twenty_four.standard < sixteen.standard


def test_loudness_uncertainty_reports_the_mean_square_it_used() -> None:
    budget = loudness_uncertainty(_audio(-14.0))
    assert budget.mean_square == pytest.approx(10.0 ** ((-14.0 + 0.691) / 10.0))
    assert budget.expanded(2.0) == pytest.approx(2.0 * budget.standard)


def test_loudness_uncertainty_rejects_a_wrong_type() -> None:
    with pytest.raises(GUMInputError, match="AudioPerception"):
        loudness_uncertainty("nope")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True])
def test_loudness_uncertainty_rejects_unusable_bit_depths(bad: object) -> None:
    with pytest.raises(GUMInputError, match="bit_depth"):
        loudness_uncertainty(_audio(), bit_depth=bad)  # type: ignore[arg-type]


def test_loudness_uncertainty_rejects_unusable_sample_counts() -> None:
    with pytest.raises(GUMInputError, match="sample_count"):
        loudness_uncertainty(_audio(), sample_count=0)


def test_loudness_uncertainty_refuses_a_silent_measurement() -> None:
    with pytest.raises(GUMInputError, match="positive mean square"):
        loudness_uncertainty(_audio(-math.inf))


# ------------------------------------------------------------- scene cut


def test_scene_cut_uncertainty_shrinks_with_more_pixels() -> None:
    before = (0.9, 0.1)
    after = (0.1, 0.9)
    coarse = scene_cut_uncertainty(before, after, samples_per_histogram=1_000)
    fine = scene_cut_uncertainty(before, after, samples_per_histogram=100_000)
    assert coarse.standard > 0.0
    assert fine.standard < coarse.standard
    assert fine.js_divergence == pytest.approx(coarse.js_divergence)
    assert coarse.combined.has_correlation is True


def test_scene_cut_uncertainty_of_identical_histograms_is_zero() -> None:
    budget = scene_cut_uncertainty((0.5, 0.5), (0.5, 0.5), samples_per_histogram=1_000)
    assert budget.js_divergence == 0.0
    assert budget.standard == 0.0


def test_scene_cut_uncertainty_ignores_empty_bins() -> None:
    budget = scene_cut_uncertainty((1.0, 0.0), (0.0, 1.0), samples_per_histogram=1_000)
    assert math.isfinite(budget.standard)


def test_scene_cut_uncertainty_cross_checks_the_reported_divergence() -> None:
    before, after = (0.9, 0.1), (0.1, 0.9)
    reference = scene_cut_uncertainty(before, after, samples_per_histogram=1_000)
    matched = scene_cut_uncertainty(
        before,
        after,
        samples_per_histogram=1_000,
        reported_divergence=reference.js_divergence,
    )
    assert matched.js_divergence == pytest.approx(reference.js_divergence)
    with pytest.raises(GUMInputError, match="disagrees"):
        scene_cut_uncertainty(before, after, samples_per_histogram=1_000, reported_divergence=0.5)


def test_scene_cut_uncertainty_rejects_mismatched_bin_counts() -> None:
    with pytest.raises(GUMInputError, match="bin count"):
        scene_cut_uncertainty((1.0,), (0.5, 0.5), samples_per_histogram=10)


def test_scene_cut_uncertainty_rejects_empty_histograms() -> None:
    with pytest.raises(GUMInputError, match="must not be empty"):
        scene_cut_uncertainty((), (), samples_per_histogram=10)


@pytest.mark.parametrize("histogram", [(0.5, 0.4), (1.5, -0.5)])
def test_scene_cut_uncertainty_requires_normalised_histograms(
    histogram: tuple[float, float],
) -> None:
    with pytest.raises(GUMInputError, match="normalised|non-negative"):
        scene_cut_uncertainty(histogram, (0.5, 0.5), samples_per_histogram=10)


def test_scene_cut_uncertainty_rejects_unusable_sample_counts() -> None:
    with pytest.raises(GUMInputError, match="samples_per_histogram"):
        scene_cut_uncertainty((0.5, 0.5), (0.5, 0.5), samples_per_histogram=0)


@pytest.mark.parametrize("bad", [math.nan, True, "0.5"])
def test_scene_cut_uncertainty_rejects_non_finite_entries(bad: object) -> None:
    with pytest.raises(GUMInputError, match="before"):
        scene_cut_uncertainty((bad,), (1.0,), samples_per_histogram=10)  # type: ignore[arg-type]


def test_scene_cut_uncertainty_exposes_the_expanded_uncertainty() -> None:
    budget = scene_cut_uncertainty((0.9, 0.1), (0.1, 0.9), samples_per_histogram=1_000)
    assert budget.expanded(2.0) == pytest.approx(2.0 * budget.standard)


# --------------------------------------------------------- chromophores


def test_chromophore_uncertainty_reports_a_symmetric_covariance() -> None:
    budget = chromophore_uncertainty(_chromophores(), reflectance_sigma=0.01)
    assert budget.covariance[0][1] == pytest.approx(budget.covariance[1][0])
    assert budget.combined.standard > 0.0
    assert budget.condition_number >= 1.0
    assert budget.amplification > 0.0
    assert budget.budget().values == (0.30, 0.20)


def test_chromophore_uncertainty_grows_with_reflectance_noise() -> None:
    quiet = chromophore_uncertainty(_chromophores(), reflectance_sigma=0.01)
    noisy = chromophore_uncertainty(_chromophores(), reflectance_sigma=0.02)
    assert noisy.combined.standard == pytest.approx(2.0 * quiet.combined.standard)


def test_chromophore_uncertainty_rejects_a_wrong_type() -> None:
    with pytest.raises(GUMInputError, match="SkinChromophoreEstimate"):
        chromophore_uncertainty("nope", reflectance_sigma=0.01)  # type: ignore[arg-type]


def test_chromophore_uncertainty_rejects_negative_noise() -> None:
    with pytest.raises(GUMInputError, match="reflectance_sigma"):
        chromophore_uncertainty(_chromophores(), reflectance_sigma=-0.01)


def test_chromophore_uncertainty_rejects_mismatched_extinction_curves() -> None:
    with pytest.raises(GUMInputError, match="band count"):
        chromophore_uncertainty(
            _chromophores(),
            reflectance_sigma=0.01,
            melanin_extinction=(1.0, 2.0, 3.0),
            hemoglobin_extinction=(1.0, 2.0),
        )


def test_chromophore_uncertainty_rejects_empty_extinction_curves() -> None:
    with pytest.raises(GUMInputError, match="must not be empty"):
        chromophore_uncertainty(
            _chromophores(),
            reflectance_sigma=0.01,
            melanin_extinction=(),
            hemoglobin_extinction=(),
        )


def test_chromophore_uncertainty_rejects_a_negative_ridge() -> None:
    with pytest.raises(GUMInputError, match="ridge"):
        chromophore_uncertainty(_chromophores(), reflectance_sigma=0.01, ridge=-1.0)


def test_chromophore_uncertainty_detects_parallel_extinction_curves() -> None:
    curve = (1.0, 2.0, 3.0)
    with pytest.raises(GUMInputError, match="linearly dependent"):
        chromophore_uncertainty(
            _chromophores(),
            reflectance_sigma=0.01,
            melanin_extinction=curve,
            hemoglobin_extinction=curve,
            ridge=0.0,
        )


# --------------------------------------------------------------- SLO burn


def test_slo_burn_uncertainty_matches_the_binomial_model() -> None:
    budget = slo_burn_uncertainty(_window(), target=0.01)
    expected = math.sqrt(0.01 * 0.99 / 1000) / 0.99
    assert budget.standard == pytest.approx(expected)
    assert budget.observations == 1000
    assert budget.expanded(3.0) == pytest.approx(3.0 * expected)


def test_slo_burn_uncertainty_of_an_error_free_window_is_zero() -> None:
    budget = slo_burn_uncertainty(_window(successes=1000, failures=0), target=0.01)
    assert budget.standard == 0.0


def test_slo_burn_uncertainty_rejects_a_wrong_type() -> None:
    with pytest.raises(GUMInputError, match="BurnWindowResult"):
        slo_burn_uncertainty("nope", target=0.01)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, math.nan])
def test_slo_burn_uncertainty_rejects_unusable_targets(bad: float) -> None:
    with pytest.raises(GUMInputError):
        slo_burn_uncertainty(_window(), target=bad)


def test_slo_burn_uncertainty_refuses_a_window_scored_against_another_target() -> None:
    with pytest.raises(GUMInputError, match="disagrees"):
        slo_burn_uncertainty(_window(), target=0.05)


def test_slo_burn_uncertainty_rejects_an_empty_window() -> None:
    with pytest.raises(GUMInputError, match="window.total"):
        slo_burn_uncertainty(_window(successes=0, failures=0), target=0.01)


# ---------------------------------------------------------------- bridge


def test_bridge_gates_loudness_against_a_limit() -> None:
    bridge = ScientificVerificationBridge(coverage_factor=2.0)
    verdict = bridge.check_loudness(_audio(-14.0), SpecificationLimits(lower=-23.0))
    assert verdict.outcome is VerificationOutcome.PASS
    strict = bridge.check_loudness(_audio(-14.0), SpecificationLimits(lower=-14.0))
    assert strict.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE


def test_bridge_gates_a_scene_cut_against_a_limit() -> None:
    before, after = (0.9, 0.1), (0.1, 0.9)
    reference = scene_cut_uncertainty(before, after, samples_per_histogram=1_000)
    transition = FrameTransition(
        kind=TransitionKind.HARD_CUT,
        js_divergence=reference.js_divergence,
        js_distance=math.sqrt(reference.js_divergence),
        gradient_flux=0.0,
        entropy_before=1.0,
        entropy_after=1.0,
    )
    bridge = ScientificVerificationBridge()
    verdict = bridge.check_scene_cut(
        transition, before, after, SpecificationLimits(upper=1.0), samples_per_histogram=1_000
    )
    assert verdict.outcome is VerificationOutcome.PASS


def test_bridge_gates_a_burn_rate_against_a_budget() -> None:
    bridge = ScientificVerificationBridge()
    verdict = bridge.check_burn(_window(), SpecificationLimits(upper=2.0), target=0.01)
    assert verdict.outcome is VerificationOutcome.PASS
    breach = bridge.check_burn(_window(failures=100), SpecificationLimits(upper=0.05), target=0.01)
    assert breach.outcome is VerificationOutcome.FAIL


def test_bridge_rejects_an_unusable_coverage_factor() -> None:
    with pytest.raises(GUMInputError, match="coverage_factor"):
        ScientificVerificationBridge(coverage_factor=0.0)


# ---------------------------------------------------------------------------
# Wave A corrections: the arithmetic the audit found wrong
# ---------------------------------------------------------------------------


def test_disjoint_histograms_are_maximally_different() -> None:
    """One-hot histograms on different bins: JS = 1 bit, not 0."""
    budget = scene_cut_uncertainty((1.0, 0.0), (0.0, 1.0), samples_per_histogram=1_000)
    assert budget.js_divergence == pytest.approx(1.0, rel=1e-12)


def test_identical_histograms_have_zero_divergence() -> None:
    budget = scene_cut_uncertainty((0.5, 0.5), (0.5, 0.5), samples_per_histogram=1_000)
    assert budget.js_divergence == pytest.approx(0.0, abs=1e-15)
    assert budget.standard == pytest.approx(0.0, abs=1e-15)


def test_a_noisier_after_histogram_costs_more_uncertainty() -> None:
    """Both blocks propagate: the after sample count is not decoration."""
    fine = scene_cut_uncertainty(
        (0.9, 0.1),
        (0.1, 0.9),
        samples_per_histogram=1_000,
        samples_per_histogram_after=1_000,
    )
    coarse_after = scene_cut_uncertainty(
        (0.9, 0.1),
        (0.1, 0.9),
        samples_per_histogram=1_000,
        samples_per_histogram_after=1,
    )
    assert coarse_after.standard > 10.0 * fine.standard


def test_the_scene_cut_budget_matches_the_closed_form_two_block_propagation() -> None:
    """Independent re-derivation of u_c(JSD) from both multinomial blocks.

    Sigma_P, Sigma_Q and both gradients are rebuilt by hand from
    J = 0.5 KL(p||m) + 0.5 KL(q||m), then fed through the same GUM law. Any change
    to either gradient or to the block assembly breaks the equality, which is
    what makes the W12/W16/W17 mutants non-vacuous.
    """
    from nexus_ai_agent.creative.verification.uncertainty_gum import (
        UncertaintyInput,
        combine,
    )

    first, second, samples_p, samples_q = (0.9, 0.1), (0.1, 0.9), 1_000, 37
    midpoint = tuple(0.5 * (p + q) for p, q in zip(first, second, strict=True))

    def covariance(values: tuple[float, ...], samples: int) -> list[list[float]]:
        size = len(values)
        return [
            [
                (
                    values[row] * (1.0 - values[row])
                    if row == column
                    else -values[row] * values[column]
                )
                / samples
                for column in range(size)
            ]
            for row in range(size)
        ]

    gradient_p = tuple(
        0.5 * math.log(p / m) / math.log(2.0) for p, m in zip(first, midpoint, strict=True)
    )
    gradient_q = tuple(
        0.5 * math.log(q / m) / math.log(2.0) for q, m in zip(second, midpoint, strict=True)
    )
    sigma_p = covariance(first, samples_p)
    sigma_q = covariance(second, samples_q)
    size = len(first)
    # diag(Sigma_P, Sigma_Q): the two histograms are estimated independently.
    matrix = [
        [sigma_p[row][column] for column in range(size)] + [0.0] * size for row in range(size)
    ] + [[0.0] * size + [sigma_q[row][column] for column in range(size)] for row in range(size)]
    closed_form = combine(
        gradient_p + gradient_q,
        UncertaintyInput.from_covariance((0.0,) * (2 * size), matrix),
    )

    budget = scene_cut_uncertainty(
        first, second, samples_per_histogram=samples_p, samples_per_histogram_after=samples_q
    )
    assert budget.standard == pytest.approx(closed_form.standard, rel=1e-12)
    assert budget.standard > 0.0


def test_the_chromophore_condition_number_is_the_spectral_ratio() -> None:
    """kappa_2 of [[1, rho], [rho, 1]] is (1 + rho)/(1 - rho), never 1.0."""
    rho = 0.9
    estimate = SkinChromophoreEstimate(
        melanin=0.3,
        hemoglobin=0.2,
        residual_norm=0.01,
        reconstructed_optical_density=(0.3,),
        clamped=False,
    )
    budget = chromophore_uncertainty(
        estimate,
        reflectance_sigma=0.01,
        melanin_extinction=(1.0, 0.0),
        hemoglobin_extinction=(rho, math.sqrt(1.0 - rho * rho)),
        ridge=0.0,
    )
    assert budget.condition_number == pytest.approx((1.0 + rho) / (1.0 - rho), rel=1e-12)


def test_a_parallel_gramian_fails_closed_before_the_condition_is_reported() -> None:
    estimate = SkinChromophoreEstimate(
        melanin=0.3,
        hemoglobin=0.2,
        residual_norm=0.01,
        reconstructed_optical_density=(0.3,),
        clamped=False,
    )
    with pytest.raises(GUMInputError, match="linearly dependent"):
        chromophore_uncertainty(
            estimate,
            reflectance_sigma=0.01,
            melanin_extinction=(1.0, 0.0),
            hemoglobin_extinction=(1.0, 0.0),
            ridge=0.0,
        )


def test_a_nearly_parallel_gramian_reports_a_large_finite_condition_number() -> None:
    estimate = SkinChromophoreEstimate(
        melanin=0.3,
        hemoglobin=0.2,
        residual_norm=0.01,
        reconstructed_optical_density=(0.3,),
        clamped=False,
    )
    rho = 1.0 - 1e-6
    budget = chromophore_uncertainty(
        estimate,
        reflectance_sigma=0.01,
        melanin_extinction=(1.0, 0.0),
        hemoglobin_extinction=(rho, math.sqrt(1.0 - rho * rho)),
        ridge=0.0,
    )
    assert 1e5 < budget.condition_number < 1e7


def test_the_loudness_budget_is_level_dependent() -> None:
    """u(L) grows as the programme gets quieter; the old model had no level term."""
    quiet = loudness_uncertainty(_audio(-40.0)).combined.standard
    loud = loudness_uncertainty(_audio(-10.0)).combined.standard
    # 30 dB of level is 1000x the mean square, i.e. sqrt(1000) in u(L).
    assert quiet > 30.0 * loud > 0.0


def test_digital_silence_is_refused_instead_of_dividing_by_zero() -> None:
    with pytest.raises(GUMInputError, match="positive mean square"):
        loudness_uncertainty(_audio(-math.inf))


def test_a_bin_empty_in_both_histograms_contributes_exactly_zero() -> None:
    """``lim_{x -> 0+} x log(x/m) = 0``: a shared empty bin adds nothing to the JSD."""
    budget = scene_cut_uncertainty(
        (0.5, 0.5, 0.0, 0.0),
        (0.5, 0.0, 0.5, 0.0),
        samples_per_histogram=1_000,
    )
    # bin 0 identical; bins 1 and 2 are one-sided (0.25 bit each); bin 3 empty on
    # both sides. The empty bin must contribute exactly zero, not an error.
    assert budget.js_divergence == pytest.approx(0.5, rel=1e-12)
    assert budget.standard > 0.0


# ---------------------------------------------------------------------------
# silence-segment RMS uncertainty (Wave B)
# ---------------------------------------------------------------------------


def test_silence_uncertainty_matches_the_mean_square_variance_model() -> None:
    rms = -40.0
    samples = 48_000
    bit_depth = 16
    budget = silence_uncertainty(rms, -30.0, samples, bit_depth=bit_depth)
    step = 2.0 / (2**bit_depth - 1)
    mean_square = 10.0 ** (rms / 10.0)
    expected_variance = step**2 * mean_square / (3.0 * samples) + step**4 / (180.0 * samples)
    expected_u_db = 10.0 / (math.log(10.0) * mean_square) * math.sqrt(expected_variance)
    assert budget.mean_square == pytest.approx(mean_square)
    assert budget.quantisation_step == pytest.approx(step)
    assert budget.standard == pytest.approx(expected_u_db, rel=1e-12)
    assert budget.combined.variance == pytest.approx(expected_u_db**2, rel=1e-12)
    assert budget.combined.effective_dof == pytest.approx(samples - 1)


def test_silence_uncertainty_scales_with_sqrt_of_the_mean_square() -> None:
    quiet = silence_uncertainty(-40.0, -30.0, 48_000)
    loud = silence_uncertainty(-20.0, -30.0, 48_000)
    quiet_u_m = math.sqrt(quiet.combined.variance) / quiet.combined.sensitivity[0]
    loud_u_m = math.sqrt(loud.combined.variance) / loud.combined.sensitivity[0]
    # u(M) ~ sqrt(M) in the high-SNR regime, so a 100x level is a 10x deviation.
    assert loud_u_m / quiet_u_m == pytest.approx(10.0, rel=1e-6)
    # u(dB) = g u(M) with g ~ 1/M, so the reported deviation shrinks by 10x.
    assert loud.standard / quiet.standard == pytest.approx(0.1, rel=1e-6)


def test_silence_guard_band_passes_below_and_fails_above() -> None:
    below = silence_uncertainty(
        -40.0, -30.0, 72_000, window_duration_s=1.5, min_silence_duration_s=0.5
    )
    assert below.decision.outcome is VerificationOutcome.PASS
    above = silence_uncertainty(
        -20.0, -30.0, 72_000, window_duration_s=1.5, min_silence_duration_s=0.5
    )
    assert above.decision.outcome is VerificationOutcome.FAIL


def test_silence_guard_band_abstains_on_a_straddle() -> None:
    budget = silence_uncertainty(
        -30.0, -30.0, 72_000, window_duration_s=1.5, min_silence_duration_s=0.5
    )
    assert budget.decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    assert budget.decision.interval_low < -30.0 < budget.decision.interval_high


def test_the_finite_window_term_is_zero_without_durations() -> None:
    quantisation_only = silence_uncertainty(-40.0, -30.0, 48_000)
    with_window = silence_uncertainty(
        -40.0, -30.0, 48_000, window_duration_s=1.5, min_silence_duration_s=0.5
    )
    assert with_window.combined.variance > quantisation_only.combined.variance


@pytest.mark.parametrize(
    "overrides",
    [
        {"sample_count": 1},
        {"bit_depth": 0},
        {"window_duration_s": -1.0},
        {"min_silence_duration_s": -1.0},
        {"window_duration_s": 0.5, "min_silence_duration_s": 1.0},
        {"coverage_probability": 0.0},
        {"coverage_probability": 1.0},
        {"sample_count": True},
    ],
)
def test_silence_uncertainty_refuses_unusable_inputs(overrides: dict[str, object]) -> None:
    base: dict[str, object] = {"rms_db": -40.0, "threshold_db": -30.0, "sample_count": 48_000}
    base.update(overrides)
    with pytest.raises(GUMInputError):
        silence_uncertainty(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rms", "threshold"),
    [(math.inf, -30.0), (-40.0, math.nan), (-40.0, math.inf)],
)
def test_silence_uncertainty_refuses_non_finite_levels(rms: float, threshold: float) -> None:
    with pytest.raises(GUMInputError):
        silence_uncertainty(rms, threshold, 48_000)


def test_the_normal_quantile_covers_both_tails_and_is_symmetric() -> None:
    low = _normal_quantile(0.01)
    high = _normal_quantile(0.99)
    assert low == pytest.approx(-high, rel=1e-12)
    assert low < 0.0 < high
    assert _normal_quantile(0.5) == pytest.approx(0.0, abs=1e-12)


def test_silence_expanded_property_matches_the_decision() -> None:
    budget = silence_uncertainty(-40.0, -30.0, 48_000)
    assert budget.expanded == budget.decision.expanded_uncertainty


def test_silence_refuses_a_level_that_underflows_to_zero() -> None:
    with pytest.raises(GUMInputError):
        silence_uncertainty(-4000.0, -30.0, 48_000)
