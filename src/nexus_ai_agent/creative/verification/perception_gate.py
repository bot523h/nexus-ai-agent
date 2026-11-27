"""Admission gate: license supremum + GUM guard band before a perception edit.

The harvested perception pack turns *supplied* measurement evidence into
timeline markers and a loudness recommendation. Two things must be true before
such a command is worth dispatching:

* every contributing source's license must join to a compatible regime
  (:mod:`nexus_ai_agent.creative.composition.license_lattice`), and
* the loudness measurement must be precise enough to justify the gain it
  recommends -- otherwise the honest answer is to abstain, not to guess.

**Interface.** The pack may not import this module (the pack substrate depends on
``creative.packs``/``studio``/``temporal`` only, see ``MODULE_MAP.md`` R31), so the
public port is *plain data*: a mapping in, a mapping out.

``{"kind": "loudness"|"scene_cut"|"silence"|"license", ...}``
-> ``{"admitted", "outcome", "refusal", ...}``

:func:`gate_perception` is the single entry point
(:class:`~nexus_ai_agent.creative.packs.perception.operations.PerceptionGate`),
and :func:`admit_perception` is the typed wrapper for callers already holding
domain objects. Both run the same arithmetic, in the same order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.creative.composition.license_lattice import (
    LicenseCombination,
    LicenseRegime,
    combine_licenses,
)

from .adapters import scene_cut_uncertainty, silence_uncertainty
from .loudness import loudness_budget
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
    "gate_licenses",
    "gate_loudness",
    "gate_perception",
    "gate_scene_cut",
    "gate_silence",
    "parse_regimes",
]


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GUMInputError(f"{label} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GUMInputError(f"{label} must be finite, got {number!r}")
    return number


def _mapping(context: object, label: str) -> Mapping[str, Any]:
    if not isinstance(context, Mapping):
        raise GUMInputError(f"{label} must be a mapping, got {type(context).__name__}")
    return context


def _required(context: Mapping[str, Any], keys: Sequence[str]) -> None:
    missing = [key for key in keys if key not in context]
    if missing:
        raise GUMInputError(f"gate context is missing {sorted(missing)}")


def parse_regimes(names: object) -> tuple[LicenseRegime, ...]:
    """Map license regime *names* to lattice elements, refusing anything unknown."""
    if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
        raise GUMInputError(
            f"licenses must be a tuple of license names, got {type(names).__name__}"
        )
    regimes: list[LicenseRegime] = []
    for index, name in enumerate(names):
        if not isinstance(name, str):
            raise GUMInputError(f"licenses[{index}] must be a license name string")
        try:
            regimes.append(LicenseRegime(name))
        except ValueError:
            raise GUMInputError(
                f"unknown license regime {name!r}; expected one of "
                f"{[member.value for member in LicenseRegime]}"
            ) from None
    return tuple(regimes)


def _license_payload(combination: LicenseCombination) -> dict[str, Any]:
    return {
        "supremum": combination.supremum.value,
        "regimes": [regime.value for regime in combination.regimes],
        "commercial_intent": combination.commercial_intent,
        "compatible": combination.compatible,
    }


def gate_licenses(context: object) -> dict[str, Any]:
    """Join the licenses of every contributing source, and nothing else."""
    payload = _mapping(context, "gate context")
    _required(payload, ("licenses",))
    regimes = parse_regimes(payload["licenses"])
    if not regimes:
        raise GUMInputError("gate context must carry at least one license regime")
    combination = combine_licenses(
        regimes, commercial_intent=bool(payload.get("commercial_intent", False))
    )
    if not combination.compatible:
        return {
            "admitted": False,
            "outcome": "license_conflict",
            "refusal": (
                f"license supremum {combination.supremum.value} is not distributable "
                f"for regimes {[regime.value for regime in regimes]}"
            ),
            "license": _license_payload(combination),
        }
    return {
        "admitted": True,
        "outcome": "license_ok",
        "refusal": None,
        "license": _license_payload(combination),
    }


_LOUDNESS_KEYS = ("integrated_lufs", "target_lufs", "tolerance_lu")


def gate_loudness(context: object) -> dict[str, Any]:
    """License join **and** GUM guard band over one supplied loudness measurement.

    The measurement must already exist (the pack never runs an analyser); this
    function only decides whether it is admissible.
    """
    payload = _mapping(context, "gate context")
    _required(payload, _LOUDNESS_KEYS)
    # A caller that declares no contributing source makes no license claim; the
    # guard band still runs. Declaring a conflicting set refuses outright.
    regimes = parse_regimes(payload.get("licenses", ()))
    target = _finite(payload["target_lufs"], "target_lufs")
    tolerance = _finite(payload["tolerance_lu"], "tolerance_lu")
    if tolerance <= 0.0:
        raise GUMInputError(f"tolerance_lu must be positive, got {tolerance!r}")
    coverage = _finite(payload.get("coverage_factor", 2.0), "coverage_factor")
    if coverage <= 0.0:
        raise GUMInputError("coverage_factor must be positive")
    bit_depth = payload.get("bit_depth", 16)
    sample_count = payload.get("sample_count", 48_000)
    if isinstance(bit_depth, bool) or not isinstance(bit_depth, int):
        raise GUMInputError("bit_depth must be an int")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise GUMInputError("sample_count must be an int")

    combination = (
        combine_licenses(regimes, commercial_intent=bool(payload.get("commercial_intent", False)))
        if regimes
        else None
    )
    measured, mean_square, _step, budget = loudness_budget(
        _finite(payload["integrated_lufs"], "integrated_lufs"),
        bit_depth=bit_depth,
        sample_count=sample_count,
    )
    limits = SpecificationLimits(lower=target - tolerance, upper=target + tolerance)
    decision = evaluate_guard_band(measured, budget, limits, coverage)
    record: dict[str, Any] = {
        "outcome": decision.outcome.value,
        "admitted": False,
        "refusal": None,
        "recommended_gain_db": None,
        "integrated_lufs": measured,
        "mean_square": mean_square,
        "expanded_uncertainty": decision.expanded_uncertainty,
        "interval": [decision.interval_low, decision.interval_high],
        "limits": [limits.lower, limits.upper],
        "coverage_factor": coverage,
        "effective_dof": decision.effective_dof,
        "license": None if combination is None else _license_payload(combination),
    }
    if combination is not None and not combination.compatible:
        record["outcome"] = "license_conflict"
        record["refusal"] = (
            f"license supremum {combination.supremum.value} is not distributable "
            f"for regimes {[regime.value for regime in regimes]}"
        )
        return record
    if decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE:
        record["refusal"] = (
            f"loudness interval [{decision.interval_low:.3f}, "
            f"{decision.interval_high:.3f}] LUFS straddles the tolerance band; "
            "the measurement cannot justify a gain"
        )
        return record
    record["admitted"] = True
    record["recommended_gain_db"] = (
        0.0 if decision.outcome is VerificationOutcome.PASS else target - measured
    )
    return record


def _insufficient_evidence(reason: str, **extra: Any) -> dict[str, Any]:
    """A fail-closed abstention: no measurement means no gated claim."""
    record: dict[str, Any] = {
        "outcome": VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE.value,
        "admitted": False,
        "refusal": f"insufficient_evidence: {reason}",
        "recommended_gain_db": None,
    }
    record.update(extra)
    return record


def gate_scene_cut(context: object) -> dict[str, Any]:
    """GUM guard band over one candidate scene cut's *measured* divergence.

    The gate only accepts **empirical** frame histograms (``hist_before`` /
    ``hist_after``, normalised bin probabilities) with a positive sample count.
    The detector ``threshold`` is *not* a measurement and is never used to
    synthesise a histogram: a divergence built from the threshold would restate
    the claim. Without real histograms the honest answer is
    ``ABSTAIN_INSUFFICIENT_EVIDENCE``.
    """
    payload = _mapping(context, "gate context")
    cut_threshold = _finite(payload.get("cut_threshold_bits", 0.02), "cut_threshold_bits")
    if cut_threshold <= 0.0:
        raise GUMInputError(f"cut_threshold_bits must be positive, got {cut_threshold!r}")
    coverage = _finite(payload.get("coverage_factor", 2.0), "coverage_factor")
    if coverage <= 0.0:
        raise GUMInputError("coverage_factor must be positive")
    samples = payload.get("samples_per_histogram", 4096)
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise GUMInputError("samples_per_histogram must be an int")
    before = payload.get("hist_before")
    after = payload.get("hist_after")
    if before is None or after is None:
        return _insufficient_evidence(
            "missing_frame_histograms",
            limit=cut_threshold,
            coverage_factor=coverage,
        )
    if not isinstance(before, Sequence) or not isinstance(after, Sequence):
        raise GUMInputError("hist_before/hist_after must be sequences of bin probabilities")
    if len(before) != len(after) or not before:
        return _insufficient_evidence(
            "inconsistent_frame_histograms",
            limit=cut_threshold,
            coverage_factor=coverage,
        )
    budget = scene_cut_uncertainty(
        before,
        after,
        samples_per_histogram=samples,
        samples_per_histogram_after=payload.get("samples_per_histogram_after"),
    )
    decision = evaluate_guard_band(
        budget.js_divergence,
        budget.combined,
        SpecificationLimits(lower=cut_threshold),
        coverage,
    )
    admitted = decision.outcome is VerificationOutcome.PASS
    record: dict[str, Any] = {
        "outcome": decision.outcome.value,
        "admitted": admitted,
        "refusal": None,
        "recommended_gain_db": None,
        "js_divergence": budget.js_divergence,
        "combined_u": budget.combined.standard,
        "expanded_U": decision.expanded_uncertainty,
        "nu_eff": decision.effective_dof,
        "decision": decision.outcome.value,
        "interval": [decision.interval_low, decision.interval_high],
        "limit": cut_threshold,
        "samples_per_histogram": samples,
        "coverage_factor": coverage,
    }
    if not admitted:
        record["refusal"] = (
            f"scene-cut divergence {budget.js_divergence:.6f} bits does not clear the "
            f"{cut_threshold:.6f}-bit threshold outside the guard band "
            f"[{decision.interval_low:.6f}, {decision.interval_high:.6f}]"
        )
    return record


def gate_silence(context: object) -> dict[str, Any]:
    """GUM guard band over one candidate silence segment's **measured** RMS level.

    ``measured_rms_db`` is the empirical level of the signal; ``threshold_db`` is
    the decision threshold (normally the detector's ``noise_floor_db``). The two
    must never be the same value by construction: substituting the threshold for
    the measurement is a circular claim, so a missing measurement abstains.
    """
    payload = _mapping(context, "gate context")
    measured = payload.get("measured_rms_db")
    if measured is None:
        measured = payload.get("rms_db")
    if measured is None:
        return _insufficient_evidence("missing_measured_rms_db")
    samples = payload.get("sample_count")
    if samples is None:
        return _insufficient_evidence("missing_sample_count")
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise GUMInputError("sample_count must be an int")
    if samples <= 0:
        return _insufficient_evidence("non_positive_sample_count", sample_count=samples)
    _required(payload, ("threshold_db",))
    rms = _finite(measured, "measured_rms_db")
    threshold = _finite(payload["threshold_db"], "threshold_db")
    bit_depth = payload.get("bit_depth", 16)
    if isinstance(bit_depth, bool) or not isinstance(bit_depth, int):
        raise GUMInputError("bit_depth must be an int")
    probability = _finite(payload.get("coverage_probability", 0.95), "coverage_probability")
    budget = silence_uncertainty(
        rms,
        threshold,
        samples,
        bit_depth=bit_depth,
        window_duration_s=_finite(payload.get("window_duration_s", 0.0), "window_duration_s"),
        min_silence_duration_s=_finite(
            payload.get("min_silence_duration_s", 0.0), "min_silence_duration_s"
        ),
        coverage_probability=probability,
    )
    decision = budget.decision
    admitted = decision.outcome is VerificationOutcome.PASS
    record: dict[str, Any] = {
        "outcome": decision.outcome.value,
        "admitted": admitted,
        "refusal": None,
        "recommended_gain_db": None,
        "rms_db": budget.rms_db,
        "threshold_db": budget.threshold_db,
        "mean_square": budget.mean_square,
        "combined_u": budget.combined.standard,
        "expanded_U": decision.expanded_uncertainty,
        "nu_eff": decision.effective_dof,
        "decision": decision.outcome.value,
        "interval": [decision.interval_low, decision.interval_high],
        "coverage_probability": probability,
    }
    if not admitted:
        record["refusal"] = (
            f"silence RMS {budget.rms_db:.3f} dBFS is not below {budget.threshold_db:.3f} dBFS "
            f"outside the guard band [{decision.interval_low:.3f}, "
            f"{decision.interval_high:.3f}]"
        )
    return record


def gate_perception(context: object) -> dict[str, Any]:
    """The pack's single admission port: dispatch on ``context["kind"]``."""
    payload = _mapping(context, "gate context")
    kind = payload.get("kind", "loudness")
    if kind == "loudness":
        return gate_loudness(payload)
    if kind == "scene_cut":
        return gate_scene_cut(payload)
    if kind == "silence":
        return gate_silence(payload)
    if kind == "license":
        return gate_licenses(payload)
    raise GUMInputError(
        f"unknown gate kind {kind!r}; expected 'loudness', 'scene_cut', 'silence' or 'license'"
    )


@dataclass(frozen=True, slots=True)
class PerceptionAdmissionRequest:
    """Typed request for callers that already hold domain objects."""

    licenses: tuple[LicenseRegime, ...]
    commercial_intent: bool
    integrated_lkfs: float
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
        if isinstance(self.integrated_lkfs, bool) or not isinstance(
            self.integrated_lkfs, (int, float)
        ):
            raise GUMInputError("integrated_lkfs must be a real number")
        object.__setattr__(self, "licenses", regimes)
        object.__setattr__(
            self,
            "target_lkfs",
            _finite(self.target_lkfs, "target_lkfs"),
        )
        tolerance = _finite(self.tolerance_lu, "tolerance_lu")
        if tolerance <= 0.0:
            raise GUMInputError(f"tolerance_lu must be positive, got {tolerance!r}")
        object.__setattr__(self, "tolerance_lu", tolerance)
        if _finite(self.coverage_factor, "coverage_factor") <= 0.0:
            raise GUMInputError("coverage_factor must be positive")

    def as_gate_context(self) -> dict[str, Any]:
        return {
            "kind": "loudness",
            "licenses": [regime.value for regime in self.licenses],
            "commercial_intent": self.commercial_intent,
            "integrated_lufs": self.integrated_lkfs,
            "target_lufs": self.target_lkfs,
            "tolerance_lu": self.tolerance_lu,
            "coverage_factor": self.coverage_factor,
            "bit_depth": self.bit_depth,
            "sample_count": self.sample_count,
        }


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
    """Typed wrapper around :func:`gate_loudness` (same arithmetic, same order)."""
    if not isinstance(request, PerceptionAdmissionRequest):
        raise GUMInputError("request must be a PerceptionAdmissionRequest")
    record = gate_loudness(request.as_gate_context())
    combination = combine_licenses(request.licenses, commercial_intent=request.commercial_intent)
    measured, _mean_square, _step, budget = loudness_budget(
        request.integrated_lkfs,
        bit_depth=request.bit_depth,
        sample_count=request.sample_count,
    )
    limits = SpecificationLimits(
        lower=request.target_lkfs - request.tolerance_lu,
        upper=request.target_lkfs + request.tolerance_lu,
    )
    decision = evaluate_guard_band(measured, budget, limits, request.coverage_factor)
    return PerceptionAdmission(
        admitted=bool(record["admitted"]),
        license=combination,
        loudness=decision,
        recommended_gain_db=record["recommended_gain_db"],
        refusal=record["refusal"],
    )
