"""JCGM 100:2008 (GUM) first-order uncertainty propagation and guard-band gating.

Implements three things and refuses to pretend about a fourth:

1. the law of propagation of uncertainty to first order,
   ``u_c^2(y) = g^T Sigma_x g``, over independent *or* correlated inputs;
2. the Welch-Satterthwaite effective degrees of freedom;
3. a three-valued conformance decision -- ``PASS``/``FAIL`` only when the whole
   expanded interval is inside/outside the specification, otherwise
   ``ABSTAIN_INSUFFICIENT_EVIDENCE``.

It does **not** implement Monte Carlo propagation (JCGM 101) or second-order
terms (JCGM 102). First-order propagation is only as honest as the
linearisation, so every result carries ``linearisation_penalty``: the ratio of
the covariance contribution to the diagonal contribution. A value far from 1.0
means the cross terms dominate and the first-order model deserves suspicion.

Every float is validated with :func:`math.isfinite` and ``bool`` is rejected
explicitly, because ``True`` is an ``int`` in Python and would otherwise slip
through a numeric guard.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CombinedUncertainty",
    "GUMCovarianceError",
    "GUMError",
    "GUMInputError",
    "GuardBandDecision",
    "SpecificationLimits",
    "UncertaintyInput",
    "VerificationOutcome",
    "combine",
    "evaluate_guard_band",
    "evaluate_threshold",
    "welch_satterthwaite",
]

# Symmetry and Cauchy-Schwarz checks are floating point, so they compare against
# a relative tolerance scaled by the magnitudes involved rather than exact zero.
_RELATIVE_TOLERANCE = 1e-12

# A covariance matrix is positive semi-definite up to this relative spectral
# tolerance; below it, a negative eigenvalue is a real defect, not round-off.
_SPECTRAL_TOLERANCE = 1e-9

# Jacobi sweeps needed for a symmetric matrix of this size; the rotation is
# quadratically convergent, so a handful of sweeps reach machine precision.
_MAX_JACOBI_SWEEPS = 60


class GUMError(ValueError):
    """Base class for every uncertainty-domain failure."""


class GUMInputError(GUMError):
    """A value, sensitivity or degree of freedom is not a usable real number."""


class GUMCovarianceError(GUMError):
    """The covariance matrix is not symmetric positive semi-definite."""


class VerificationOutcome(str, Enum):
    """Three-valued conformance verdict; ``ABSTAIN`` is never a silent pass."""

    PASS = "pass"
    FAIL = "fail"
    ABSTAIN_INSUFFICIENT_EVIDENCE = "abstain_insufficient_evidence"


def _finite(value: object, label: str) -> float:
    """Coerce one real number, rejecting ``bool``, ``NaN`` and infinities."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GUMInputError(f"{label} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GUMInputError(f"{label} must be finite, got {number!r}")
    return number


def _finite_or_infinite(value: object, label: str) -> float:
    """Coerce one real number, allowing ``+inf`` for an exactly known input."""
    if isinstance(value, float) and value == math.inf:
        return math.inf
    return _finite(value, label)


def _tolerance(*magnitudes: float) -> float:
    return _RELATIVE_TOLERANCE * max(1.0, *(abs(value) for value in magnitudes))


def _gershgorin_floor(matrix: tuple[tuple[float, ...], ...]) -> float:
    """Lower bound on ``lambda_min`` for a symmetric matrix.

    A non-negative floor *proves* positive semi-definiteness, so the exact
    spectrum is only computed for the matrices where the bound is inconclusive.
    """
    floor = math.inf
    for index, row in enumerate(matrix):
        radius = math.fsum(abs(entry) for position, entry in enumerate(row) if position != index)
        floor = min(floor, row[index] - radius)
    return floor


