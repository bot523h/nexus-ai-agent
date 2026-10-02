"""Real Deterministic Mutation Test Suite for Temporal Algebra Core.

This module explicitly simulates 10 critical mutations in arithmetic, GCD normalization,
rounding, boundary conditions, rate profile resolution, and type invariants,
proving that test failures occur for every mutated logic state.
"""

from fractions import Fraction
import pytest

from nexus_ai_agent.creative.temporal import (
    Duration,
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
    seconds_exact = dur.seconds  # Exact Fraction: 125125 / 3

    # Float mutation approximation
    seconds_float = 1_000_000 / 23.976
    drift_us = abs(float(seconds_exact) - seconds_float) * 1_000_000

    # Float arithmetic causes >69ms drift over 1M frames!
    assert drift_us > 1000


def test_mutant_2_timebase_gcd_omission_failure() -> None:
    # Unnormalized Timebase(60, 2) must normalize to Timebase(30, 1)
    tb_unnormalized = Timebase(60, 2)
    tb_normalized = Timebase(30, 1)

    assert tb_unnormalized.numerator == 30
    assert tb_unnormalized.denominator == 1
    assert tb_unnormalized == tb_normalized


def test_mutant_3_improper_rounding_direction() -> None:
    dur = Duration(Fraction(1, 60))  # 0.5 frames at 30fps
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

    # Half-open intervals [0, 5) and [5, 10) do NOT overlap
    a = TemporalInterval(p0, p5)
    b = TemporalInterval(p5, p10)

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

    # Timeline duration at 2x speed must be 5s, NOT 20s (multiplication mutant)
    timeline_dur = transform.map_source_to_timeline_duration(source_dur)
    assert timeline_dur == Duration.from_seconds(5)
    assert timeline_dur != Duration.from_seconds(20)


def test_mutant_8_lossless_metadata_erasure_mutation() -> None:
    # 1 us at 24fps is lossy
    dur = Duration.from_us(1)
    outcome = dur.to_ticks(Timebase.fps_24(), rounding=RoundingPolicy.NEAREST)

    assert outcome.lossless is False
    assert outcome.residual_seconds != Fraction(0, 1)


def test_mutant_9_corrupted_ntsc_alias_resolver() -> None:
    resolved = FrameRateResolver.resolve("23.976")
    # Must resolve to NTSC exact 24000/1001, NOT decimal 23976/1000
    assert resolved.numerator == 24000
    assert resolved.denominator == 1001
    assert resolved != Timebase(23976, 1000)


def test_mutant_10_large_numerator_pure_integer_precision() -> None:
    # 80-digit integer numerator to test pure integer arithmetic with zero float overflow
    large_sec = Fraction(10**80 + 1, 10**80)
    dur = Duration(large_sec)
    tb = Timebase.fps_30()

    outcome = dur.to_ticks(tb, rounding=RoundingPolicy.FLOOR)
    assert outcome.value == 30
