"""Exact rational time for multi-rate timelines (Wave 2).

``RationalTime(value, time_base)`` is ``value`` ticks each lasting ``time_base``
seconds, with ``time_base`` an exact :class:`~fractions.Fraction`. That is the
OpenTimelineIO convention and the only representation that keeps an NTSC
``24000/1001`` frame grid exact: an integer "rate" cannot express 23.976 fps.

All arithmetic is exact. Addition/subtraction align both operands to the finest
common grid (the GCD of their time bases), so a multi-hour timeline accumulates
**zero** IEEE-754 drift.

Canonical frame rates are exposed as exact ``Fraction`` fps values, and the
matching time bases as ``Fraction(1, fps)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd

from .errors import RateMismatchError, RationalTimeError

__all__ = [
    "RationalTime",
    "TimeRange",
    "RATE_23_976",
    "RATE_24",
    "RATE_25",
    "RATE_29_97",
    "RATE_30",
    "RATE_59_94",
    "RATE_48000",
    "RATE_96000",
    "CANONICAL_RATES",
    "common_time_base",
    "frames_per_second",
]

#: Exact frame rates as fractions of frames per second.
RATE_23_976 = Fraction(24000, 1001)
RATE_29_97 = Fraction(30000, 1001)
RATE_59_94 = Fraction(60000, 1001)
RATE_24 = Fraction(24, 1)
RATE_25 = Fraction(25, 1)
RATE_30 = Fraction(30, 1)
RATE_48000 = Fraction(48000, 1)
RATE_96000 = Fraction(96000, 1)

CANONICAL_RATES: tuple[Fraction, ...] = (
    RATE_23_976,
    RATE_29_97,
    RATE_59_94,
    RATE_24,
    RATE_25,
    RATE_30,
    RATE_48000,
    RATE_96000,
)


def frames_per_second(time_base: Fraction) -> Fraction:
    """The exact frame rate for a time base (seconds per tick)."""
    if time_base <= 0:
        raise RationalTimeError("time_base must be positive")
    return Fraction(1, 1) / time_base


def common_time_base(a: Fraction, b: Fraction) -> Fraction:
    """The finest grid that both time bases land on exactly (GCD of fractions)."""
    if a <= 0 or b <= 0:
        raise RationalTimeError("time bases must be positive")
    num = gcd(a.numerator * b.denominator, b.numerator * a.denominator)
    den = a.denominator * b.denominator
    return Fraction(num, den)


@dataclass(frozen=True)
class RationalTime:
    """An exact instant: ``value`` ticks, each ``time_base`` seconds long."""

    value: int
    time_base: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.value, int):
            raise RationalTimeError(f"value must be an int, got {self.value!r}")
        if self.time_base <= 0:
            raise RationalTimeError(f"time_base must be positive, got {self.time_base}")

    @classmethod
    def at_fps(cls, value: int, fps: Fraction) -> RationalTime:
        """``value`` frames at an exact ``fps``."""
        if fps <= 0:
            raise RationalTimeError("fps must be positive")
        return cls(value, Fraction(1, 1) / fps)

    @classmethod
    def from_seconds(cls, seconds: Fraction, time_base: Fraction) -> RationalTime:
        """The exact tick on ``time_base`` equal to ``seconds`` (must land)."""
        ticks = Fraction(seconds) / time_base
        if ticks.denominator != 1:
            raise RateMismatchError(f"{seconds} s does not land on an exact tick of {time_base} s")
        return cls(int(ticks), time_base)

    @property
    def seconds(self) -> Fraction:
        return Fraction(self.value) * self.time_base

    @property
    def rate(self) -> Fraction:
        return frames_per_second(self.time_base)

    def rescale(self, time_base: Fraction) -> RationalTime:
        """Exact conversion to another time base (refuses a fractional tick)."""
        if time_base <= 0:
            raise RationalTimeError("time_base must be positive")
        if time_base == self.time_base:
            return self
        ticks = self.seconds / time_base
        if ticks.denominator != 1:
            raise RateMismatchError(
                f"{self.value}@{self.time_base} does not land on an exact tick of {time_base}"
            )
        return RationalTime(int(ticks), time_base)

    def _aligned(self, other: RationalTime) -> tuple[RationalTime, RationalTime]:
        base = common_time_base(self.time_base, other.time_base)
        return self.rescale(base), other.rescale(base)

    def __add__(self, other: RationalTime) -> RationalTime:
        a, b = self._aligned(other)
        return RationalTime(a.value + b.value, a.time_base)

    def __sub__(self, other: RationalTime) -> RationalTime:
        a, b = self._aligned(other)
        return RationalTime(a.value - b.value, a.time_base)

    def __lt__(self, other: RationalTime) -> bool:
        return self.seconds < other.seconds

    def __le__(self, other: RationalTime) -> bool:
        return self.seconds <= other.seconds

    def __gt__(self, other: RationalTime) -> bool:
        return self.seconds > other.seconds

    def __ge__(self, other: RationalTime) -> bool:
        return self.seconds >= other.seconds


@dataclass(frozen=True)
class TimeRange:
    """A half-open interval ``[start, start + duration)``."""

    start: RationalTime
    duration: RationalTime

    def __post_init__(self) -> None:
        if self.duration.seconds < 0:
            raise RationalTimeError("duration must be non-negative")

    @property
    def end(self) -> RationalTime:
        return self.start + self.duration

    @property
    def time_base(self) -> Fraction:
        return common_time_base(self.start.time_base, self.duration.time_base)

    def overlap(self, other: TimeRange) -> TimeRange | None:
        """The exact intersection, or ``None`` when the ranges are disjoint."""
        start = self.start if self.start >= other.start else other.start
        end = self.end if self.end <= other.end else other.end
        if end <= start:
            return None
        return TimeRange(start, end - start)

    def union(self, other: TimeRange) -> TimeRange:
        """The exact union, or raise when a gap separates the ranges."""
        touching = self.end == other.start or other.end == self.start
        if self.overlap(other) is None and not touching:
            raise RationalTimeError("cannot union disjoint ranges without a gap")
        start = self.start if self.start <= other.start else other.start
        end = self.end if self.end >= other.end else other.end
        return TimeRange(start, end - start)

    def clamp(self, bounds: TimeRange) -> TimeRange | None:
        """Clip this range to ``bounds``; ``None`` when fully outside."""
        return self.overlap(bounds)

    def contains(self, t: RationalTime) -> bool:
        return self.start <= t < self.end

    def split_on_frame(self, frame_ticks: int) -> tuple[RationalTime, ...]:
        """Split the range at exact ``frame_ticks`` boundaries from the start."""
        if frame_ticks <= 0:
            raise RationalTimeError("frame_ticks must be positive")
        base = self.time_base
        out: list[RationalTime] = []
        n = 0
        while True:
            t = self.start + RationalTime(frame_ticks * n, base)
            if t >= self.end:
                break
            out.append(t)
            n += 1
        return tuple(out)