def _symmetric_eigenvalues(matrix: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
    """All eigenvalues of a small symmetric matrix by cyclic Jacobi rotations.

    Deterministic (no library, no random start, no sign ambiguity) and exact to
    the last few ulps, which is what a ``lambda_min < 0`` refusal needs.
    """
    size = len(matrix)
    work = [list(row) for row in matrix]
    for _ in range(_MAX_JACOBI_SWEEPS):
        off_diagonal = math.fsum(
            work[row][column] ** 2 for row in range(size) for column in range(size) if row != column
        )
        if off_diagonal <= (1e-30 * max(1.0, abs(work[0][0])) ** 2):
            break
        for pivot in range(size - 1):
            for other in range(pivot + 1, size):
                entry = work[pivot][other]
                if entry == 0.0:
                    continue
                theta = (work[other][other] - work[pivot][pivot]) / (2.0 * entry)
                if theta == 0.0:
                    tangent = 1.0
                else:
                    tangent = math.copysign(1.0, theta) / (
                        abs(theta) + math.sqrt(theta * theta + 1.0)
                    )
                cosine = 1.0 / math.sqrt(tangent * tangent + 1.0)
                sine = tangent * cosine
                for index in range(size):
                    left = work[index][pivot]
                    right = work[index][other]
                    work[index][pivot] = cosine * left - sine * right
                    work[index][other] = sine * left + cosine * right
                for index in range(size):
                    top = work[pivot][index]
                    bottom = work[other][index]
                    work[pivot][index] = cosine * top - sine * bottom
                    work[other][index] = sine * top + cosine * bottom
    return tuple(sorted(work[index][index] for index in range(size)))


@dataclass(frozen=True, slots=True)
class UncertaintyInput:
    """Input estimates ``x`` with their covariance matrix ``Sigma_x``.

    ``standard_uncertainties`` is always derived from the diagonal, so there is
    exactly one source of truth for ``u(x_i)``.
    """

    values: tuple[float, ...]
    standard_uncertainties: tuple[float, ...]
    covariance: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        values = tuple(
            _finite(value, f"values[{index}]") for index, value in enumerate(self.values)
        )
        covariance_rows = tuple(tuple(row) for row in self.covariance)
        size = len(values)
        if size == 0:
            raise GUMInputError("at least one input estimate is required")
        if len(covariance_rows) != size:
            raise GUMInputError(
                f"covariance has {len(covariance_rows)} rows for {size} input estimates"
            )
        for index, row in enumerate(covariance_rows):
            if len(row) != size:
                raise GUMInputError(
                    f"covariance row {index} has {len(row)} entries for {size} input estimates"
                )
        matrix = tuple(
            tuple(
                _finite(entry, f"covariance[{row}][{column}]") for column, entry in enumerate(line)
            )
            for row, line in enumerate(covariance_rows)
        )
        for first in range(size):
            for second in range(first + 1, size):
                left = matrix[first][second]
                right = matrix[second][first]
                if abs(left - right) > _tolerance(left, right):
                    raise GUMCovarianceError(
                        f"covariance must be symmetric: [{first}][{second}]={left!r} "
                        f"but [{second}][{first}]={right!r}"
                    )
        variances = tuple(matrix[index][index] for index in range(size))
        for index, variance in enumerate(variances):
            if variance < 0.0:
                raise GUMCovarianceError(
                    f"variance of input {index} must be non-negative, got {variance!r}"
                )
        deviations = tuple(math.sqrt(variance) for variance in variances)
        for first in range(size):
            for second in range(first + 1, size):
                bound = deviations[first] * deviations[second]
                if abs(matrix[first][second]) > bound + _tolerance(bound):
                    raise GUMCovarianceError(
                        f"covariance[{first}][{second}]={matrix[first][second]!r} violates the "
                        f"Cauchy-Schwarz bound {bound!r}"
                    )
        if size >= 3 and _gershgorin_floor(matrix) < 0.0:
            spectrum = _symmetric_eigenvalues(matrix)
            lambda_min = spectrum[0]
            spectral_tolerance = _SPECTRAL_TOLERANCE * max(1.0, abs(spectrum[-1]))
            if lambda_min < -spectral_tolerance:
                raise GUMCovarianceError(
                    f"covariance is not positive semi-definite: lambda_min={lambda_min!r} "
                    "(pairwise Cauchy-Schwarz is necessary but not sufficient)"
                )
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "standard_uncertainties", deviations)
        object.__setattr__(self, "covariance", matrix)

    @classmethod
    def from_standard(
        cls, values: Sequence[float], standard_uncertainties: Sequence[float]
    ) -> UncertaintyInput:
        """Build an independent-input budget from ``u(x_i)``."""
        deviations = tuple(
            _finite(value, f"standard_uncertainties[{index}]")
            for index, value in enumerate(standard_uncertainties)
        )
        for index, deviation in enumerate(deviations):
            if deviation < 0.0:
                raise GUMInputError(
                    f"standard_uncertainties[{index}] must be non-negative, got {deviation!r}"
                )
        matrix = tuple(
            tuple(
                deviations[row] ** 2 if row == column else 0.0 for column in range(len(deviations))
            )
            for row in range(len(deviations))
        )
        return cls(values=tuple(values), standard_uncertainties=deviations, covariance=matrix)

    @classmethod
    def from_covariance(
        cls, values: Sequence[float], covariance: Sequence[Sequence[float]]
    ) -> UncertaintyInput:
        """Build a correlated-input budget; the diagonal is authoritative for ``u(x_i)``."""
        # __post_init__ validates the shape and then derives the deviations, so no
        # row is indexed before its width has been checked.
        return cls(
            values=tuple(values),
            standard_uncertainties=(),
            covariance=tuple(tuple(entry for entry in line) for line in covariance),
        )


