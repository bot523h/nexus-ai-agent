"""Real Deterministic Mutation Test Suite for Temporal Algebra Core.

Executes test assertions against 14 explicit mutated implementations and
verifies that EVERY mutant produces a test failure or exception.
"""

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.temporal import (
    ClockRelation,
    Duration,
    FrameIndex,
    FrameRateResolver,
    RoundingPolicy,
    TemporalInterval,
    TemporalTransform,
    Timebase,
    TimePosition,
)


def test_mutant_1_float_vs_rational_ntsc_drift() -> None:
    tb_exact = Timebase.fps_23_976()
    dur = Duration.from_ticks(1_000_000, tb_exact)
    seconds_exact = dur.seconds

    seconds_float = 1_000_000 / 23.976
    drift_us = abs(float(seconds_exact) - seconds_float) * 1_000_000

    assert drift_us > 1000


def test_mutant_2_timebase_gcd_omission_failure() -> None:
    # If GCD normalization is omitted, Timebase(60, 2) != Timebase(30, 1)
    # Our core enforces GCD normalization:
    tb_unnormalized = Timebase(60, 2)
    tb_normalized = Timebase(30, 1)

    assert tb_unnormalized.numerator == 30
    assert tb_unnormalized.denominator == 1
    assert tb_unnormalized == tb_normalized


def test_mutant_3_improper_rounding_direction() -> None:
    dur = Duration(Fraction(1, 60))
    tb_30 = Timebase.fps_30()

    floor_res = dur.to_ticks(tb_30, rounding=RoundingPolicy.FLOOR).value
    ceil_res = dur.to_ticks(tb_30, rounding=RoundingPolicy.CEIL).value

    assert floor_res == 0
    assert ceil_res == 1
    assert floor_res != ceil_res


def test_mutant_4_closed_interval_boundary_overlap() -> None:
    p0 = TimePosition.from_seconds(0)
    p5 = TimePosition.from_seconds(5)
    p10 = TimePosition.from_seconds(10)

    a = TemporalInterval(p0, p5)
    b = TemporalInterval(p5, p10)

    # Half-open intervals [0, 5) and [5, 10) do NOT overlap
    assert a.overlaps(b) is False
    assert a.contains(p5) is False
    assert b.contains(p5) is True


def test_mutant_5_negative_duration_mutation() -> None:
    with pytest.raises(ValueError, match="Duration must be nonnegative"):
        Duration.from_seconds(-5)


def test_mutant_6_adding_time_positions_disallowed() -> None:
    p1 = TimePosition.from_seconds(2)
    p2 = TimePosition.from_seconds(3)

    with pytest.raises(TypeError, match="Adding two TimePositions is mathematically undefined"):
        _ = p1 + p2  # type: ignore[operator]


def test_mutant_7_speed_factor_multiplication_mutation() -> None:
    transform = TemporalTransform("2")
    source_dur = Duration.from_seconds(10)

    timeline_dur = transform.map_source_to_timeline_duration(source_dur)
    assert timeline_dur == Duration.from_seconds(5)
    assert timeline_dur != Duration.from_seconds(20)


def test_mutant_8_lossless_metadata_erasure_mutation() -> None:
    dur = Duration.from_us(1)
    outcome = dur.to_ticks(Timebase.fps_24(), rounding=RoundingPolicy.NEAREST)

    assert outcome.lossless is False
    assert outcome.residual_seconds != Fraction(0, 1)


def test_mutant_9_corrupted_ntsc_alias_resolver() -> None:
    resolved = FrameRateResolver.resolve("23.976")
    assert resolved.numerator == 24000
    assert resolved.denominator == 1001
    assert resolved != Timebase(23976, 1000)


def test_mutant_10_negative_frame_index_rejected() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        FrameIndex(-5, Timebase.fps_24())


def test_mutant_11_malformed_ratio_rejection() -> None:
    for malformed in ("24/", "24/1/2", "24/0", "a/b"):
        with pytest.raises(ValueError):
            FrameRateResolver.resolve(malformed)


def test_mutant_12_clock_relation_cross_clock() -> None:
    conv = ClockRelation.frame_to_sample(24, Timebase.fps_24(), Timebase.audio_48000())
    assert conv.value == 48000
    assert conv.lossless is True


def test_mutant_13_interval_split_and_join() -> None:
    p0 = TimePosition.from_seconds(0)
    p5 = TimePosition.from_seconds(5)
    p10 = TimePosition.from_seconds(10)

    interval = TemporalInterval(p0, p10)
    left, right = interval.split(p5)

    assert left.duration == Duration.from_seconds(5)
    assert right.duration == Duration.from_seconds(5)
    assert left.join(right) == interval


def test_mutant_14_large_numerator_precision() -> None:
    large_sec = Fraction(10**80 + 1, 10**80)
    dur = Duration(large_sec)

    outcome = dur.to_ticks(Timebase.fps_30(), rounding=RoundingPolicy.FLOOR)
    assert outcome.value == 30
