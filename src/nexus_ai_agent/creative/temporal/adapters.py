"""Boundary Adapters for Nexus Temporal Primitives.

Provides serialization and conversion adapters for studio Pydantic models,
OpenTimelineIO schema types, and legacy microsecond contracts.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.temporal.core import (
    ConversionOutcome,
    Duration,
    FrameRateResolver,
    RoundingPolicy,
    Timebase,
    TimePosition,
)


def duration_to_us(
    dur: Duration | int, rounding: RoundingPolicy = RoundingPolicy.NEAREST
) -> ConversionOutcome[int]:
    """Convert Duration or int microseconds to integer microseconds with loss tracking."""
    if isinstance(dur, int):
        dur = Duration.from_us(dur)
    return dur.to_us(rounding)


def us_to_duration(us: int) -> Duration:
    """Convert integer microseconds to Duration."""
    return Duration.from_us(us)


def timebase_to_dict(tb: Timebase) -> dict[str, Any]:
    """Serialize Timebase to canonical dict."""
    return {
        "numerator": tb.numerator,
        "denominator": tb.denominator,
        "rate": str(tb.rate),
    }


def timebase_from_spec(spec: str | float | int | dict[str, Any] | Timebase) -> Timebase:
    """Reconstruct Timebase from spec or dictionary."""
    if isinstance(spec, dict):
        return Timebase(int(spec["numerator"]), int(spec["denominator"]))
    return FrameRateResolver.resolve(spec)
