"""Uncertainty adapters for the W10X scientific engines.

Each adapter derives a *first-order* uncertainty budget from a real engine output
and states the model it assumes. Nothing here invents a confidence interval: the
budgets are the classical quantisation, multinomial, least-squares and binomial
propagations, and each docstring names the term that is **not** modelled so a
reviewer can see the gap instead of trusting a number.

The bridge then turns a budget into a :class:`GuardBandDecision`, so a
scientific measurement either passes with margin, fails with margin, or abstains.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from nexus_ai_agent.creative.perception.analyzer import AudioPerception
from nexus_ai_agent.creative.perception.radiometric_scene_cut import FrameTransition
from nexus_ai_agent.creative.temporal.biophysical_portrait import (
    HEMOGLOBIN_EXTINCTION,
    MELANIN_EXTINCTION,
    SkinChromophoreEstimate,
)
from nexus_ai_agent.observability.slo_burn_calculus import BurnWindowResult

from .uncertainty_gum import (
    CombinedUncertainty,
    GuardBandDecision,
    GUMInputError,
    SpecificationLimits,
    UncertaintyInput,
    combine,
    evaluate_guard_band,
)

__all__ = [
    "ChromophoreUncertainty",
    "LoudnessUncertainty",
    "SceneCutUncertainty",
    "ScientificVerificationBridge",
    "SLOBurnUncertainty",
    "chromophore_uncertainty",
    "loudness_uncertainty",
    "scene_cut_uncertainty",
    "slo_burn_uncertainty",
]

# BS.1770-4 reference offset: L_K = -0.691 + 10 log10(mean square).
_LUFS_OFFSET = -0.691
_DECIBEL_PER_NEPER = 10.0 / math.log(10.0)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GUMInputError(f"{label} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise GUMInputError(f"{label} must be positive, got {value}")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GUMInputError(f"{label} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GUMInputError(f"{label} must be finite, got {number!r}")
    return number


@dataclass(frozen=True, slots=True)
class LoudnessUncertainty:
    """Quantisation-limited uncertainty of one integrated loudness value."""

    lkfs: float
    mean_square: float
    quantisation_step: float
    standard: float
    combined: CombinedUncertainty

    def expanded(self, coverage_factor: float) -> float:
        return self.combined.expanded(coverage_factor)


def loudness_uncertainty(
    perception: AudioPerception, *, bit_depth: int = 16, sample_count: int = 48000
) -> LoudnessUncertainty:
    """Propagate uniform quantisation noise into an integrated loudness value.

    Model: samples are quantised with step ``q = 2 / 2**bit_depth`` (full scale
    ``+-1``), so each sample carries variance ``q^2 / 12``. The mean square of
    ``N`` independent samples therefore has ``u(ms) = sqrt(q^2 / (12 N))``, and
    ``L = -0.691 + 10 log10(ms)`` gives ``u(L) = 10/(ln 10) * u(ms)/ms``.

    Not modelled: K-weighting filter coefficient uncertainty, gating-window
    boundary effects and the finite block count. This is a *lower bound* on the
    real uncertainty of a loudness measurement.
    """
    if not isinstance(perception, AudioPerception):
        raise GUMInputError("perception must be an AudioPerception")
    depth = _positive_int(bit_depth, "bit_depth")
    samples = _positive_int(sample_count, "sample_count")
    lkfs = perception.integrated_lkfs
    if lkfs != -math.inf:
        lkfs = _finite_number(lkfs, "integrated_lkfs")
    mean_square = 10.0 ** ((lkfs - _LUFS_OFFSET) / 10.0)
    if not math.isfinite(mean_square) or mean_square <= 0.0:
        raise GUMInputError(f"integrated_lkfs {lkfs!r} does not map to a positive mean square")
    step = 2.0 / float(2**depth)
    sample_variance = step * step / 12.0
    mean_square_deviation = math.sqrt(sample_variance / samples)
    sensitivity = _DECIBEL_PER_NEPER / mean_square
    budget = UncertaintyInput.from_standard((mean_square,), (mean_square_deviation,))
    combined = combine((sensitivity,), budget)
    return LoudnessUncertainty(
        lkfs=lkfs,
        mean_square=mean_square,
        quantisation_step=step,
        standard=combined.standard,
        combined=combined,
    )


@dataclass(frozen=True, slots=True)
class SceneCutUncertainty:
    """Multinomial delta-method uncertainty of a Jensen-Shannon divergence."""

    js_divergence: float
    standard: float
    combined: CombinedUncertainty
    samples_per_histogram: int

    def expanded(self, coverage_factor: float) -> float:
        return self.combined.expanded(coverage_factor)


def _js_gradient(first: Sequence[float], second: Sequence[float]) -> tuple[float, ...]:
    """``d JS / d p_i`` in bits for ``JS(p, q)`` with ``m = (p + q)/2``.

    ``JS = 0.5 KL(p||m) + 0.5 KL(q||m)`` and the ``q`` term contributes
    ``-0.5 q_i / m_i`` to the derivative; the ``p`` term contributes
    ``0.5 (ln(p_i/m_i) + 1 - 0.5 p_i/m_i)``. Because ``(p_i + q_i)/m_i = 2`` the
    ``+1`` and the ``-0.5(p_i+q_i)/m_i`` cancel exactly, leaving ``0.5 ln(p_i/m_i)``.
    Bins with ``p_i <= 0`` carry zero variance, so their gradient is pinned to
    zero to keep the product finite.
    """
    gradient = []
    for p_value, q_value in zip(first, second, strict=False):
        midpoint = 0.5 * (p_value + q_value)
        if p_value <= 0.0 or midpoint <= 0.0:
            gradient.append(0.0)
        else:
            gradient.append(0.5 * math.log(p_value / midpoint) / math.log(2.0))
    return tuple(gradient)


def scene_cut_uncertainty(
    before: Sequence[float],
    after: Sequence[float],
    *,
    samples_per_histogram: int,
    reported_divergence: float | None = None,
) -> SceneCutUncertainty:
    """Propagate histogram sampling noise into a JS-divergence score.

    Model: each histogram is a multinomial draw of ``N`` pixels over the bins, so
    ``Cov(p) = (diag(p) - p p^T) / N``. The bins are therefore **correlated**,
    which is why this adapter exercises the covariance path of
    :func:`combine` instead of the diagonal one. ``reported_divergence`` is
    optional; when given it must agree with the recomputed divergence to within
    ``1e-9`` or the caller is looking at a different histogram than the engine.

    Not modelled: spatial correlation between neighbouring pixels, which makes
    ``N`` an optimistic sample count.
    """
    samples = _positive_int(samples_per_histogram, "samples_per_histogram")
    first = tuple(_finite_number(value, f"before[{index}]") for index, value in enumerate(before))
    second = tuple(_finite_number(value, f"after[{index}]") for index, value in enumerate(after))
    if len(first) != len(second):
        raise GUMInputError(
            f"histograms must share a bin count, got {len(first)} and {len(second)}"
        )
    if not first:
        raise GUMInputError("histograms must not be empty")
    for label, histogram in (("before", first), ("after", second)):
        total = math.fsum(histogram)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise GUMInputError(f"{label} histogram must be normalised, sums to {total!r}")
        if any(value < 0.0 for value in histogram):
            raise GUMInputError(f"{label} histogram entries must be non-negative")
    gradient = _js_gradient(first, second)
    size = len(first)
    covariance = tuple(
        tuple(
            ((first[row] if row == column else 0.0) - first[row] * first[column]) / samples
            for column in range(size)
        )
        for row in range(size)
    )
    budget = UncertaintyInput.from_covariance(first, covariance)
    combined = combine(gradient, budget)
    divergence = math.fsum(
        0.5 * p * math.log(p / (0.5 * (p + q))) / math.log(2.0)
        + 0.5 * q * math.log(q / (0.5 * (p + q))) / math.log(2.0)
        for p, q in zip(first, second, strict=False)
        if p > 0.0 and q > 0.0
    )
    if reported_divergence is not None:
        reported = _finite_number(reported_divergence, "reported_divergence")
        if abs(reported - divergence) > 1e-9:
            raise GUMInputError(
                f"reported divergence {reported!r} disagrees with the recomputed "
                f"{divergence!r}; the histograms are not the ones that were scored"
            )
    return SceneCutUncertainty(
        js_divergence=divergence,
        standard=combined.standard,
        combined=combined,
        samples_per_histogram=samples,
    )


@dataclass(frozen=True, slots=True)
class ChromophoreUncertainty:
    """Least-squares covariance of the two-chromophore inversion."""

    melanin: float
    hemoglobin: float
    covariance: tuple[tuple[float, float], tuple[float, float]]
    condition_number: float
    amplification: float
    combined: CombinedUncertainty

    def budget(self) -> UncertaintyInput:
        """The ``(melanin, hemoglobin)`` budget, ready for further propagation."""
        return UncertaintyInput.from_covariance((self.melanin, self.hemoglobin), self.covariance)


def _gramian_inverse(
    melanin_extinction: Sequence[float], hemoglobin_extinction: Sequence[float], ridge: float
) -> tuple[float, float, float]:
    """Inverse of the ridge-regularised ``2x2`` Gramian ``E E^T + ridge I``."""
    mel = tuple(
        _finite_number(value, f"melanin_extinction[{index}]")
        for index, value in enumerate(melanin_extinction)
    )
    hem = tuple(
        _finite_number(value, f"hemoglobin_extinction[{index}]")
        for index, value in enumerate(hemoglobin_extinction)
    )
    if len(mel) != len(hem):
        raise GUMInputError(
            f"extinction curves must share a band count, got {len(mel)} and {len(hem)}"
        )
    if not mel:
        raise GUMInputError("extinction curves must not be empty")
    regularisation = _finite_number(ridge, "ridge")
    if regularisation < 0.0:
        raise GUMInputError(f"ridge must be non-negative, got {regularisation!r}")
    gramian_mel = math.fsum(value * value for value in mel) + regularisation
    gramian_hem = math.fsum(value * value for value in hem) + regularisation
    gramian_cross = math.fsum(a * b for a, b in zip(mel, hem, strict=False))
    determinant = gramian_mel * gramian_hem - gramian_cross * gramian_cross
    if determinant <= 0.0:
        raise GUMInputError(
            "extinction curves are linearly dependent; the chromophore inversion is singular"
        )
    return (
        gramian_hem / determinant,
        gramian_mel / determinant,
        -gramian_cross / determinant,
    )


def chromophore_uncertainty(
    estimate: SkinChromophoreEstimate,
    *,
    reflectance_sigma: float,
    melanin_extinction: Sequence[float] | None = None,
    hemoglobin_extinction: Sequence[float] | None = None,
    ridge: float = 1e-9,
) -> ChromophoreUncertainty:
    """Covariance of the NNLS chromophore fit under reflectance noise.

    Model: with optical density ``OD = E c`` and independent per-band noise of
    standard deviation ``sigma``, the least-squares estimate has
    ``Cov(c) = sigma^2 (E^T E)^-1``. ``E^T E`` is the 2x2 Gramian of the two
    extinction curves; its inverse is computed in closed form. The condition
    number of the Gramian is reported because it *is* the sensitivity: two
    nearly parallel extinction curves amplify noise without bound.

    Not modelled: the active-set clamping itself. When ``estimate.clamped`` is
    true the covariance is a local linearisation at a boundary and is reported
    as-is; callers should treat a clamped estimate as ``ABSTAIN`` territory.
    """
    if not isinstance(estimate, SkinChromophoreEstimate):
        raise GUMInputError("estimate must be a SkinChromophoreEstimate")
    sigma = _finite_number(reflectance_sigma, "reflectance_sigma")
    if sigma < 0.0:
        raise GUMInputError(f"reflectance_sigma must be non-negative, got {sigma!r}")
    mel = MELANIN_EXTINCTION if melanin_extinction is None else melanin_extinction
    hem = HEMOGLOBIN_EXTINCTION if hemoglobin_extinction is None else hemoglobin_extinction
    inverse_mel, inverse_hem, inverse_cross = _gramian_inverse(mel, hem, ridge)
    variance_scale = sigma * sigma
    covariance = (
        (variance_scale * inverse_mel, variance_scale * inverse_cross),
        (variance_scale * inverse_cross, variance_scale * inverse_hem),
    )
    budget = UncertaintyInput.from_covariance((estimate.melanin, estimate.hemoglobin), covariance)
    combined = combine((1.0, 0.0), budget)
    amplification = math.sqrt(max(0.0, inverse_mel + inverse_hem))
    largest = max(inverse_mel, inverse_hem, abs(inverse_cross))
    smallest = min(inverse_mel, inverse_hem)
    condition = math.inf if smallest <= 0.0 else largest / smallest
    return ChromophoreUncertainty(
        melanin=estimate.melanin,
        hemoglobin=estimate.hemoglobin,
        covariance=covariance,
        condition_number=condition,
        amplification=amplification,
        combined=combined,
    )


@dataclass(frozen=True, slots=True)
class SLOBurnUncertainty:
    """Binomial uncertainty of an error-budget burn rate."""

    burn_rate: float
    error_rate: float
    standard: float
    combined: CombinedUncertainty
    observations: int

    def expanded(self, coverage_factor: float) -> float:
        return self.combined.expanded(coverage_factor)


def slo_burn_uncertainty(window: BurnWindowResult, *, target: float) -> SLOBurnUncertainty:
    """Propagate binomial sampling noise into an SLO burn rate.

    Model: ``failures`` out of ``total`` is a binomial draw, so
    ``u(p) = sqrt(p(1-p)/n)`` and ``burn = p / target`` has ``u(burn) = u(p)/target``.

    Not modelled: temporal correlation between requests (a single incident
    inflates many windows at once), which is why burn-rate alerts are still
    windowed rather than point-estimated.
    """
    if not isinstance(window, BurnWindowResult):
        raise GUMInputError("window must be a BurnWindowResult")
    objective = _finite_number(target, "target")
    if not 0.0 < objective < 1.0:
        raise GUMInputError(f"target must lie inside (0, 1), got {objective!r}")
    observations = _positive_int(window.total, "window.total")
    error_rate = window.failures / observations
    deviation = math.sqrt(error_rate * (1.0 - error_rate) / observations)
    budget = UncertaintyInput.from_standard((error_rate,), (deviation,))
    allowance = 1.0 - objective
    measured = error_rate / allowance
    tolerance = 1e-9 * max(1.0, abs(measured))
    if abs(window.burn_rate - measured) > tolerance:
        raise GUMInputError(
            f"window burn_rate {window.burn_rate!r} disagrees with the measured "
            f"{measured!r} for target {objective!r}; the window and the target do not match"
        )
    combined = combine((1.0 / allowance,), budget)
    return SLOBurnUncertainty(
        burn_rate=measured,
        error_rate=error_rate,
        standard=combined.standard,
        combined=combined,
        observations=observations,
    )


@dataclass(frozen=True, slots=True)
class ScientificVerificationBridge:
    """Turn a scientific measurement into a three-valued conformance verdict."""

    coverage_factor: float = 2.0

    def __post_init__(self) -> None:
        factor = _finite_number(self.coverage_factor, "coverage_factor")
        if factor <= 0.0:
            raise GUMInputError(f"coverage_factor must be positive, got {factor!r}")

    def check(
        self, value: float, combined: CombinedUncertainty, limits: SpecificationLimits
    ) -> GuardBandDecision:
        """Gate any propagated quantity against a specification region."""
        return evaluate_guard_band(value, combined, limits, self.coverage_factor)

    def check_loudness(
        self, perception: AudioPerception, limits: SpecificationLimits, **kwargs: int
    ) -> GuardBandDecision:
        budget = loudness_uncertainty(perception, **kwargs)
        return self.check(budget.lkfs, budget.combined, limits)

    def check_scene_cut(
        self,
        transition: FrameTransition,
        before: Sequence[float],
        after: Sequence[float],
        limits: SpecificationLimits,
        *,
        samples_per_histogram: int,
    ) -> GuardBandDecision:
        budget = scene_cut_uncertainty(
            before,
            after,
            samples_per_histogram=samples_per_histogram,
            reported_divergence=transition.js_divergence,
        )
        return self.check(budget.js_divergence, budget.combined, limits)

    def check_burn(
        self, window: BurnWindowResult, limits: SpecificationLimits, *, target: float
    ) -> GuardBandDecision:
        budget = slo_burn_uncertainty(window, target=target)
        return self.check(budget.burn_rate, budget.combined, limits)
