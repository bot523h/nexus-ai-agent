"""Canonical Temporal Algebra Core for Nexus Creative OS.

This module provides exact, composable, rational-based temporal primitives:
* :class:`Timebase` — Exact rational frame/sample/clock rate representation.
* :class:`RoundingPolicy` — Explicit rounding modes for timebase conversion.
* :class:`ConversionOutcome` — Loss-aware result container capturing exact remainders.
* :class:`TemporalPoint` — Exact immutable time point and duration primitive.
* :class:`TemporalInterval` — Half-open [start, end) interval with split/join algebra.
* :class:`TemporalTransform` — Exact speed scaling and retiming transformation.
* :class:`ClockRelation` — Cross-clock video frame and audio sample mapping.
"""

from __future__ import annotations

from enum import Enum
from fractions import Fraction
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class RoundingPolicy(str, Enum):
    """Explicit rounding policy for converting exact temporal points to discrete units."""

    EXACT = "exact"
    FLOOR = "floor"
    CEIL = "ceil"
    NEAREST = "nearest"
    TRUNCATE = "truncate"


class Timebase(BaseModel):
    """Exact rational rate representation (e.g., 24000/1001 for 23.976 fps)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(gt=0, description="Rate numerator (e.g. 24000 or 30).")
    denominator: int = Field(gt=0, description="Rate denominator (e.g. 1001 or 1).")

    @property
    def rate(self) -> Fraction:
        """Exact rate in Hz (ticks per second) as a Fraction."""
        return Fraction(self.numerator, self.denominator)

    @property
    def period_seconds(self) -> Fraction:
        """Exact duration of 1 tick in seconds."""
        return Fraction(self.denominator, self.numerator)

    @classmethod
    def fps_23_976(cls) -> Timebase:
        return cls(numerator=24000, denominator=1001)

    @classmethod
    def fps_24(cls) -> Timebase:
        return cls(numerator=24, denominator=1)

    @classmethod
    def fps_25(cls) -> Timebase:
        return cls(numerator=25, denominator=1)

    @classmethod
    def fps_29_97(cls) -> Timebase:
        return cls(numerator=30000, denominator=1001)

    @classmethod
    def fps_30(cls) -> Timebase:
        return cls(numerator=30, denominator=1)

    @classmethod
    def fps_50(cls) -> Timebase:
        return cls(numerator=50, denominator=1)

    @classmethod
    def fps_59_94(cls) -> Timebase:
        return cls(numerator=60000, denominator=1001)

    @classmethod
    def fps_60(cls) -> Timebase:
        return cls(numerator=60, denominator=1)

    @classmethod
    def audio_44100(cls) -> Timebase:
        return cls(numerator=44100, denominator=1)

    @classmethod
    def audio_48000(cls) -> Timebase:
        return cls(numerator=48000, denominator=1)

    @classmethod
    def microseconds(cls) -> Timebase:
        return cls(numerator=1_000_000, denominator=1)

    @classmethod
    def nanoseconds(cls) -> Timebase:
        return cls(numerator=1_000_000_000, denominator=1)


T = TypeVar("T")


class ConversionOutcome(BaseModel, Generic[T]):
    """Loss-aware conversion outcome container."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T = Field(description="The converted discrete value (e.g., frame count or integer us).")
    lossless: bool = Field(description="True if conversion was exact with zero remainder.")
    remainder_seconds: Fraction = Field(
        description="Exact residual loss in seconds (unrounded exact minus rounded value)."
    )
    rounding_policy: RoundingPolicy = Field(description="Policy applied during conversion.")
    target_timebase: Timebase | None = Field(
        default=None, description="Timebase converted into, if applicable."
    )


