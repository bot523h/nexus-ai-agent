"""Canonical Temporal Truth Primitive Package for Nexus Creative OS.

Surface:
* :class:`Timebase` — Exact rational frame/sample/clock rate.
* :class:`RoundingPolicy` — Explicit rounding policies during timebase conversions.
* :class:`ConversionOutcome` — Loss-aware result container tracking exact remainders.
* :class:`TemporalPoint` — Exact rational time point/duration.
* :class:`TemporalInterval` — Half-open interval [start, end) with split/join algebra.
* :class:`TemporalTransform` — Exact retiming and speed scaling transformation.
* :class:`ClockRelation` — Cross-clock mapping between video frames and audio samples.
"""

from nexus_ai_agent.creative.temporal.core import (
    ClockRelation,
    ConversionOutcome,
    RoundingPolicy,
    TemporalInterval,
    TemporalPoint,
    TemporalTransform,
    Timebase,
)

__all__ = [
    "ClockRelation",
    "ConversionOutcome",
    "RoundingPolicy",
    "TemporalInterval",
    "TemporalPoint",
    "TemporalTransform",
    "Timebase",
]
