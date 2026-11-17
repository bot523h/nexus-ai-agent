"""Tests for exact rational time (Fraction time-base representation)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.composition import (
    RATE_23_976,
    RATE_29_97,
    RATE_30,
    RATE_59_94,
    RATE_48000,
    RATE_96000,
    RateMismatchError,
    RationalTime,
    RationalTimeError,
    TimeRange,
    common_time_base,
    frames_per_second,
)

FPS24_BASE = Fraction(1, 24)
FPS25_BASE = Fraction(1, 25)
NTSC_BASE = Fraction(1001, 24000)


def test_ntsc_time_base_is_exact() -> None:
    t = RationalTime.at_fps(100_000, RATE_23_976)
    # 100000 frames at 24000/1001 fps = 100000*1001/24000 s exactly
    assert t.seconds == Fraction(100_000 * 1001, 24000)
    assert t.seconds != Fraction(100_000, 24)


def test_no_drift_over_100000_frames_29_97() -> None:
    t = RationalTime.at_fps(100_000, RATE_29_97)
    assert t.seconds == Fraction(100_000 * 1001, 30000)


def test_frames_per_second_round_trip() -> None:
    assert frames_per_second(Fraction(1, 24)) == Fraction(24, 1)
    assert frames_per_second(NTSC_BASE) == RATE_23_976


def test_exact_conversion_23_976_to_60() -> None:
    # 24000 frames at 23.976 fps == 1001 s exactly; at 60 fps that is 60060 frames
    t = RationalTime(24000, NTSC_BASE)
    assert t.seconds == 1001
    assert t.rescale(Fraction(1, 60)).value == 60060


def test_mixed_rate_addition_is_exact() -> None:
    a = RationalTime(24, FPS24_BASE)  # 1 s
    b = RationalTime(25, FPS25_BASE)  # 1 s
    s = a + b
    assert s.seconds == 2
    assert s.time_base == common_time_base(FPS24_BASE, FPS25_BASE)


def test_subtraction() -> None:
    a = RationalTime(90, Fraction(1, 30))
    b = RationalTime(30, Fraction(1, 30))
    assert (a - b).value == 60


def test_rescale_refuses_fractional_tick() -> None:
    with pytest.raises(RateMismatchError):
        RationalTime(1, FPS25_BASE).rescale(FPS24_BASE)


def test_from_seconds_requires_exact_landing() -> None:
    # 1001 s is exactly 24000 ticks at the 23.976 time base
    assert RationalTime.from_seconds(Fraction(1001), NTSC_BASE).value == 24000
    # 1 s does not land on a whole 23.976 frame
    with pytest.raises(RateMismatchError):
        RationalTime.from_seconds(Fraction(1), NTSC_BASE)


def test_invalid_inputs() -> None:
    with pytest.raises(RationalTimeError):
        RationalTime(1, Fraction(0))
    with pytest.raises(RationalTimeError):
        RationalTime(1.5, FPS24_BASE)  # type: ignore[arg-type]
    with pytest.raises(RationalTimeError):
        RationalTime.at_fps(1, Fraction(-1))


def test_comparisons() -> None:
    assert RationalTime(1, FPS24_BASE) < RationalTime(2, FPS24_BASE)
    assert RationalTime(24, FPS24_BASE) <= RationalTime(1, Fraction(1, 1))
    assert RationalTime(30, FPS24_BASE) > RationalTime(1, Fraction(1, 1))
    assert RationalTime(48, FPS24_BASE) >= RationalTime(2, Fraction(1, 1))


def test_common_time_base_and_errors() -> None:
    assert common_time_base(FPS24_BASE, FPS25_BASE) == Fraction(1, 600)
    with pytest.raises(RationalTimeError):
        common_time_base(Fraction(0), FPS24_BASE)


def test_rate_property() -> None:
    assert RationalTime(1, FPS24_BASE).rate == Fraction(24, 1)


def test_time_range_overlap_and_end() -> None:
    r1 = TimeRange(RationalTime(0, FPS24_BASE), RationalTime(24, FPS24_BASE))
    r2 = TimeRange(RationalTime(12, FPS24_BASE), RationalTime(24, FPS24_BASE))
    ov = r1.overlap(r2)
    assert ov is not None
    assert ov.duration.seconds == Fraction(1, 2)
    assert r1.end == RationalTime(24, FPS24_BASE)


def test_time_range_disjoint() -> None:
    r1 = TimeRange(RationalTime(0, FPS24_BASE), RationalTime(12, FPS24_BASE))
    r2 = TimeRange(RationalTime(24, FPS24_BASE), RationalTime(12, FPS24_BASE))
    assert r1.overlap(r2) is None


def test_time_range_union_touching_and_gap() -> None:
    r1 = TimeRange(RationalTime(0, FPS24_BASE), RationalTime(12, FPS24_BASE))
    r2 = TimeRange(RationalTime(12, FPS24_BASE), RationalTime(12, FPS24_BASE))
    assert r1.union(r2).duration.seconds == 1
    r3 = TimeRange(RationalTime(48, FPS24_BASE), RationalTime(12, FPS24_BASE))
    with pytest.raises(RationalTimeError):
        r1.union(r3)


def test_time_range_contains_and_clamp() -> None:
    bounds = TimeRange(RationalTime(0, FPS24_BASE), RationalTime(24, FPS24_BASE))
    inner = TimeRange(RationalTime(6, FPS24_BASE), RationalTime(6, FPS24_BASE))
    assert bounds.contains(RationalTime(12, FPS24_BASE))
    assert bounds.contains(RationalTime(24, FPS24_BASE)) is False
    assert inner.clamp(bounds) == inner
    outside = TimeRange(RationalTime(48, FPS24_BASE), RationalTime(6, FPS24_BASE))
    assert outside.clamp(bounds) is None


def test_time_range_negative_duration_refused() -> None:
    with pytest.raises(RationalTimeError):
        TimeRange(RationalTime(10, FPS24_BASE), RationalTime(-1, FPS24_BASE))


def test_split_on_frame() -> None:
    r = TimeRange(RationalTime(0, FPS24_BASE), RationalTime(4, FPS24_BASE))
    ticks = r.split_on_frame(1)
    assert len(ticks) == 4
    with pytest.raises(RationalTimeError):
        r.split_on_frame(0)


def test_canonical_rate_constants() -> None:
    assert RATE_59_94 == Fraction(60000, 1001)
    assert RATE_48000 == 48000
    assert RATE_96000 == 96000
    assert RATE_30 == 30