class TemporalPoint:
    """Exact immutable time coordinate or duration represented as exact Fraction seconds."""

    __slots__ = ("_seconds",)

    def __init__(self, seconds: Fraction | int | str) -> None:
        if isinstance(seconds, Fraction):
            self._seconds = seconds
        elif isinstance(seconds, int):
            self._seconds = Fraction(seconds, 1)
        elif isinstance(seconds, str):
            self._seconds = Fraction(seconds)
        else:
            raise TypeError(f"TemporalPoint requires Fraction, int, or str, got {type(seconds)}")

    @property
    def seconds(self) -> Fraction:
        return self._seconds

    @classmethod
    def zero(cls) -> TemporalPoint:
        return cls(Fraction(0, 1))

    @classmethod
    def from_seconds(cls, s: Fraction | int | str) -> TemporalPoint:
        return cls(s)

    @classmethod
    def from_us(cls, us: int) -> TemporalPoint:
        return cls(Fraction(us, 1_000_000))

    @classmethod
    def from_ticks(cls, ticks: int, timebase: Timebase) -> TemporalPoint:
        """Construct exact temporal point from integer ticks on a timebase.

        seconds = ticks / rate = ticks * denominator / numerator.
        """
        secs = Fraction(ticks * timebase.denominator, timebase.numerator)
        return cls(secs)

    def to_seconds(self) -> Fraction:
        return self._seconds

    def to_us(
        self, rounding: RoundingPolicy = RoundingPolicy.NEAREST
    ) -> ConversionOutcome[int]:
        return self.to_ticks(Timebase.microseconds(), rounding)

    def to_ticks(
        self, timebase: Timebase, rounding: RoundingPolicy = RoundingPolicy.NEAREST
    ) -> ConversionOutcome[int]:
        """Convert exact temporal point to discrete integer ticks on target timebase."""
        exact_ticks = self._seconds * timebase.rate

        num = exact_ticks.numerator
        den = exact_ticks.denominator

        if den == 1:
            return ConversionOutcome(
                value=num,
                lossless=True,
                remainder_seconds=Fraction(0, 1),
                rounding_policy=rounding,
                target_timebase=timebase,
            )

        if rounding == RoundingPolicy.EXACT:
            raise ValueError(
                f"Conversion of {self._seconds}s to timebase {timebase.numerator}/{timebase.denominator} "
                f"is lossy (exact ticks: {exact_ticks}), but EXPLICIT policy was EXACT."
            )

        if rounding == RoundingPolicy.FLOOR:
            value = num // den
        elif rounding == RoundingPolicy.CEIL:
            value = num // den + (1 if num % den != 0 else 0)
        elif rounding == RoundingPolicy.TRUNCATE:
            if num >= 0:
                value = num // den
            else:
                value = -(-num // den)
        elif rounding == RoundingPolicy.NEAREST:
            floor_val = num // den
            remainder = exact_ticks - floor_val
            if remainder > Fraction(1, 2):
                value = floor_val + 1
            elif remainder < Fraction(1, 2):
                value = floor_val
            else:
                value = floor_val if floor_val % 2 == 0 else floor_val + 1
        else:
            raise ValueError(f"Unsupported rounding policy: {rounding}")

        rounded_seconds = Fraction(value * timebase.denominator, timebase.numerator)
        remainder_sec = self._seconds - rounded_seconds

        return ConversionOutcome(
            value=value,
            lossless=(remainder_sec == Fraction(0, 1)),
            remainder_seconds=remainder_sec,
            rounding_policy=rounding,
            target_timebase=timebase,
        )

    def __add__(self, other: TemporalPoint) -> TemporalPoint:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return TemporalPoint(self._seconds + other._seconds)

    def __sub__(self, other: TemporalPoint) -> TemporalPoint:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return TemporalPoint(self._seconds - other._seconds)

    def __mul__(self, scalar: Fraction | int) -> TemporalPoint:
        if isinstance(scalar, (int, Fraction)):
            return TemporalPoint(self._seconds * scalar)
        return NotImplemented

    def __rmul__(self, scalar: Fraction | int) -> TemporalPoint:
        return self.__mul__(scalar)

    def __truediv__(self, other: TemporalPoint | Fraction | int) -> TemporalPoint | Fraction:
        if isinstance(other, TemporalPoint):
            if other._seconds == 0:
                raise ZeroDivisionError("Cannot divide TemporalPoint by zero TemporalPoint")
            return self._seconds / other._seconds
        if isinstance(other, (int, Fraction)):
            if other == 0:
                raise ZeroDivisionError("Cannot divide TemporalPoint by zero scalar")
            return TemporalPoint(self._seconds / other)
        return NotImplemented

    def __neg__(self) -> TemporalPoint:
        return TemporalPoint(-self._seconds)

    def __abs__(self) -> TemporalPoint:
        return TemporalPoint(abs(self._seconds))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, TemporalPoint):
            return self._seconds == other._seconds
        return False

    def __lt__(self, other: TemporalPoint) -> bool:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return self._seconds < other._seconds

    def __le__(self, other: TemporalPoint) -> bool:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return self._seconds <= other._seconds

    def __gt__(self, other: TemporalPoint) -> bool:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return self._seconds > other._seconds

    def __ge__(self, other: TemporalPoint) -> bool:
        if not isinstance(other, TemporalPoint):
            return NotImplemented
        return self._seconds >= other._seconds

    def __hash__(self) -> int:
        return hash(self._seconds)

    def __repr__(self) -> str:
        return f"TemporalPoint({self._seconds}s)"


