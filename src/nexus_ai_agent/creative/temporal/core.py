"""Canonical Pure-Stdlib Temporal Algebra Core for Nexus Creative OS.

Zero-dependency, pure Python stdlib implementation:
* :class:`RoundingPolicy` — Explicit rounding modes for discrete time conversions.
* :class:`Timebase` — Canonical normalized rational frame/sample/clock rate.
* :class:`FrameRateResolver` — Standard NTSC rate profile resolver (23.976, 29.97, 59.94).
* :class:`ConversionOutcome` — Loss-aware result container tracking exact residuals and error.
* :class:`Duration` — Nonnegative temporal span primitive (duration >= 0).
* :class:`TimePosition` — Signed temporal coordinate primitive (e.g. pre-roll, playhead position).
* :class:`FrameIndex` — Discrete nonnegative video frame address index.
* :class:`SampleIndex` — Discrete nonnegative audio sample address index.
* :class:`PointEvent` / :class:`Marker` — Point event without duration.
* :class:`TemporalInterval` — Half-open [start, end) interval span with split/join algebra.
* :class:`TemporalTransform` — Exact retiming and speed scaling transformation.
* :class:`ClockRelation` — Cross-clock video frame and audio sample mapping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Any, Generic, TypeVar


class RoundingPolicy(str, Enum):
    """Explicit rounding policy for converting exact temporal values to discrete units."""

    EXACT = "exact"
    FLOOR = "floor"
    CEIL = "ceil"
    NEAREST = "nearest"
    TRUNCATE = "truncate"


@dataclass(frozen=True, slots=True)
class Timebase:
    """Canonical, normalized rational rate representation.

    Enforces GCD normalization upon construction so that Timebase(60, 2) == Timebase(30, 1).
    """

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.numerator, bool)
            or not isinstance(self.numerator, int)
            or self.numerator <= 0
        ):
            raise ValueError(f"Timebase numerator must be positive integer, got {self.numerator!r}")
        if (
            isinstance(self.denominator, bool)
            or not isinstance(self.denominator, int)
            or self.denominator <= 0
        ):
            raise ValueError(
                f"Timebase denominator must be positive integer, got {self.denominator!r}"
            )

        gcd = math.gcd(self.numerator, self.denominator)
        if gcd > 1:
            object.__setattr__(self, "numerator", self.numerator // gcd)
            object.__setattr__(self, "denominator", self.denominator // gcd)

    @property
    def rate(self) -> Fraction:
        """Exact rate in Hz (ticks per second) as a Fraction."""
        return Fraction(self.numerator, self.denominator)

    @property
    def period_seconds(self) -> Fraction:
        """Exact duration of 1 tick in seconds as a Fraction."""
        return Fraction(self.denominator, self.numerator)

    @classmethod
    def fps_23_976(cls) -> Timebase:
        return cls(24000, 1001)

    @classmethod
    def fps_24(cls) -> Timebase:
        return cls(24, 1)

    @classmethod
    def fps_25(cls) -> Timebase:
        return cls(25, 1)

    @classmethod
    def fps_29_97(cls) -> Timebase:
        return cls(30000, 1001)

    @classmethod
    def fps_30(cls) -> Timebase:
        return cls(30, 1)

    @classmethod
    def fps_50(cls) -> Timebase:
        return cls(50, 1)

    @classmethod
    def fps_59_94(cls) -> Timebase:
        return cls(60000, 1001)

    @classmethod
    def fps_60(cls) -> Timebase:
        return cls(60, 1)

    @classmethod
    def audio_44100(cls) -> Timebase:
        return cls(44100, 1)

    @classmethod
    def audio_48000(cls) -> Timebase:
        return cls(48000, 1)

    @classmethod
    def microseconds(cls) -> Timebase:
        return cls(1_000_000, 1)

    @classmethod
    def nanoseconds(cls) -> Timebase:
        return cls(1_000_000_000, 1)


class FrameRateResolver:
    """Explicit resolver for standard frame-rate profiles and aliases."""

    _STANDARD_PROFILES: dict[Any, Timebase] = {
        "23.976": Timebase.fps_23_976(),
        "23.976fps": Timebase.fps_23_976(),
        23.976: Timebase.fps_23_976(),
        "24000/1001": Timebase.fps_23_976(),
        Fraction(24000, 1001): Timebase.fps_23_976(),
        "24": Timebase.fps_24(),
        "24fps": Timebase.fps_24(),
        24: Timebase.fps_24(),
        "25": Timebase.fps_25(),
        "25fps": Timebase.fps_25(),
        25: Timebase.fps_25(),
        "29.97": Timebase.fps_29_97(),
        "29.97fps": Timebase.fps_29_97(),
        29.97: Timebase.fps_29_97(),
        "30000/1001": Timebase.fps_29_97(),
        Fraction(30000, 1001): Timebase.fps_29_97(),
        "30": Timebase.fps_30(),
        "30fps": Timebase.fps_30(),
        30: Timebase.fps_30(),
        "50": Timebase.fps_50(),
        "50fps": Timebase.fps_50(),
        50: Timebase.fps_50(),
        "59.94": Timebase.fps_59_94(),
        "59.94fps": Timebase.fps_59_94(),
        59.94: Timebase.fps_59_94(),
        "60000/1001": Timebase.fps_59_94(),
        Fraction(60000, 1001): Timebase.fps_59_94(),
        "60": Timebase.fps_60(),
        "60fps": Timebase.fps_60(),
        60: Timebase.fps_60(),
    }

    @classmethod
    def resolve(cls, value: str | float | int | Fraction | Timebase) -> Timebase:
        """Resolve a rate specifier into a canonical Timebase."""
        if isinstance(value, Timebase):
            return value

        if isinstance(value, bool):
            raise TypeError("Boolean value is not a valid rate specifier")

        if value in cls._STANDARD_PROFILES:
            return cls._STANDARD_PROFILES[value]

        if isinstance(value, int):
            if value <= 0:
                raise ValueError(f"Frame rate integer must be positive, got {value}")
            return Timebase(value, 1)

        if isinstance(value, Fraction):
            if value <= 0:
                raise ValueError(f"Frame rate fraction must be positive, got {value}")
            return Timebase(value.numerator, value.denominator)

        if isinstance(value, float):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Frame rate float must be positive and finite, got {value}")
            frac = Fraction(value).limit_denominator(100000)
            return Timebase(frac.numerator, frac.denominator)

        if isinstance(value, str):
            clean = value.strip().lower().removesuffix("fps").removesuffix("hz")
            if clean in cls._STANDARD_PROFILES:
                return cls._STANDARD_PROFILES[clean]
            if "/" in clean:
                parts = [p.strip() for p in clean.split("/")]
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    num, den = int(parts[0]), int(parts[1])
                    if den > 0 and num > 0:
                        return Timebase(num, den)
                raise ValueError(f"Invalid timebase ratio specifier: {value!r}")
            try:
                if "." in clean:
                    flt = float(clean)
                    if not math.isfinite(flt) or flt <= 0:
                        raise ValueError(f"Frame rate float must be positive, got {flt}")
                    if flt in cls._STANDARD_PROFILES:
                        return cls._STANDARD_PROFILES[flt]
                    frac = Fraction(flt).limit_denominator(100000)
                    return Timebase(frac.numerator, frac.denominator)
                ival = int(clean)
                if ival <= 0:
                    raise ValueError(f"Frame rate integer must be positive, got {ival}")
                return Timebase(ival, 1)
            except ValueError as exc:
                raise ValueError(f"Cannot resolve rate specifier {value!r} to Timebase") from exc

        raise TypeError(f"Unsupported rate specifier type: {type(value)}")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ConversionOutcome(Generic[T]):
    """Loss-aware conversion outcome container."""

    value: T
    lossless: bool
    residual_seconds: Fraction
    absolute_error_seconds: Fraction
    rounding_policy: RoundingPolicy
    target_timebase: Timebase | None = None


def _round_exact_ticks(
    exact_ticks: Fraction, rounding: RoundingPolicy, timebase: Timebase
) -> ConversionOutcome[int]:
    num = exact_ticks.numerator
    den = exact_ticks.denominator

    if den == 1:
        return ConversionOutcome(
            value=num,
            lossless=True,
            residual_seconds=Fraction(0, 1),
            absolute_error_seconds=Fraction(0, 1),
            rounding_policy=rounding,
            target_timebase=timebase,
        )

    if rounding == RoundingPolicy.EXACT:
        raise ValueError(
            f"Conversion to timebase {timebase.numerator}/{timebase.denominator} "
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
    unrounded_seconds = Fraction(num * timebase.denominator, den * timebase.numerator)
    residual_sec = unrounded_seconds - rounded_seconds

    return ConversionOutcome(
        value=value,
        lossless=(residual_sec == Fraction(0, 1)),
        residual_seconds=residual_sec,
        absolute_error_seconds=abs(residual_sec),
        rounding_policy=rounding,
        target_timebase=timebase,
    )


class Duration:
    """Nonnegative temporal span primitive (duration >= 0)."""

    __slots__ = ("_seconds",)

    def __init__(self, seconds: Fraction | int | str | Duration) -> None:
        if isinstance(seconds, Duration):
            secs = seconds._seconds
        elif isinstance(seconds, Fraction):
            secs = seconds
        elif isinstance(seconds, bool):
            raise TypeError("Boolean value is not a valid Duration input")
        elif isinstance(seconds, int):
            secs = Fraction(seconds, 1)
        elif isinstance(seconds, str):
            secs = Fraction(seconds)
        else:
            raise TypeError(
                f"Duration requires Fraction, int, str, or Duration, got {type(seconds)}"
            )

        if secs < 0:
            raise ValueError(f"Duration must be nonnegative, got {secs}s")

        self._seconds = secs

    @property
    def seconds(self) -> Fraction:
        return self._seconds

    @classmethod
    def zero(cls) -> Duration:
        return cls(Fraction(0, 1))

    @classmethod
    def from_seconds(cls, s: Fraction | int | str) -> Duration:
        return cls(s)

    @classmethod
    def from_us(cls, us: int) -> Duration:
        if isinstance(us, bool) or not isinstance(us, int):
            raise TypeError(f"Microseconds duration must be integer, got {type(us)}")
        if us < 0:
            raise ValueError(f"Microseconds duration must be nonnegative, got {us}")
        return cls(Fraction(us, 1_000_000))

    @classmethod
    def from_ticks(cls, ticks: int, timebase: Timebase | str | float) -> Duration:
        tb = FrameRateResolver.resolve(timebase)
        if isinstance(ticks, bool) or not isinstance(ticks, int):
            raise TypeError(f"Ticks duration must be integer, got {type(ticks)}")
        if ticks < 0:
            raise ValueError(f"Ticks duration must be nonnegative, got {ticks}")
        secs = Fraction(ticks * tb.denominator, tb.numerator)
        return cls(secs)

    def to_seconds(self) -> Fraction:
        return self._seconds

    def to_us(self, rounding: RoundingPolicy = RoundingPolicy.NEAREST) -> ConversionOutcome[int]:
        return self.to_ticks(Timebase.microseconds(), rounding)

    def to_ticks(
        self, timebase: Timebase | str | float, rounding: RoundingPolicy = RoundingPolicy.NEAREST
    ) -> ConversionOutcome[int]:
        tb = FrameRateResolver.resolve(timebase)
        exact_ticks = self._seconds * tb.rate
        return _round_exact_ticks(exact_ticks, rounding, tb)

    def __add__(self, other: Duration) -> Duration:
        if isinstance(other, Duration):
            return Duration(self._seconds + other._seconds)
        return NotImplemented

    def __sub__(self, other: Duration) -> Duration:
        if isinstance(other, Duration):
            diff = self._seconds - other._seconds
            if diff < 0:
                raise ValueError(
                    f"Duration subtraction result cannot be negative: "
                    f"{self._seconds}s - {other._seconds}s"
                )
            return Duration(diff)
        return NotImplemented

    def __mul__(self, scalar: Fraction | int) -> Duration:
        if isinstance(scalar, bool):
            return NotImplemented
        if isinstance(scalar, (int, Fraction)):
            if scalar < 0:
                raise ValueError(f"Duration multiplier must be nonnegative, got {scalar}")
            return Duration(self._seconds * scalar)
        return NotImplemented

    def __rmul__(self, scalar: Fraction | int) -> Duration:
        return self.__mul__(scalar)

    def __truediv__(self, other: Duration | Fraction | int) -> Duration | Fraction:
        if isinstance(other, Duration):
            if other._seconds == 0:
                raise ZeroDivisionError("Cannot divide Duration by zero Duration")
            return self._seconds / other._seconds
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, (int, Fraction)):
            if other <= 0:
                raise ValueError("Duration divisor scalar must be strictly positive")
            return Duration(self._seconds / other)
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Duration):
            return self._seconds == other._seconds
        return False

    def __lt__(self, other: Duration) -> bool:
        if isinstance(other, Duration):
            return self._seconds < other._seconds
        return NotImplemented

    def __le__(self, other: Duration) -> bool:
        if isinstance(other, Duration):
            return self._seconds <= other._seconds
        return NotImplemented

    def __gt__(self, other: Duration) -> bool:
        if isinstance(other, Duration):
            return self._seconds > other._seconds
        return NotImplemented

    def __ge__(self, other: Duration) -> bool:
        if isinstance(other, Duration):
            return self._seconds >= other._seconds
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._seconds)

    def __repr__(self) -> str:
        return f"Duration({self._seconds}s)"


class TimePosition:
    """Signed time coordinate primitive (e.g. pre-roll offset, playhead position)."""

    __slots__ = ("_seconds",)

    def __init__(self, seconds: Fraction | int | str | TimePosition) -> None:
        if isinstance(seconds, TimePosition):
            secs = seconds._seconds
        elif isinstance(seconds, Fraction):
            secs = seconds
        elif isinstance(seconds, bool):
            raise TypeError("Boolean value is not a valid TimePosition input")
        elif isinstance(seconds, int):
            secs = Fraction(seconds, 1)
        elif isinstance(seconds, str):
            secs = Fraction(seconds)
        else:
            raise TypeError(
                f"TimePosition requires Fraction, int, str, or TimePosition, got {type(seconds)}"
            )

        self._seconds = secs

    @property
    def seconds(self) -> Fraction:
        return self._seconds

    @classmethod
    def zero(cls) -> TimePosition:
        return cls(Fraction(0, 1))

    @classmethod
    def from_seconds(cls, s: Fraction | int | str) -> TimePosition:
        return cls(s)

    @classmethod
    def from_us(cls, us: int) -> TimePosition:
        if isinstance(us, bool) or not isinstance(us, int):
            raise TypeError(f"Microseconds position must be integer, got {type(us)}")
        return cls(Fraction(us, 1_000_000))

    @classmethod
    def from_ticks(cls, ticks: int, timebase: Timebase | str | float) -> TimePosition:
        tb = FrameRateResolver.resolve(timebase)
        if isinstance(ticks, bool) or not isinstance(ticks, int):
            raise TypeError(f"Ticks position must be integer, got {type(ticks)}")
        secs = Fraction(ticks * tb.denominator, tb.numerator)
        return cls(secs)

    def to_seconds(self) -> Fraction:
        return self._seconds

    def to_us(self, rounding: RoundingPolicy = RoundingPolicy.NEAREST) -> ConversionOutcome[int]:
        return self.to_ticks(Timebase.microseconds(), rounding)

    def to_ticks(
        self, timebase: Timebase | str | float, rounding: RoundingPolicy = RoundingPolicy.NEAREST
    ) -> ConversionOutcome[int]:
        tb = FrameRateResolver.resolve(timebase)
        exact_ticks = self._seconds * tb.rate
        return _round_exact_ticks(exact_ticks, rounding, tb)

    def __add__(self, other: Duration) -> TimePosition:
        if isinstance(other, Duration):
            return TimePosition(self._seconds + other.seconds)
        if isinstance(other, TimePosition):
            raise TypeError(
                "Adding two TimePositions is mathematically undefined. "
                "Add a Duration to a TimePosition instead."
            )
        return NotImplemented

    def __sub__(self, other: TimePosition | Duration) -> TimePosition | Duration:
        if isinstance(other, TimePosition):
            diff = self._seconds - other._seconds
            if diff < 0:
                raise ValueError(
                    f"TimePosition diff {diff}s is negative. Duration requires non-negative span."
                )
            return Duration(diff)
        if isinstance(other, Duration):
            return TimePosition(self._seconds - other.seconds)
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, TimePosition):
            return self._seconds == other._seconds
        return False

    def __lt__(self, other: TimePosition) -> bool:
        if isinstance(other, TimePosition):
            return self._seconds < other._seconds
        return NotImplemented

    def __le__(self, other: TimePosition) -> bool:
        if isinstance(other, TimePosition):
            return self._seconds <= other._seconds
        return NotImplemented

    def __gt__(self, other: TimePosition) -> bool:
        if isinstance(other, TimePosition):
            return self._seconds > other._seconds
        return NotImplemented

    def __ge__(self, other: TimePosition) -> bool:
        if isinstance(other, TimePosition):
            return self._seconds >= other._seconds
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._seconds)

    def __repr__(self) -> str:
        return f"TimePosition({self._seconds}s)"


# Backwards compatibility alias
TemporalPoint = TimePosition


@dataclass(frozen=True, slots=True)
class FrameIndex:
    """Discrete nonnegative frame address index (start of frame N)."""

    index: int
    timebase: Timebase

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError(f"FrameIndex must be a nonnegative integer, got {self.index!r}")

    def to_position(self) -> TimePosition:
        return TimePosition.from_ticks(self.index, self.timebase)


@dataclass(frozen=True, slots=True)
class SampleIndex:
    """Discrete nonnegative audio sample address index (start of sample N)."""

    index: int
    timebase: Timebase

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError(f"SampleIndex must be a nonnegative integer, got {self.index!r}")

    def to_position(self) -> TimePosition:
        return TimePosition.from_ticks(self.index, self.timebase)


@dataclass(frozen=True, slots=True)
class PointEvent:
    """Point event or marker at a specific TimePosition (zero duration)."""

    position: TimePosition
    label: str = ""


class TemporalInterval:
    """Half-open [start, end) time interval with exact algebraic operations."""

    __slots__ = ("_start", "_end")

    def __init__(self, start: TimePosition, end: TimePosition) -> None:
        if end < start:
            raise ValueError(
                f"TemporalInterval requires start <= end, got start={start} and end={end}"
            )
        self._start = start
        self._end = end

    @property
    def start(self) -> TimePosition:
        return self._start

    @property
    def end(self) -> TimePosition:
        return self._end

    @property
    def duration(self) -> Duration:
        return self._end - self._start

    @classmethod
    def from_start_duration(cls, start: TimePosition, duration: Duration) -> TemporalInterval:
        return cls(start, start + duration)

    def is_empty(self) -> bool:
        """True if interval span is empty [t, t)."""
        return self._start == self._end

    def contains(self, point: TimePosition) -> bool:
        """Check if point falls within half-open interval [start, end)."""
        return self._start <= point < self._end

    def overlaps(self, other: TemporalInterval) -> bool:
        """True if self and other share a non-zero time span."""
        max_start = max(self._start, other._start)
        min_end = min(self._end, other._end)
        return max_start < min_end

    def split(self, at_point: TimePosition) -> tuple[TemporalInterval, TemporalInterval]:
        """Split interval at given point into two adjacent intervals."""
        if at_point < self._start or at_point > self._end:
            raise ValueError(
                f"Split point {at_point} is outside interval [{self._start}, {self._end}]"
            )
        left = TemporalInterval(self._start, at_point)
        right = TemporalInterval(at_point, self._end)
        return left, right

    def join(self, other: TemporalInterval) -> TemporalInterval:
        """Join two adjacent intervals."""
        if self._end == other._start:
            return TemporalInterval(self._start, other._end)
        elif other._end == self._start:
            return TemporalInterval(other._start, self._end)
        else:
            raise ValueError(
                f"Cannot join non-adjacent intervals [{self._start}, {self._end}) "
                f"and [{other._start}, {other._end})"
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
        if isinstance(speed_ratio, bool):
            raise TypeError("Boolean speed_ratio is not allowed")
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

    def map_source_to_timeline_duration(self, source_duration: Duration) -> Duration:
        """Timeline duration = source duration / speed_ratio."""
        return source_duration / self._speed_ratio

    def map_timeline_to_source_duration(self, timeline_duration: Duration) -> Duration:
        """Source duration = timeline duration * speed_ratio."""
        return timeline_duration * self._speed_ratio


class ClockRelation:
    """Cross-clock frame and sample index converter."""

    @staticmethod
    def frame_to_sample(
        frame: FrameIndex | int,
        video_tb: Timebase | str | float,
        audio_tb: Timebase | str | float,
        rounding: RoundingPolicy = RoundingPolicy.NEAREST,
    ) -> ConversionOutcome[int]:
        """Convert video frame index to audio sample index."""
        if isinstance(frame, FrameIndex):
            v_tb = frame.timebase
            f_idx = frame.index
        else:
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
                raise ValueError(f"Frame index must be a nonnegative integer, got {frame!r}")
            v_tb = FrameRateResolver.resolve(video_tb)
            f_idx = frame

        a_tb = FrameRateResolver.resolve(audio_tb)
        pos = TimePosition.from_ticks(f_idx, v_tb)
        return pos.to_ticks(a_tb, rounding)

    @staticmethod
    def sample_to_frame(
        sample: SampleIndex | int,
        audio_tb: Timebase | str | float,
        video_tb: Timebase | str | float,
        rounding: RoundingPolicy = RoundingPolicy.NEAREST,
    ) -> ConversionOutcome[int]:
        """Convert audio sample index to video frame index."""
        if isinstance(sample, SampleIndex):
            a_tb = sample.timebase
            s_idx = sample.index
        else:
            if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
                raise ValueError(f"Sample index must be a nonnegative integer, got {sample!r}")
            a_tb = FrameRateResolver.resolve(audio_tb)
            s_idx = sample

        v_tb = FrameRateResolver.resolve(video_tb)
        pos = TimePosition.from_ticks(s_idx, a_tb)
        return pos.to_ticks(v_tb, rounding)
