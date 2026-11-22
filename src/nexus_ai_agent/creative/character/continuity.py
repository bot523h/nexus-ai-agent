"""Continuity verification: per-dimension, calibrated three-state verdicts.

The verifier measures several dimensions independently and refuses to let one
stand in for another.  Each dimension produces one of three states:

* ``VERIFIED`` — measured distance is within the calibrated verified band;
* ``REVIEW_REQUIRED`` — measured distance is inside the review band;
* ``DRIFT`` — measured distance is at or above the drift threshold.

plus ``NOT_MEASURED`` for dimensions with no source.  The aggregate verdict is
the lowest-precedence state present: ``DRIFT`` dominates, then ``REVIEW_REQUIRED``,
then ``VERIFIED``, then ``NOT_MEASURED``.

A dimension with no measurement is reported as ``NOT_MEASURED``, never scored as zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VerificationState(str, Enum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    NOT_MEASURED = "NOT_MEASURED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


#: Precedence: first match wins. DRIFT dominates.
STATE_PRECEDENCE = (
    VerificationState.IDENTITY_DRIFT,
    VerificationState.REVIEW_REQUIRED,
    VerificationState.VERIFIED,
    VerificationState.NOT_MEASURED,
    VerificationState.INSUFFICIENT_EVIDENCE,
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

    def seal(self) -> str:
        """Domain-separated digest of the report's content."""
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
) -> ContinuityReport:
    """Verify a set of dimension measurements against calibrated thresholds.

    ``measurements`` maps dimension → distance (None = not measured).  Dimensions
    with no measurement are reported as NOT_MEASURED; dimensions with a
    measurement at or above ``drift_min`` are IDENTITY_DRIFT; between
    ``verified_max`` and ``drift_min`` are REVIEW_REQUIRED; at or below
    ``verified_max`` are VERIFIED.
    """
    if not measurements:
        raise ValueError("at least one dimension must be measured")
    results: dict[str, DimensionResult] = {}
    for name, raw_distance in measurements.items():
        if name not in thresholds:
            raise ValueError(f"no threshold configured for dimension {name!r}")
        threshold = thresholds[name]
        if raw_distance is None:
            results[name] = DimensionResult(name, None, VerificationState.NOT_MEASURED)
            continue
        if not isinstance(raw_distance, (int, float)) or not (0.0 <= float(raw_distance)):
            raise ValueError(f"distance for {name!r} must be a non-negative number")
        distance = float(raw_distance)
        if distance >= threshold.drift_min:
            state = VerificationState.IDENTITY_DRIFT
        elif distance > threshold.verified_max:
            state = VerificationState.REVIEW_REQUIRED
        else:
            state = VerificationState.VERIFIED
        results[name] = DimensionResult(name, distance, state)

    states = {result.state for result in results.values()}
    for state in STATE_PRECEDENCE:
        if state in states:
            verdict = state
            break
    else:
        verdict = VerificationState.INSUFFICIENT_EVIDENCE
    return ContinuityReport(verdict=verdict, dimensions=results, note=note)


__all__ = [
    "VerificationState",
    "STATE_PRECEDENCE",
    "ContinuityThreshold",
    "DimensionResult",
    "ContinuityReport",
    "verify_dimensions",
]
