"""Continuity verification: per-dimension, calibrated three-state verdicts.

This module is a deterministic threshold / evidence-state engine.  It compares
caller-supplied measured distances against calibrated thresholds and emits
per-dimension states — it is **not** a neural vision model and makes no
claim to measure pixel-level continuity against imagery.  "Verified" here
means "within the calibrated band", not "visually indistinguishable".

Each dimension produces one of three states (plus ``NOT_MEASURED`` for
dimensions with no source, and ``INSUFFICIENT_EVIDENCE`` for measured
dimensions whose sample count is below the caller's floor):

* ``VERIFIED`` — measured distance is within the calibrated verified band;
* ``REVIEW_REQUIRED`` — measured distance is inside the review band;
* ``IDENTITY_DRIFT`` — measured distance is at or above the drift threshold;
* ``NOT_MEASURED`` — no measurement was supplied for the dimension;
* ``INSUFFICIENT_EVIDENCE`` — a distance was supplied, but it rests on fewer
  than ``min_evidence_samples`` samples, so it is not scored at all: thin
  evidence may neither pass (``VERIFIED``) nor accuse (``IDENTITY_DRIFT``).

The aggregate verdict is the lowest-precedence state present: ``IDENTITY_DRIFT``
dominates, then ``REVIEW_REQUIRED``, then ``INSUFFICIENT_EVIDENCE``, then
``NOT_MEASURED``, then ``VERIFIED``.  ``VERIFIED`` is fail-closed: it is only
emitted when every measured dimension is ``VERIFIED``; any unmeasured or
insufficient-evidence dimension outranks it.

A dimension with no measurement is reported as ``NOT_MEASURED``, never scored as zero.

``ContinuityReport.seal()`` produces a domain-separated SHA-256 **integrity
digest** of the report.  It is a tamper-evidence / reproducibility check —
it is **not** a digital signature (no key, no key-holder, no authenticity claim).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class VerificationState(str, Enum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    NOT_MEASURED = "NOT_MEASURED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


#: Precedence: first match wins. DRIFT dominates; VERIFIED is last so the
#: aggregate verdict can only be VERIFIED when every dimension is VERIFIED.
STATE_PRECEDENCE = (
    VerificationState.IDENTITY_DRIFT,
    VerificationState.REVIEW_REQUIRED,
    VerificationState.INSUFFICIENT_EVIDENCE,
    VerificationState.NOT_MEASURED,
    VerificationState.VERIFIED,
)


@dataclass(frozen=True, slots=True)
class ContinuityThreshold:
    """Calibrated three-band thresholds for one dimension."""

    dimension: str
    verified_max: float
    drift_min: float

    def __post_init__(self) -> None:
        if not self.dimension:
            raise ValueError("dimension must be non-empty")
        if not isinstance(self.verified_max, (int, float)) or not isinstance(
            self.drift_min, (int, float)
        ):
            raise ValueError("thresholds must be numeric")
        if not (math.isfinite(self.verified_max) and math.isfinite(self.drift_min)):
            raise ValueError("thresholds must be finite (no NaN or +/-inf)")
        if not (0.0 <= self.verified_max < self.drift_min):
            raise ValueError("require 0 <= verified_max < drift_min")


@dataclass(frozen=True, slots=True)
class DimensionResult:
    dimension: str
    distance: float | None
    state: VerificationState


@dataclass(frozen=True, slots=True)
class ContinuityReport:
    verdict: VerificationState
    dimensions: dict[str, DimensionResult]
    note: str = ""

    def __post_init__(self) -> None:
        # Deep-freeze: defensive copy -> MappingProxyType.  Any mutation of the
        # caller-held dict (or a mutation of the frozen mapping itself) raises.
        copied: dict[str, DimensionResult] = {}
        for name, result in self.dimensions.items():
            if not isinstance(result, DimensionResult):
                raise ValueError(f"dimensions[{name!r}] must be a DimensionResult")
            copied[name] = DimensionResult(result.dimension, result.distance, result.state)
        object.__setattr__(self, "dimensions", MappingProxyType(copied))

    def seal(self) -> str:
        """Domain-separated integrity digest of the report's content.

        This is an integrity/reproducibility check (SHA-256 over a canonical
        JSON serialization), **not** a digital signature: it proves the
        report's bytes have not changed since sealing, not who authored it.
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(b"continuity/1|" + canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "character-continuity/1",
            "verdict": self.verdict.value,
            "dimensions": {
                name: {"distance": result.distance, "state": result.state.value}
                for name, result in sorted(self.dimensions.items())
            },
            "note": self.note,
        }


