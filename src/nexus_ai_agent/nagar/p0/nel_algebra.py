"""Allen interval algebra, NEL edit operators and exact speed-ramp calculus.

All arithmetic stays in ``fractions.Fraction``.  Ripple inserts add exactly the
inserted duration to the timeline; roll, slip and slide changes leave the total
duration at exactly ``0/1``.  The speed-ramp inverse uses a 60-digit decimal
square root carried into a ``Fraction`` with explicit rational error bounds,
because the analytical inverse is generally irrational for a linear ramp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from fractions import Fraction
from typing import Final

__all__ = [
    "AllenRelation",
    "Clip",
    "Interval",
    "IntervalAlgebraError",
    "RampInversion",
    "SpeedRamp",
    "TimelineEdit",
    "classify",
    "invert",
    "ripple_insert",
    "roll_edit",
    "slide_edit",
    "slip_edit",
    "total_duration",
]

_DECIMAL_PRECISION: Final[int] = 60
_INVERSE_TOLERANCE: Final[Fraction] = Fraction(1, 10**18)


class IntervalAlgebraError(ValueError):
    """Raised when an interval or edit violates temporal invariants."""


def _fraction(value: Fraction | int, label: str, *, non_negative: bool = True) -> Fraction:
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, (Fraction, int))
    ):
        raise TypeError(f"{label} must be an exact Fraction or int")
    result = Fraction(value)
    if non_negative and result < 0:
        raise IntervalAlgebraError(f"{label} must be non-negative")
    return result


@dataclass(frozen=True, slots=True)
class Interval:
    """A closed timeline interval ``[start, end]`` with exact rational endpoints."""

    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        start = _fraction(self.start, "start")
        end = _fraction(self.end, "end")
        if end <= start:
            raise IntervalAlgebraError("interval end must exceed start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


class AllenRelation(Enum):
    BEFORE = "BEFORE"
    MEETS = "MEETS"
    OVERLAPS = "OVERLAPS"
    FINISHED_BY = "FINISHED_BY"
    CONTAINS = "CONTAINS"
    STARTS = "STARTS"
    EQUALS = "EQUALS"
    STARTED_BY = "STARTED_BY"
    DURING = "DURING"
    FINISHES = "FINISHES"
    OVERLAPPED_BY = "OVERLAPPED_BY"
    MET_BY = "MET_BY"
    AFTER = "AFTER"


INVERSE: Final[Mapping[AllenRelation, AllenRelation]] = {
    AllenRelation.BEFORE: AllenRelation.AFTER,
    AllenRelation.AFTER: AllenRelation.BEFORE,
    AllenRelation.MEETS: AllenRelation.MET_BY,
    AllenRelation.MET_BY: AllenRelation.MEETS,
    AllenRelation.OVERLAPS: AllenRelation.OVERLAPPED_BY,
    AllenRelation.OVERLAPPED_BY: AllenRelation.OVERLAPS,
    AllenRelation.FINISHED_BY: AllenRelation.FINISHES,
    AllenRelation.FINISHES: AllenRelation.FINISHED_BY,
    AllenRelation.CONTAINS: AllenRelation.DURING,
    AllenRelation.DURING: AllenRelation.CONTAINS,
    AllenRelation.STARTS: AllenRelation.STARTED_BY,
    AllenRelation.STARTED_BY: AllenRelation.STARTS,
    AllenRelation.EQUALS: AllenRelation.EQUALS,
}


def invert(relation: AllenRelation) -> AllenRelation:
    if not isinstance(relation, AllenRelation):
        raise TypeError("relation must be an AllenRelation")
    return INVERSE[relation]


def classify(first: Interval, second: Interval) -> AllenRelation:
    """Return the unique, exhaustive Allen relation of ``first`` to ``second``."""

    if not isinstance(first, Interval) or not isinstance(second, Interval):
        raise TypeError("classify requires two Interval values")
    if first.start == second.start and first.end == second.end:
        return AllenRelation.EQUALS
    if first.end < second.start:
        return AllenRelation.BEFORE
    if first.end == second.start:
        return AllenRelation.MEETS
    if second.end < first.start:
        return AllenRelation.AFTER
    if second.end == first.start:
        return AllenRelation.MET_BY
    if first.start < second.start:
        if first.end < second.end:
            return AllenRelation.OVERLAPS
        if first.end == second.end:
            return AllenRelation.FINISHED_BY
        return AllenRelation.CONTAINS
    if first.start == second.start:
        if first.end < second.end:
            return AllenRelation.STARTS
        return AllenRelation.STARTED_BY
    if first.end < second.end:
        return AllenRelation.DURING
    if first.end == second.end:
        return AllenRelation.FINISHES
    return AllenRelation.OVERLAPPED_BY


@dataclass(frozen=True, slots=True)
class Clip:
    """A timeline interval with the source-media in-point it reads from."""

    interval: Interval
    source_in: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.interval, Interval):
            raise TypeError("interval must be an Interval")
        object.__setattr__(self, "source_in", _fraction(self.source_in, "source_in"))


@dataclass(frozen=True, slots=True)
class TimelineEdit:
    clips: tuple[Clip, ...]
    total_duration: Fraction
    delta_duration: Fraction

    def __post_init__(self) -> None:
        clips = tuple(self.clips)
        for clip in clips:
            if not isinstance(clip, Clip):
                raise TypeError("clips must contain Clip values")
        object.__setattr__(self, "clips", clips)
        object.__setattr__(self, "total_duration", _fraction(self.total_duration, "total"))
        object.__setattr__(self, "delta_duration", _fraction(self.delta_duration, "delta"))
        expected = total_duration(clips)
        if expected != self.total_duration:
            raise IntervalAlgebraError("total_duration disagrees with the clip sum")


def total_duration(clips: Sequence[Clip]) -> Fraction:
    """Timeline extent from zero to the furthest clip end."""

    if not clips:
        return Fraction(0, 1)
    return max(clip.interval.end for clip in clips)


def ripple_insert(clips: Sequence[Clip], at: Fraction, duration: Fraction) -> TimelineEdit:
    """Insert ``duration`` at ``at``; every later clip shifts right by exactly it."""

    point = _fraction(at, "at")
    inserted = _fraction(duration, "duration")
    if inserted <= 0:
        raise IntervalAlgebraError("inserted duration must be strictly positive")
    updated: list[Clip] = []
    for clip in clips:
        if clip.interval.end <= point:
            updated.append(clip)
        elif clip.interval.start >= point:
            updated.append(
                Clip(
                    Interval(clip.interval.start + inserted, clip.interval.end + inserted),
                    clip.source_in,
                )
            )
        else:
            left = Clip(Interval(clip.interval.start, point), clip.source_in)
            right_out = clip.interval.end + inserted
            right_in = clip.source_in + (point - clip.interval.start)
            updated.append(left)
            updated.append(Clip(Interval(point + inserted, right_out), right_in))
    total = total_duration(updated)
    return TimelineEdit(tuple(updated), total, inserted)


def roll_edit(left: Interval, right: Interval, delta: Fraction) -> tuple[Interval, Interval]:
    """Move the shared cut by ``delta``; total duration is exactly unchanged."""

    if not isinstance(left, Interval) or not isinstance(right, Interval):
        raise TypeError("roll_edit requires two Interval values")
    if left.end != right.start:
        raise IntervalAlgebraError("roll_edit needs adjacent clips sharing a cut")
    shift = _fraction(delta, "delta", non_negative=False)
    new_left = Interval(left.start, left.end + shift)
    new_right = Interval(right.start + shift, right.end)
    return new_left, new_right


def slip_edit(clip: Clip, source_delta: Fraction) -> Clip:
    """Change the source in-point without moving the clip on the timeline."""

    if not isinstance(clip, Clip):
        raise TypeError("clip must be a Clip")
    shift = _fraction(source_delta, "source_delta", non_negative=False)
    resulting = clip.source_in + shift
    if resulting < 0:
        raise IntervalAlgebraError("slip_edit would move before the source start")
    return Clip(clip.interval, resulting)


def slide_edit(clips: Sequence[Clip], index: int, delta: Fraction) -> tuple[Clip, ...]:
    """Move one clip, stretching its neighbours; total duration stays exact."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an int")
    if index <= 0 or index >= len(clips) - 1:
        raise IntervalAlgebraError("slide_edit needs a clip with two neighbours")
    shift = _fraction(delta, "delta", non_negative=False)
    ordered = list(clips)
    previous = ordered[index - 1]
    target = ordered[index]
    following = ordered[index + 1]
    if previous.interval.end != target.interval.start:
        raise IntervalAlgebraError("slide_edit requires contiguous clips")
    if target.interval.end != following.interval.start:
        raise IntervalAlgebraError("slide_edit requires contiguous clips")
    new_previous = Clip(
        Interval(previous.interval.start, previous.interval.end + shift), previous.source_in
    )
    new_target = Clip(
        Interval(target.interval.start + shift, target.interval.end + shift), target.source_in
    )
    new_following = Clip(
        Interval(following.interval.start + shift, following.interval.end), following.source_in
    )
    ordered[index - 1] = new_previous
    ordered[index] = new_target
    ordered[index + 1] = new_following
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class RampInversion:
    tau: Fraction
    time: Fraction
    lower: Fraction
    upper: Fraction

    def __post_init__(self) -> None:
        for name in ("tau", "time", "lower", "upper"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
                raise TypeError(f"{name} must be a Fraction")
            object.__setattr__(self, name, Fraction(value))
        if self.tau < 0 or self.time < 0 or self.lower < 0 or self.upper < 0:
            raise IntervalAlgebraError("ramp inversion values must be non-negative")
        if not self.lower <= self.time <= self.upper:
            raise IntervalAlgebraError("time must lie inside its rational bounds")


@dataclass(frozen=True, slots=True)
class SpeedRamp:
    """A strictly positive piecewise-linear speed curve on ``[t0, t1]``."""

    t0: Fraction
    t1: Fraction
    v0: Fraction
    v1: Fraction

    def __post_init__(self) -> None:
        start = _fraction(self.t0, "t0")
        end = _fraction(self.t1, "t1")
        initial = _fraction(self.v0, "v0")
        final = _fraction(self.v1, "v1")
        if end <= start:
            raise IntervalAlgebraError("t1 must exceed t0")
        if initial <= 0 or final <= 0:
            raise IntervalAlgebraError("speed must be strictly positive")
        object.__setattr__(self, "t0", start)
        object.__setattr__(self, "t1", end)
        object.__setattr__(self, "v0", initial)
        object.__setattr__(self, "v1", final)

    @property
    def span(self) -> Fraction:
        return self.t1 - self.t0

    @property
    def slope(self) -> Fraction:
        return (self.v1 - self.v0) / self.span

    def instant_speed(self, time: Fraction) -> Fraction:
        moment = _fraction(time, "time")
        if not self.t0 <= moment <= self.t1:
            raise IntervalAlgebraError("time lies outside the ramp")
        return self.v0 + self.slope * (moment - self.t0)

    def displacement_at(self, time: Fraction) -> Fraction:
        moment = _fraction(time, "time")
        if not self.t0 <= moment <= self.t1:
            raise IntervalAlgebraError("time lies outside the ramp")
        offset = moment - self.t0
        return self.v0 * offset + Fraction(1, 2) * self.slope * offset * offset

    def displacement(self) -> Fraction:
        """Exact trapezoidal quadrature of the speed curve."""

        return Fraction(1, 2) * (self.v0 + self.v1) * self.span

    def invert(self, tau: Fraction) -> RampInversion:
        """Solve ``displacement_at(t) = tau`` for the exact-rational time boundary."""

        target = _fraction(tau, "tau")
        if target < 0 or target > self.displacement():
            raise IntervalAlgebraError("tau lies outside the ramp displacement")
        if target == 0:
            zero = self.t0
            return RampInversion(target, zero, zero, zero)
        slope = self.slope
        if slope == 0:
            offset = target / self.v0
        else:
            quadratic = Fraction(1, 2) * slope
            discriminant = self.v0 * self.v0 + 4 * quadratic * target
            if discriminant < 0:  # pragma: no cover - positive speeds guarantee this
                raise IntervalAlgebraError("ramp inverse has no real root")
            with localcontext() as context:
                context.prec = _DECIMAL_PRECISION
                root = (Decimal(discriminant.numerator) / Decimal(discriminant.denominator)).sqrt()
                b_decimal = Decimal(self.v0.numerator) / Decimal(self.v0.denominator)
                a_decimal = Decimal(quadratic.numerator) / Decimal(quadratic.denominator)
                offset = Fraction((-b_decimal + root) / (Decimal(2) * a_decimal))
        moment = self.t0 + offset
        lower = max(self.t0, moment - _INVERSE_TOLERANCE)
        upper = min(self.t1, moment + _INVERSE_TOLERANCE)
        return RampInversion(target, moment, lower, upper)

    def time_at_displacement(self, tau: Fraction) -> Fraction:
        return self.invert(tau).time

    def verify_inverse(self, tau: Fraction) -> bool:
        inversion = self.invert(tau)
        error = self.displacement_at(inversion.time) - inversion.tau
        return abs(error) <= _INVERSE_TOLERANCE * max(Fraction(1), abs(inversion.tau))