class TemporalInterval:
    """Half-open [start, end) time interval with exact algebraic operations."""

    __slots__ = ("_start", "_end", "_allow_zero")

    def __init__(
        self, start: TemporalPoint, end: TemporalPoint, *, allow_zero: bool = True
    ) -> None:
        if end < start:
            raise ValueError(
                f"TemporalInterval requires start <= end, got start={start} and end={end}"
            )
        if not allow_zero and start == end:
            raise ValueError("Zero-length interval is prohibited when allow_zero=False")

        self._start = start
        self._end = end
        self._allow_zero = allow_zero

    @property
    def start(self) -> TemporalPoint:
        return self._start

    @property
    def end(self) -> TemporalPoint:
        return self._end

    @property
    def duration(self) -> TemporalPoint:
        return self._end - self._start

    @property
    def allow_zero(self) -> bool:
        return self._allow_zero

    @classmethod
    def from_start_duration(
        cls, start: TemporalPoint, duration: TemporalPoint, *, allow_zero: bool = True
    ) -> TemporalInterval:
        if duration < TemporalPoint.zero():
            raise ValueError(f"Negative duration {duration} is strictly prohibited")
        return cls(start, start + duration, allow_zero=allow_zero)

    def contains_point(self) -> bool:
        """True if interval is a zero-length point event [t, t)."""
        return self._start == self._end

    def contains(self, point: TemporalPoint) -> bool:
        """Check if point falls within half-open interval [start, end)."""
        if self.contains_point():
            return point == self._start
        return self._start <= point < self._end

    def overlaps(self, other: TemporalInterval) -> bool:
        """True if self and other share a non-zero time span."""
        max_start = max(self._start, other._start)
        min_end = min(self._end, other._end)
        return max_start < min_end

    def split(self, at_point: TemporalPoint) -> tuple[TemporalInterval, TemporalInterval]:
        """Split interval at given point into two adjacent intervals [start, at_point) and [at_point, end)."""
        if at_point < self._start or at_point > self._end:
            raise ValueError(
                f"Split point {at_point} is outside interval [{self._start}, {self._end}]"
            )
        left = TemporalInterval(self._start, at_point, allow_zero=self._allow_zero)
        right = TemporalInterval(at_point, self._end, allow_zero=self._allow_zero)
        return left, right

    def join(self, other: TemporalInterval) -> TemporalInterval:
        """Join two adjacent intervals."""
        if self._end == other._start:
            return TemporalInterval(
                self._start, other._end, allow_zero=self._allow_zero and other._allow_zero
            )
        elif other._end == self._start:
            return TemporalInterval(
                other._start, self._end, allow_zero=self._allow_zero and other._allow_zero
            )
        else:
            raise ValueError(
                f"Cannot join non-adjacent intervals [{self._start}, {self._end}) and [{other._start}, {other._end})"
            )

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, TemporalInterval):
            return self._start == other._start and self._end == other._end
        return False

    def __hash__(self) -> int:
        return hash((self._start, self._end))

    def __repr__(self) -> str:
        return f"TemporalInterval[{self._start} .. {self._end})"


class TemporalTransform:
    """Exact speed scaling and retiming transformation."""

    __slots__ = ("_speed_ratio",)

    def __init__(self, speed_ratio: Fraction | int | str) -> None:
        if isinstance(speed_ratio, (int, str)):
            speed = Fraction(speed_ratio)
        elif isinstance(speed_ratio, Fraction):
            speed = speed_ratio
        else:
            raise TypeError(f"speed_ratio must be Fraction, int, or str, got {type(speed_ratio)}")

        if speed <= 0:
            raise ValueError(f"Speed ratio must be strictly positive, got {speed}")

        self._speed_ratio = speed

    @property
    def speed_ratio(self) -> Fraction:
        return self._speed_ratio

    @property
    def is_reversible(self) -> bool:
        return self._speed_ratio > 0

    def map_source_to_timeline_duration(self, source_duration: TemporalPoint) -> TemporalPoint:
        """Timeline duration = source duration / speed_ratio."""
        return source_duration / self._speed_ratio

    def map_timeline_to_source_duration(self, timeline_duration: TemporalPoint) -> TemporalPoint:
        """Source duration = timeline duration * speed_ratio."""
        return timeline_duration * self._speed_ratio


class ClockRelation:
    """Cross-clock frame and sample index converter."""

    @staticmethod
    def frame_to_sample(
        frame_idx: int,
        video_tb: Timebase,
        audio_tb: Timebase,
        rounding: RoundingPolicy = RoundingPolicy.NEAREST,
    ) -> ConversionOutcome[int]:
        """Convert video frame index to audio sample index."""
        pt = TemporalPoint.from_ticks(frame_idx, video_tb)
        return pt.to_ticks(audio_tb, rounding)

    @staticmethod
    def sample_to_frame(
        sample_idx: int,
        audio_tb: Timebase,
        video_tb: Timebase,
        rounding: RoundingPolicy = RoundingPolicy.NEAREST,
    ) -> ConversionOutcome[int]:
        """Convert audio sample index to video frame index."""
        pt = TemporalPoint.from_ticks(sample_idx, audio_tb)
        return pt.to_ticks(video_tb, rounding)