def verify_dimensions(
    measurements: Mapping[str, float | None],
    thresholds: Mapping[str, ContinuityThreshold],
    *,
    note: str = "",
    evidence_counts: Mapping[str, int] | None = None,
    min_evidence_samples: int = 1,
) -> ContinuityReport:
    """Verify a set of dimension measurements against calibrated thresholds.

    ``measurements`` maps dimension → distance (None = not measured).  Dimensions
    with no measurement are reported as NOT_MEASURED; dimensions with a
    measurement at or above ``drift_min`` are IDENTITY_DRIFT; between
    ``verified_max`` and ``drift_min`` are REVIEW_REQUIRED; at or below
    ``verified_max`` are VERIFIED.

    ``evidence_counts`` maps dimension → the number of independent samples the
    distance rests on (``min_evidence_samples`` is the floor, default 1).  A
    measured dimension whose count is below the floor is reported as
    ``INSUFFICIENT_EVIDENCE`` and is **not** scored: a distance computed from
    too few samples must not be allowed to pass as VERIFIED, nor to accuse a
    character of IDENTITY_DRIFT.  Dimensions absent from ``evidence_counts``
    are scored exactly as before, so the parameter is backwards compatible.
    """
    if not measurements:
        raise ValueError("at least one dimension must be measured")
    if isinstance(min_evidence_samples, bool) or not isinstance(min_evidence_samples, int):
        raise ValueError("min_evidence_samples must be an int")
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    evidence: dict[str, int] = {}
    for key, count in (evidence_counts or {}).items():
        if key not in measurements:
            raise ValueError(f"evidence_counts[{key!r}] has no matching measurement")
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError(f"evidence_counts[{key!r}] must be an int sample count")
        if count < 0:
            raise ValueError(f"evidence_counts[{key!r}] must be non-negative")
        evidence[key] = count
    # Every (key, threshold) pair must be internally consistent: a threshold
    # stored under one key must be calibrated for that same key.  A mismatch is
    # a caller bug, not a valid calibration, so the verdict would silently
    # misdirect the drift/review boundary for that dimension — refuse it.
    for key, threshold in thresholds.items():
        if not isinstance(threshold, ContinuityThreshold):
            raise ValueError(f"thresholds[{key!r}] must be a ContinuityThreshold")
        if threshold.dimension != key:
            raise ValueError(
                f"thresholds[{key!r}] is calibrated for {threshold.dimension!r} "
                f"but stored under key {key!r}"
            )
    results: dict[str, DimensionResult] = {}
    for name, raw_distance in measurements.items():
        if name not in thresholds:
            raise ValueError(f"no threshold configured for dimension {name!r}")
        threshold = thresholds[name]
        if raw_distance is None:
            results[name] = DimensionResult(name, None, VerificationState.NOT_MEASURED)
            continue
        if not isinstance(raw_distance, (int, float)):
            raise ValueError(f"distance for {name!r} must be a non-negative number")
        distance = float(raw_distance)
        if not math.isfinite(distance):
            raise ValueError(f"distance for {name!r} must be finite (no NaN or +/-inf)")
        if distance < 0.0:
            raise ValueError(f"distance for {name!r} must be non-negative")
        if evidence.get(name, min_evidence_samples) < min_evidence_samples:
            # Measured, but on a thinner sample base than the caller's floor:
            # the distance is real but not decidable, so it is reported as
            # INSUFFICIENT_EVIDENCE rather than scored into a band.
            results[name] = DimensionResult(name, distance, VerificationState.INSUFFICIENT_EVIDENCE)
            continue
        if distance >= threshold.drift_min:
            state = VerificationState.IDENTITY_DRIFT
        elif distance > threshold.verified_max:
            state = VerificationState.REVIEW_REQUIRED
        else:
            state = VerificationState.VERIFIED
        results[name] = DimensionResult(name, distance, state)

    states = {result.state for result in results.values()}
    # Precedence selection over the per-dimension states.  Every branch is
    # reachable: INSUFFICIENT_EVIDENCE comes from a measured dimension below
    # the caller's evidence floor, the other four from the banded scoring.  The
    # loop keeps VERIFIED last, so the aggregate is never VERIFIED unless every
    # single dimension is VERIFIED (fail-closed).
    verdict = VerificationState.INSUFFICIENT_EVIDENCE
    for state in STATE_PRECEDENCE:
        if state in states:
            verdict = state
            break
    return ContinuityReport(verdict=verdict, dimensions=results, note=note)


__all__ = [
    "VerificationState",
    "STATE_PRECEDENCE",
    "ContinuityThreshold",
    "DimensionResult",
    "ContinuityReport",
    "verify_dimensions",
]
