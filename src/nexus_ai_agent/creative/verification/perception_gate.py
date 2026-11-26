"""Admission gate: license supremum + GUM guard band before a perception edit.

The harvested perception pack turns *supplied* measurement evidence into
timeline markers and a loudness recommendation. Two things must be true before
such a command is worth dispatching:

* every contributing source's license must join to a compatible regime
  (:mod:`nexus_ai_agent.creative.composition.license_lattice`), and
* the loudness measurement must be precise enough to justify the gain it
  recommends -- otherwise the honest answer is to abstain, not to guess.

This module composes those two independent refusals into one typed admission
record so a caller cannot accidentally apply the license check without the
uncertainty check, or vice versa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from nexus_ai_agent.creative.composition.license_lattice import (
    LicenseCombination,
    LicenseRegime,
    combine_licenses,
)
from nexus_ai_agent.creative.perception.analyzer import AudioPerception

from .adapters import loudness_uncertainty
from .uncertainty_gum import (
    GuardBandDecision,
    GUMInputError,
    SpecificationLimits,
    VerificationOutcome,
    evaluate_guard_band,
)

__all__ = [
    "PerceptionAdmission",
    "PerceptionAdmissionRequest",
    "admit_perception",
]


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GUMInputError(f"{label} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GUMInputError(f"{label} must be finite, got {number!r}")
    return number


@dataclass(frozen=True, slots=True)
class PerceptionAdmissionRequest:
    """What an admission decision needs: licenses, a measurement, a tolerance."""

    licenses: tuple[LicenseRegime, ...]
    commercial_intent: bool
    loudness: AudioPerception
    target_lkfs: float
    tolerance_lu: float
    coverage_factor: float = 2.0
    bit_depth: int = 16
    sample_count: int = 48_000

    def __post_init__(self) -> None:
        regimes = tuple(self.licenses)
        if not regimes:
            raise GUMInputError("at least one license regime is required")
        if any(not isinstance(regime, LicenseRegime) for regime in regimes):
            raise GUMInputError("licenses must contain LicenseRegime values")
        if not isinstance(self.commercial_intent, bool):
            raise GUMInputError("commercial_intent must be a bool")
        if not isinstance(self.loudness, AudioPerception):
            raise GUMInputError("loudness must be an AudioPerception")
        target = _finite(self.target_lkfs, "target_lkfs")
        tolerance = _finite(self.tolerance_lu, "tolerance_lu")
        if tolerance <= 0.0:
            raise GUMInputError(f"tolerance_lu must be positive, got {tolerance!r}")
        if _finite(self.coverage_factor, "coverage_factor") <= 0.0:
            raise GUMInputError("coverage_factor must be positive")
        object.__setattr__(self, "licenses", regimes)
        object.__setattr__(self, "target_lkfs", target)
        object.__setattr__(self, "tolerance_lu", tolerance)


@dataclass(frozen=True, slots=True)
class PerceptionAdmission:
    """A single, auditable admission record for one perception edit."""

    admitted: bool
    license: LicenseCombination
    loudness: GuardBandDecision
    recommended_gain_db: float | None
    refusal: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.license, LicenseCombination):
            raise GUMInputError("license must be a LicenseCombination")
        if not isinstance(self.loudness, GuardBandDecision):
            raise GUMInputError("loudness must be a GuardBandDecision")
        if self.admitted and self.refusal is not None:
            raise GUMInputError("an admitted request cannot carry a refusal reason")
        if not self.admitted and self.refusal is None:
            raise GUMInputError("a refused request must carry a refusal reason")
        if self.admitted and self.recommended_gain_db is None:
            raise GUMInputError("an admitted request must recommend a gain")
        if self.recommended_gain_db is not None:
            object.__setattr__(
                self, "recommended_gain_db", _finite(self.recommended_gain_db, "gain")
            )


def admit_perception(request: PerceptionAdmissionRequest) -> PerceptionAdmission:
    """Join the licenses, then gate the gain recommendation on the guard band.

    A licence conflict refuses outright. Otherwise the loudness interval decides:

    * fully inside the tolerance band -> admitted, gain ``0.0`` (no correction);
    * fully outside the band -> admitted, gain ``target - measured``;
    * straddling the band -> refused, because the measurement cannot say which
      side of the tolerance it is on and a guess would be indistinguishable from
      a measurement.
    """
    if not isinstance(request, PerceptionAdmissionRequest):
        raise GUMInputError("request must be a PerceptionAdmissionRequest")
    combination = combine_licenses(request.licenses, commercial_intent=request.commercial_intent)
    budget = loudness_uncertainty(
        request.loudness, bit_depth=request.bit_depth, sample_count=request.sample_count
    )
    limits = SpecificationLimits(
        lower=request.target_lkfs - request.tolerance_lu,
        upper=request.target_lkfs + request.tolerance_lu,
    )
    decision = evaluate_guard_band(budget.lkfs, budget.combined, limits, request.coverage_factor)
    if not combination.compatible:
        return PerceptionAdmission(
            admitted=False,
            license=combination,
            loudness=decision,
            recommended_gain_db=None,
            refusal=f"license supremum {combination.supremum.value} is not distributable",
        )
    if decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE:
        return PerceptionAdmission(
            admitted=False,
            license=combination,
            loudness=decision,
            recommended_gain_db=None,
            refusal=(
                f"loudness interval [{decision.interval_low:.3f}, "
                f"{decision.interval_high:.3f}] LUFS straddles the tolerance band; "
                "the measurement cannot justify a gain"
            ),
        )
    gain = (
        0.0 if decision.outcome is VerificationOutcome.PASS else request.target_lkfs - budget.lkfs
    )
    return PerceptionAdmission(
        admitted=True,
        license=combination,
        loudness=decision,
        recommended_gain_db=gain,
        refusal=None,
    )
