"""Wave 6: exact rational time, Allen algebra and speed-ramp calculus."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from nexus_ai_agent.nagar.p0.nel_algebra import (
    AllenRelation,
    Clip,
    Interval,
    IntervalAlgebraError,
    RampInversion,
    SpeedRamp,
    TimelineEdit,
    classify,
    invert,
    ripple_insert,
    roll_edit,
    slide_edit,
    slip_edit,
    total_duration,
)
from nexus_ai_agent.nagar.p0.semantic_time import (
    STANDARD_FRAME_RATES,
    RationalTime,
    RationalTimeError,
    RationalTimeRange,
    parse_frame_rate,
)

F = Fraction


def _clip(start: int, end: int, source_in: int = 0) -> Clip:
    return Clip(Interval(F(start), F(end)), F(source_in))


def test_parse_frame_rate_exact() -> None:
    assert parse_frame_rate("24000/1001") == F(24000, 1001)
    assert parse_frame_rate("25/1") == 25
    assert parse_frame_rate("50") == 50
    assert parse_frame_rate(F(30000, 1001)) == F(30000, 1001)
    assert parse_frame_rate(60) == 60
    assert set(STANDARD_FRAME_RATES) == {"24000/1001", "24/1", "25/1", "30000/1001", "60/1"}
    with pytest.raises(RationalTimeError):
        parse_frame_rate("nonsense")
    with pytest.raises(RationalTimeError):
        parse_frame_rate("0/1")
    with pytest.raises(RationalTimeError):
        parse_frame_rate(-24)
    with pytest.raises(TypeError):
        parse_frame_rate(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_frame_rate(24.0)  # type: ignore[arg-type]


def test_rational_time_construction_rules() -> None:
    assert RationalTime.zero().seconds == F(0, 1)
    assert RationalTime.from_seconds(3).seconds == 3
    with pytest.raises(TypeError):
        RationalTime(0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RationalTime(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RationalTime("1/2")  # type: ignore[arg-type]
    with pytest.raises(RationalTimeError):
        RationalTime(F(-1, 2))


def test_frame_time_is_exact() -> None:
    qt = RationalTime.from_frames(1, "24000/1001")
    assert qt.seconds == F(1001, 24000)
    assert qt.to_frames("24000/1001") == 1
    assert RationalTime.from_frames(24000, "24/1").seconds == 1000
    with pytest.raises(TypeError):
        RationalTime.from_frames(True, 24)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RationalTime.from_frames(1.0, 24)  # type: ignore[arg-type]
    with pytest.raises(RationalTimeError):
        RationalTime.from_frames(-1, 24)
    with pytest.raises(RationalTimeError):
        RationalTime(F(1, 5)).to_frames(24)


def test_hundred_thousand_frame_additions_have_zero_drift() -> None:
    frame = RationalTime.from_frames(1, "24000/1001")
    accumulated = RationalTime.zero()
    for _ in range(100_000):
        accumulated = accumulated + frame
    expected = RationalTime.from_frames(100_000, "24000/1001")
    assert accumulated.seconds == expected.seconds
    assert accumulated.seconds - expected.seconds == F(0, 1)


def test_float_quantisation_is_explicit() -> None:
    quantised = RationalTime.from_float_quantized(0.52, 25)
    assert quantised.seconds == F(13, 25)
    assert RationalTime.from_float_quantized(0.0, "24/1").seconds == 0
    with pytest.raises(TypeError):
        RationalTime.from_float_quantized(True, 24)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RationalTime.from_float_quantized("0.5", 24)  # type: ignore[arg-type]
    with pytest.raises(RationalTimeError):
        RationalTime.from_float_quantized(math.nan, 24)
    with pytest.raises(RationalTimeError):
        RationalTime.from_float_quantized(-0.5, 24)


def test_rational_arithmetic_and_ordering() -> None:
    first = RationalTime(F(1, 2))
    second = RationalTime(F(1, 3))
    assert (first + second).seconds == F(5, 6)
    assert (first - second).seconds == F(1, 6)
    assert (first * 3).seconds == F(3, 2)
    assert (3 * first).seconds == F(3, 2)
    assert (first * F(1, 2)).seconds == F(1, 4)
    assert first > second and second < first and first >= first and first <= first
    assert first.as_float() == 0.5
    assert hash(first) == hash(RationalTime(F(1, 2)))
    assert first != second
    with pytest.raises(RationalTimeError):
        second - first
    with pytest.raises(RationalTimeError):
        first * -1
    with pytest.raises(TypeError):
        first + 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        first - 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        first * True  # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = first < 1  # type: ignore[operator]


def test_rational_time_range() -> None:
    window = RationalTimeRange(RationalTime(F(1)), RationalTime(F(2)))
    assert window.end.seconds == 3
    assert window.contains(RationalTime(F(1)))
    assert not window.contains(RationalTime(F(3)))
    other = RationalTimeRange(RationalTime(F(2)), RationalTime(F(2)))
    assert window.overlaps(other)
    assert not window.overlaps(RationalTimeRange(RationalTime(F(5)), RationalTime(F(1))))
    with pytest.raises(RationalTimeError):
        RationalTimeRange(RationalTime(F(1)), RationalTime.zero())
    with pytest.raises(TypeError):
        RationalTimeRange(1, RationalTime(F(1)))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RationalTimeRange(RationalTime(F(1)), 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        window.contains(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        window.overlaps(1)  # type: ignore[arg-type]


# ---------------------------------------------------------------- Allen algebra


@pytest.mark.parametrize(
    ("first", "second", "relation"),
    [
        (Interval(F(0), F(1)), Interval(F(2), F(3)), AllenRelation.BEFORE),
        (Interval(F(0), F(1)), Interval(F(1), F(2)), AllenRelation.MEETS),
        (Interval(F(0), F(2)), Interval(F(1), F(3)), AllenRelation.OVERLAPS),
        (Interval(F(0), F(2)), Interval(F(1), F(2)), AllenRelation.FINISHED_BY),
        (Interval(F(0), F(3)), Interval(F(1), F(2)), AllenRelation.CONTAINS),
        (Interval(F(0), F(2)), Interval(F(0), F(3)), AllenRelation.STARTS),
        (Interval(F(0), F(2)), Interval(F(0), F(2)), AllenRelation.EQUALS),
        (Interval(F(0), F(3)), Interval(F(0), F(2)), AllenRelation.STARTED_BY),
        (Interval(F(1), F(2)), Interval(F(0), F(3)), AllenRelation.DURING),
        (Interval(F(1), F(2)), Interval(F(0), F(2)), AllenRelation.FINISHES),
        (Interval(F(1), F(3)), Interval(F(0), F(2)), AllenRelation.OVERLAPPED_BY),
        (Interval(F(1), F(2)), Interval(F(0), F(1)), AllenRelation.MET_BY),
        (Interval(F(2), F(3)), Interval(F(0), F(1)), AllenRelation.AFTER),
    ],
)
def test_allen_relations_are_exhaustive(
    first: Interval, second: Interval, relation: AllenRelation
) -> None:
    assert classify(first, second) is relation
    assert classify(second, first) is invert(relation)
    assert invert(invert(relation)) is relation


def test_interval_and_clip_validation() -> None:
    with pytest.raises(IntervalAlgebraError):
        Interval(F(2), F(2))
    with pytest.raises(IntervalAlgebraError):
        Interval(F(3), F(2))
    with pytest.raises(IntervalAlgebraError):
        Interval(F(-1), F(2))
    with pytest.raises(TypeError):
        Interval(0.0, F(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Interval(True, F(1))  # type: ignore[arg-type]
    assert Interval(F(0), F(2)).duration == 2
    with pytest.raises(TypeError):
        Clip("x", F(0))  # type: ignore[arg-type]
    with pytest.raises(IntervalAlgebraError):
        Clip(Interval(F(0), F(1)), F(-1))
    with pytest.raises(TypeError):
        Clip(Interval(F(0), F(1)), 0.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify("x", Interval(F(0), F(1)))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        invert("BEFORE")  # type: ignore[arg-type]


def test_ripple_insert_shifts_or_splits() -> None:
    clips = [_clip(0, 1), _clip(1, 2)]
    edited = ripple_insert(clips, F(1), F(1, 2))
    assert edited.clips[0] == clips[0]
    assert edited.clips[1].interval == Interval(F(3, 2), F(5, 2))
    assert edited.total_duration == F(5, 2)
    assert edited.delta_duration == F(1, 2)

    split = ripple_insert([_clip(0, 2)], F(1), F(1, 2))
    assert len(split.clips) == 2
    assert split.clips[0].interval == Interval(F(0), F(1))
    assert split.clips[1].interval == Interval(F(3, 2), F(5, 2))
    assert split.clips[1].source_in == F(1)
    assert split.total_duration == F(5, 2)

    assert ripple_insert([], F(0), F(1)).total_duration == 0
    with pytest.raises(IntervalAlgebraError):
        ripple_insert(clips, F(1), F(0))
    with pytest.raises(IntervalAlgebraError):
        ripple_insert(clips, F(-1), F(1))
    with pytest.raises(TypeError):
        ripple_insert(clips, 1.0, F(1))  # type: ignore[arg-type]


def test_timeline_edit_validation() -> None:
    clips = (_clip(0, 1),)
    TimelineEdit(clips, F(1), F(0))
    with pytest.raises(TypeError):
        TimelineEdit((object(),), F(1), F(0))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TimelineEdit(clips, 1.0, F(0))  # type: ignore[arg-type]
    with pytest.raises(IntervalAlgebraError):
        TimelineEdit(clips, F(2), F(0))
    with pytest.raises(TypeError):
        TimelineEdit(clips, F(1), 0.0)  # type: ignore[arg-type]


def test_roll_edit_preserves_total() -> None:
    left, right = Interval(F(0), F(2)), Interval(F(2), F(4))
    new_left, new_right = roll_edit(left, right, F(-1, 2))
    assert new_left == Interval(F(0), F(3, 2))
    assert new_right == Interval(F(3, 2), F(4))
    assert new_left.duration + new_right.duration == left.duration + right.duration
    with pytest.raises(IntervalAlgebraError):
        roll_edit(Interval(F(0), F(1)), Interval(F(2), F(3)), F(1))
    with pytest.raises(IntervalAlgebraError):
        roll_edit(left, right, F(-2))
    with pytest.raises(IntervalAlgebraError):
        roll_edit(left, right, F(2))
    with pytest.raises(TypeError):
        roll_edit("x", right, F(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        roll_edit(left, right, 1.0)  # type: ignore[arg-type]


def test_slip_edit_moves_only_source() -> None:
    clip = _clip(1, 2, source_in=3)
    slipped = slip_edit(clip, F(-1))
    assert slipped.interval == clip.interval
    assert slipped.source_in == F(2)
    with pytest.raises(IntervalAlgebraError):
        slip_edit(clip, F(-4))
    with pytest.raises(TypeError):
        slip_edit("x", F(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        slip_edit(clip, 1.0)  # type: ignore[arg-type]


def test_slide_edit_preserves_total() -> None:
    clips = [_clip(0, 1), _clip(1, 2), _clip(2, 3)]
    before = total_duration(clips)
    edited = slide_edit(clips, 1, F(1, 2))
    assert edited[0].interval == Interval(F(0), F(3, 2))
    assert edited[1].interval == Interval(F(3, 2), F(5, 2))
    assert edited[2].interval == Interval(F(5, 2), F(3))
    assert total_duration(edited) == before
    with pytest.raises(IntervalAlgebraError):
        slide_edit(clips, 0, F(1))
    with pytest.raises(IntervalAlgebraError):
        slide_edit(clips, 2, F(1))
    with pytest.raises(IntervalAlgebraError):
        slide_edit(clips, 1, F(2))
    with pytest.raises(IntervalAlgebraError):
        slide_edit([_clip(0, 1), _clip(2, 3), _clip(3, 4)], 1, F(1, 2))
    with pytest.raises(IntervalAlgebraError):
        slide_edit([_clip(0, 1), _clip(1, 2), _clip(3, 4)], 1, F(1, 2))
    with pytest.raises(TypeError):
        slide_edit(clips, True, F(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        slide_edit(clips, 1, 1.0)  # type: ignore[arg-type]


def test_speed_ramp_constant_speed() -> None:
    ramp = SpeedRamp(F(0), F(2), F(2), F(2))
    assert ramp.displacement() == 4
    assert ramp.displacement_at(F(1)) == 2
    assert ramp.instant_speed(F(1)) == 2
    inversion = ramp.invert(F(3))
    assert inversion.time == F(3, 2)
    assert inversion.lower <= inversion.time <= inversion.upper
    assert ramp.verify_inverse(F(3)) is True
    zero = ramp.invert(F(0))
    assert zero.time == F(0) and zero.lower == zero.upper == F(0)
    with pytest.raises(IntervalAlgebraError):
        ramp.invert(F(-1))
    with pytest.raises(IntervalAlgebraError):
        ramp.invert(F(5))
    with pytest.raises(IntervalAlgebraError):
        ramp.instant_speed(F(3))
    with pytest.raises(IntervalAlgebraError):
        ramp.displacement_at(F(3))
    with pytest.raises(TypeError):
        ramp.instant_speed(1.0)  # type: ignore[arg-type]


def test_speed_ramp_accelerating_and_decelerating() -> None:
    accelerating = SpeedRamp(F(0), F(2), F(1), F(3))
    assert accelerating.displacement() == 4
    inversion = accelerating.invert(F(2))
    assert 1.2 < float(inversion.time) < 1.3
    assert accelerating.verify_inverse(F(2)) is True
    # Fundamental theorem: d(tau)/dt at t0 equals v0 (linear ramp derivative).
    step = F(1, 1000)
    numeric = (accelerating.displacement_at(step) - accelerating.displacement_at(F(0))) / step
    midpoint_speed = accelerating.instant_speed(step / 2)
    assert abs(float(numeric - midpoint_speed)) < 1e-12
    decelerating = SpeedRamp(F(0), F(2), F(3), F(1))
    assert decelerating.displacement() == 4
    assert decelerating.verify_inverse(F(3)) is True
    assert decelerating.time_at_displacement(F(0)) == F(0)
    assert decelerating.time_at_displacement(F(4)) == F(2)


def test_speed_ramp_validation() -> None:
    with pytest.raises(IntervalAlgebraError):
        SpeedRamp(F(2), F(1), F(1), F(1))
    with pytest.raises(IntervalAlgebraError):
        SpeedRamp(F(0), F(2), F(0), F(1))
    with pytest.raises(IntervalAlgebraError):
        SpeedRamp(F(0), F(2), F(1), F(0))
    with pytest.raises(IntervalAlgebraError):
        SpeedRamp(F(-1), F(2), F(1), F(1))
    with pytest.raises(TypeError):
        SpeedRamp(0.0, F(2), F(1), F(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SpeedRamp(F(0), F(2), F(1), F(1)).invert(1.0)  # type: ignore[arg-type]
    RampInversion(F(1), F(1), F(1), F(1))
    with pytest.raises(IntervalAlgebraError):
        RampInversion(F(-1), F(0), F(0), F(0))
    with pytest.raises(IntervalAlgebraError):
        RampInversion(F(1), F(1), F(2), F(3))
    with pytest.raises(TypeError):
        RampInversion(1.0, F(1), F(1), F(1))  # type: ignore[arg-type]
