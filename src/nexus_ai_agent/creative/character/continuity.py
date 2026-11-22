"""Continuity verification: per-dimension, calibrated three-state verdicts.

This module is a deterministic threshold / evidence-state engine.  It compares
caller-supplied measured distances against calibrated thresholds and emits
per-dimension states — it is **not** a neural vision model and makes no
claim to measure pixel-level continuity against imagery.  "Verified" here
means "within the calibrated band", not "visually indistinguishable".

Each dimension produces one of three states (plus ``NOT_MEASURED`` for
dimensions with no source):

* ``VERIFIED`` — measured distance is within the calibrated verified band;
* ``REVIEW_REQUIRED`` — measured distance is inside the review band;
* ``IDENTITY_DRIFT`` — measured distance is at or above the drift threshold.

The aggregate verdict is the lowest-precedence state present: ``IDENTITY_DRIFT``
dominates, then ``REVIEW_REQUIRED``, then ``VERIFIED``, then ``NOT_MEASURED``,
then ``INSUFFICIENT_EVIDENCE``.

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
        if not isinstance(raw_distance, (int, float)):
            raise ValueError(f"distance for {name!r} must be a non-negative number")
        distance = float(raw_distance)
        if not math.isfinite(distance):
            raise ValueError(f"distance for {name!r} must be finite (no NaN or +/-inf)")
        if distance < 0.0:
            raise ValueError(f"distance for {name!r} must be non-negative")
        if distance >= threshold.drift_min:
            state = VerificationState.IDENTITY_DRIFT
        elif distance > threshold.verified_max:
            state = VerificationState.REVIEW_REQUIRED
        else:
            state = VerificationState.VERIFIED
        results[name] = DimensionResult(name, distance, state)

    states = {result.state for result in results.values()}
    # Defensive fallback (unreachable for a validated verify_dimensions call —
    # every dimension produces a state); kept so a direct ContinuityReport
    # construction with an unknown state cannot silently pass.
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