@dataclass(frozen=True, slots=True)
class CombinedUncertainty:
    """The first-order budget of ``y = f(x)`` around one operating point."""

    sensitivity: tuple[float, ...]
    variance: float
    standard: float
    effective_dof: float
    diagonal_terms: tuple[float, ...]
    covariance_terms: tuple[float, ...]
    linearisation_penalty: float

    def expanded(self, coverage_factor: float) -> float:
        """Expanded uncertainty ``U = k u_c(y)`` for ``k > 0``."""
        factor = _finite(coverage_factor, "coverage_factor")
        if factor <= 0.0:
            raise GUMInputError(f"coverage_factor must be positive, got {factor!r}")
        return factor * self.standard

    @property
    def has_correlation(self) -> bool:
        return any(term != 0.0 for term in self.covariance_terms)


def _sensitivity(jacobian: Sequence[float]) -> tuple[float, ...]:
    return tuple(_finite(value, f"jacobian[{index}]") for index, value in enumerate(jacobian))


def combine(
    jacobian: Sequence[float],
    uncertainty: UncertaintyInput,
    degrees_of_freedom: Sequence[float] | None = None,
) -> CombinedUncertainty:
    """Propagate ``u_c^2(y) = g^T Sigma_x g`` with compensated summation.

    ``degrees_of_freedom`` is the ``nu_i`` of Welch-Satterthwaite; ``None`` means
    every input is treated as exactly known (``nu_i = inf``), which yields
    ``nu_eff = inf``. Correlated terms are *not* part of the classical
    Welch-Satterthwaite denominator (JCGM 100:2008 G.4.2 assumes independent
    inputs), so a correlated budget reports the classical value and says so via
    :attr:`CombinedUncertainty.has_correlation`.
    """
    gradient = _sensitivity(jacobian)
    if len(gradient) != len(uncertainty.values):
        raise GUMInputError(
            f"jacobian has {len(gradient)} entries for {len(uncertainty.values)} inputs"
        )
    diagonal = tuple(
        (gradient[index] * uncertainty.standard_uncertainties[index]) ** 2
        for index in range(len(gradient))
    )
    cross: list[float] = []
    for row in range(len(gradient)):
        for column in range(row + 1, len(gradient)):
            cross.append(
                2.0 * gradient[row] * gradient[column] * uncertainty.covariance[row][column]
            )
    diagonal_sum = math.fsum(diagonal)
    cross_sum = math.fsum(cross)
    variance = math.fsum((diagonal_sum, cross_sum))
    if variance < 0.0:
        # ``UncertaintyInput`` already proved ``Sigma_x`` is positive semi-definite,
        # so ``g^T Sigma g >= 0`` exactly; a negative here is cancellation noise.
        variance = 0.0
    standard = math.sqrt(variance)
    effective_dof = welch_satterthwaite(diagonal, degrees_of_freedom)
    total = abs(diagonal_sum) + abs(cross_sum)
    penalty = 1.0 if total == 0.0 else abs(cross_sum) / total
    return CombinedUncertainty(
        sensitivity=gradient,
        variance=variance,
        standard=standard,
        effective_dof=effective_dof,
        diagonal_terms=diagonal,
        covariance_terms=tuple(cross),
        linearisation_penalty=penalty,
    )


def welch_satterthwaite(
    contributions: Sequence[float], degrees_of_freedom: Sequence[float] | None = None
) -> float:
    """Effective degrees of freedom for independent contributions.

    ``contributions`` are the ``(g_i u(x_i))^2`` terms. With no degrees of
    freedom every input is exact, so the result is ``inf``. The GUM caps
    ``nu_eff`` at ``sum(nu_i)``; that cap is applied here and documented because
    an uncapped value can exceed every input's freedom.
    """
    terms = tuple(
        _finite(value, f"contributions[{index}]") for index, value in enumerate(contributions)
    )
    for index, term in enumerate(terms):
        if term < 0.0:
            raise GUMInputError(f"contributions[{index}] must be non-negative, got {term!r}")
    if degrees_of_freedom is None:
        return math.inf
    freedoms = tuple(
        _finite_or_infinite(value, f"degrees_of_freedom[{index}]")
        for index, value in enumerate(degrees_of_freedom)
    )
    if len(freedoms) != len(terms):
        raise GUMInputError(f"{len(freedoms)} degrees of freedom for {len(terms)} contributions")
    for index, freedom in enumerate(freedoms):
        if freedom <= 0.0:
            raise GUMInputError(f"degrees_of_freedom[{index}] must be positive, got {freedom!r}")
    denominator = math.fsum(
        term * term / freedom
        for term, freedom in zip(terms, freedoms, strict=False)
        if not math.isinf(freedom)
    )
    if denominator == 0.0:
        return math.inf
    combined_variance = math.fsum(terms)
    effective = combined_variance * combined_variance / denominator
    # GUM G.4.1: nu_eff may not exceed the sum of the input freedoms. With every
    # input exactly known the cap is +inf, which is what the numerator implies.
    cap = math.fsum(freedom for freedom in freedoms if not math.isinf(freedom)) or math.inf
    return min(effective, cap)


