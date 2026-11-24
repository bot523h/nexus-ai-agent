"""Exact rational time over ``fractions.Fraction``.

Floats are rejected at the boundary: a frame at ``24000/1001`` fps is exactly
``1001/24000`` seconds, and one hundred thousand of them sum to a rational with
zero error.  Standard broadcast/cinema rates are first-class, and float input is
only accepted through explicit frame-grid quantisation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from functools import total_ordering
from types import MappingProxyType
from typing import Final

__all__ = [
    "STANDARD_FRAME_RATES",
    "RationalTime",
    "RationalTimeError",
    "RationalTimeRange",
    "parse_frame_rate",
]

STANDARD_FRAME_RATES: Final[Mapping[str, Fraction]] = MappingProxyType(
    {
        "24000/1001": Fraction(24000, 1001),
        "24/1": Fraction(24, 1),
        "25/1": Fraction(25, 1),
        "30000/1001": Fraction(30000, 1001),
        "60/1": Fraction(60, 1),
    }
)


class RationalTimeError(ValueError):
    """Raised when a time value cannot be represented exactly on the rational grid."""


def parse_frame_rate(fps: Fraction | int | str) -> Fraction:
    """Resolve a frame rate exactly, accepting fractions, ints and ``"N/D"``."""

    if isinstance(fps, bool):
        raise TypeError("frame rate must not be a bool")
    if isinstance(fps, Fraction):
        rate = fps
    elif isinstance(fps, int):
        rate = Fraction(fps, 1)
    elif isinstance(fps, str):
        if fps in STANDARD_FRAME_RATES:
            rate = STANDARD_FRAME_RATES[fps]
        else:
            try:
                rate = Fraction(fps)
            except (ValueError, ZeroDivisionError) as exc:
                raise RationalTimeError(f"cannot parse frame rate {fps!r}") from exc
    else:
        raise TypeError("frame rate must be a Fraction, int or 'N/D' string")
    if rate <= 0:
        raise RationalTimeError("frame rate must be strictly positive")
    return rate


@total_ordering
@dataclass(frozen=True, slots=True)
class RationalTime:
    """A duration in seconds held as an exact rational number."""

    seconds: Fraction

    def __post_init__(self) -> None:
        value: object = self.seconds
        if isinstance(value, bool) or isinstance(value, float):
            raise TypeError("RationalTime rejects float and bool; use from_float_quantized")
        if not isinstance(value, (Fraction, int)):
            raise TypeError("seconds must be a Fraction or int")
        object.__setattr__(self, "seconds", Fraction(value))
        if self.seconds < 0:
            raise RationalTimeError("RationalTime must be non-negative")

    @classmethod
    def zero(cls) -> RationalTime:
        return cls(Fraction(0, 1))

    @classmethod
    def from_seconds(cls, value: Fraction | int) -> RationalTime:
        return cls(Fraction(value))

    @classmethod
    def from_frames(cls, frames: int, fps: Fraction | int | str) -> RationalTime:
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise TypeError("frames must be an int")
        if frames < 0:
            raise RationalTimeError("frames must be non-negative")
        return cls(Fraction(frames, 1) / parse_frame_rate(fps))

    @classmethod
    def from_float_quantized(cls, value: float, fps: Fraction | int | str) -> RationalTime:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("value must be a real number")
        number = float(value)
        if not math.isfinite(number):
            raise RationalTimeError("value must be finite")
        if number < 0:
            raise RationalTimeError("value must be non-negative")
        frame = Fraction(1, 1) / parse_frame_rate(fps)
        frames = round(Fraction(number) / frame)
        return cls(Fraction(frames, 1) * frame)

    def to_frames(self, fps: Fraction | int | str) -> int:
        value = self.seconds * parse_frame_rate(fps)
        if value.denominator != 1:
            raise RationalTimeError(
                f"{self.seconds}s is not an integer number of frames at {fps} fps"
            )
        return value.numerator

    def as_float(self) -> float:
        return float(self.seconds)

    def __add__(self, other: RationalTime) -> RationalTime:
        if not isinstance(other, RationalTime):
            return NotImplemented
        return RationalTime(self.seconds + other.seconds)

    def __sub__(self, other: RationalTime) -> RationalTime:
        if not isinstance(other, RationalTime):
            return NotImplemented
        result = self.seconds - other.seconds
        if result < 0:
            raise RationalTimeError("subtraction would produce a negative duration")
        return RationalTime(result)

    def __mul__(self, factor: Fraction | int) -> RationalTime:
        if isinstance(factor, bool) or not isinstance(factor, (Fraction, int)):
            return NotImplemented
        if factor < 0:
            raise RationalTimeError("factor must be non-negative")
        return RationalTime(self.seconds * Fraction(factor))

    __rmul__ = __mul__

    def __lt__(self, other: RationalTime) -> bool:
        if not isinstance(other, RationalTime):
            return NotImplemented
        return self.seconds < other.seconds

    def __hash__(self) -> int:
        return hash(self.seconds)


@dataclass(frozen=True, slots=True)
class RationalTimeRange:
    """A half-open exact interval ``[start, start + duration)``."""

    start: RationalTime
    duration: RationalTime

    def __post_init__(self) -> None:
        if not isinstance(self.start, RationalTime):
            raise TypeError("start must be a RationalTime")
        if not isinstance(self.duration, RationalTime):
            raise TypeError("duration must be a RationalTime")
        if self.duration.seconds <= 0:
            raise RationalTimeError("range duration must be strictly positive")

    @property
    def end(self) -> RationalTime:
        return self.start + self.duration

    def contains(self, other: RationalTime) -> bool:
        if not isinstance(other, RationalTime):
            raise TypeError("other must be a RationalTime")
        return self.start <= other < self.end

    def overlaps(self, other: RationalTimeRange) -> bool:
        if not isinstance(other, RationalTimeRange):
            raise TypeError("other must be a RationalTimeRange")
        return self.start < other.end and other.start < self.end
