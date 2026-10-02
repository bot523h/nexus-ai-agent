"""Canonical Pure-Stdlib Temporal Truth Primitive Package for Nexus Creative OS.

Surface:
* :class:`Timebase` — Canonical GCD-normalized rational frame/sample/clock rate.
* :class:`FrameRateResolver` — Standard NTSC rate profile resolver (23.976, 29.97, 59.94).
* :class:`RoundingPolicy` — Explicit rounding policies during timebase conversions.
* :class:`ConversionOutcome` — Loss-aware result container tracking exact residuals and error.
* :class:`Duration` — Nonnegative temporal span primitive (duration >= 0).
* :class:`TimePosition` — Signed temporal coordinate primitive (e.g. pre-roll, playhead position).
* :class:`FrameIndex` — Discrete nonnegative video frame address index.
* :class:`SampleIndex` — Discrete nonnegative audio sample address index.
* :class:`PointEvent` — Zero-duration point event or marker.
* :class:`TemporalInterval` — Half-open interval [start, end) with split/join algebra.
* :class:`TemporalTransform` — Exact retiming and speed scaling transformation.
* :class:`ClockRelation` — Cross-clock mapping between video frames and audio samples.
* :class:`TemporalPoint` — Backwards-compatibility alias for TimePosition.
* Adapters: :func:`duration_to_us`, :func:`us_to_duration`, :func:`timebase_to_dict`, :func:`timebase_from_spec`.
"""

from nexus_ai_agent.creative.temporal.adapters import (
    duration_to_us,
    timebase_from_spec,
    timebase_to_dict,
    us_to_duration,
)
from nexus_ai_agent.creative.temporal.core import (
    ClockRelation,
    ConversionOutcome,
    Duration,
    FrameIndex,
    FrameRateResolver,
    PointEvent,
    RoundingPolicy,
    SampleIndex,
    TemporalInterval,
    TemporalPoint,
    TemporalTransform,
    Timebase,
    TimePosition,
)

__all__ = [
    "ClockRelation",
    "ConversionOutcome",
    "Duration",
    "FrameIndex",
    "FrameRateResolver",
    "PointEvent",
    "RoundingPolicy",
    "SampleIndex",
    "TemporalInterval",
    "TemporalPoint",
    "TemporalTransform",
    "Timebase",
    "TimePosition",
    "duration_to_us",
    "timebase_from_spec",
    "timebase_to_dict",
    "us_to_duration",
]