@dataclass(frozen=True, slots=True)
class SpecificationLimits:
    """A one- or two-sided conformance region, at least one limit required."""

    lower: float | None = None
    upper: float | None = None

    def __post_init__(self) -> None:
        if self.lower is None and self.upper is None:
            raise GUMInputError("at least one specification limit is required")
        lower = None if self.lower is None else _finite(self.lower, "lower")
        upper = None if self.upper is None else _finite(self.upper, "upper")
        if lower is not None and upper is not None and not lower < upper:
            raise GUMInputError(f"lower {lower!r} must be strictly below upper {upper!r}")
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)


@dataclass(frozen=True, slots=True)
class GuardBandDecision:
    """A three-valued verdict plus the interval it was derived from."""

    outcome: VerificationOutcome
    value: float
    expanded_uncertainty: float
    interval_low: float
    interval_high: float
    limits: SpecificationLimits
    coverage_factor: float
    effective_dof: float

    @property
    def is_conclusive(self) -> bool:
        return self.outcome is not VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE


def evaluate_guard_band(
    value: float,
    combined: CombinedUncertainty,
    limits: SpecificationLimits,
    coverage_factor: float,
) -> GuardBandDecision:
    """Decide ``PASS``/``FAIL``/``ABSTAIN`` from the expanded interval.

    The decision is deliberately conservative: an interval that merely touches a
    limit abstains, because touching is the boundary case the guard band exists
    to refuse. Only a strict containment passes and only a strict separation
    fails.
    """
    centre = _finite(value, "value")
    if not isinstance(combined, CombinedUncertainty):
        raise GUMInputError("combined must be a CombinedUncertainty")
    if not isinstance(limits, SpecificationLimits):
        raise GUMInputError("limits must be SpecificationLimits")
    expanded = combined.expanded(coverage_factor)
    low = centre - expanded
    high = centre + expanded
    inside = (limits.lower is None or low > limits.lower) and (
        limits.upper is None or high < limits.upper
    )
    outside = (limits.lower is not None and high < limits.lower) or (
        limits.upper is not None and low > limits.upper
    )
    if inside:
        outcome = VerificationOutcome.PASS
    elif outside:
        outcome = VerificationOutcome.FAIL
    else:
        outcome = VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    return GuardBandDecision(
        outcome=outcome,
        value=centre,
        expanded_uncertainty=expanded,
        interval_low=low,
        interval_high=high,
        limits=limits,
        coverage_factor=_finite(coverage_factor, "coverage_factor"),
        effective_dof=combined.effective_dof,
    )


def evaluate_threshold(
    value: float,
    standard_uncertainty: float,
    threshold: float,
    coverage_k: float,
    direction: str,
) -> GuardBandDecision:
    """One-sided convenience wrapper: ``direction`` is ``"max"`` or ``"min"``.

    ``"max"`` means the measured quantity must stay at or below ``threshold``;
    ``"min"`` means it must stay at or above it.
    """
    deviation = _finite(standard_uncertainty, "standard_uncertainty")
    if deviation < 0.0:
        raise GUMInputError(f"standard_uncertainty must be non-negative, got {deviation!r}")
    limit = _finite(threshold, "threshold")
    if direction == "max":
        limits = SpecificationLimits(upper=limit)
    elif direction == "min":
        limits = SpecificationLimits(lower=limit)
    else:
        raise GUMInputError(f"direction must be 'max' or 'min', got {direction!r}")
    combined = CombinedUncertainty(
        sensitivity=(1.0,),
        variance=deviation * deviation,
        standard=deviation,
        effective_dof=math.inf,
        diagonal_terms=(deviation * deviation,),
        covariance_terms=(),
        linearisation_penalty=0.0,
    )
    return evaluate_guard_band(value, combined, limits, coverage_k)
