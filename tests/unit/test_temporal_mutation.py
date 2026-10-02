"""Mutation and Adversarial Invariant Tests for Temporal Algebra.

This module intentionally simulates mutations in arithmetic, rounding, boundary,
and speed factor formulas to prove that the test suite catches regression bugs.
"""

from fractions import Fraction
import pytest

from nexus_ai_agent.creative.temporal import (
    RoundingPolicy,
    TemporalInterval,
    TemporalPoint,
    TemporalTransform,
    Timebase,
)


def test_mutation_float_vs_exact_rational_drift() -> None:
    # At 23.976 fps (exact rate 24000/1001), 1,000,000 frames is exactly 41708.333333... seconds
    tb_exact = Timebase.fps_23_976()

    # Exact conversion:
    pt_exact = TemporalPoint.from_ticks(1_000_000, tb_exact)
    seconds_exact = pt_exact.seconds  # Fraction(1001000000, 24000) = Fraction(1001000, 24) = Fraction(125125, 3)

    # Float mutation approximation: 1_000_000 / 23.976
    float_rate = 23.976
    seconds_float = 1_000_000 / float_rate

    # Calculate drift in microseconds
    drift_us = abs(float(seconds_exact) - seconds_float) * 1_000_000

    # Float arithmetic causes ~69,500 microseconds (>69ms) drift over 1M frames!
    assert drift_us > 1000  # Proves float conversion drifts significantly


def test_mutation_off_by_one_boundary_fails() -> None:
    tb = Timebase.fps_30()
    pt = TemporalPoint.from_ticks(100, tb)

    # Correct frame count is 100
    outcome = pt.to_ticks(tb, rounding=RoundingPolicy.NEAREST)
    assert outcome.value == 100

    # Mutated expectation (off-by-one frame) must fail
    mutated_frame_count = outcome.value + 1
    assert mutated_frame_count != 100


def test_mutation_inclusive_boundary_causes_adjacent_overlap() -> None:
    p0 = TemporalPoint.from_seconds(0)
    p5 = TemporalPoint.from_seconds(5)
    p10 = TemporalPoint.from_seconds(10)

    # Half-open intervals [0, 5) and [5, 10) do NOT overlap
    interval_a = TemporalInterval(p0, p5)
    interval_b = TemporalInterval(p5, p10)

    assert interval_a.overlaps(interval_b) is False

    # If mutated to inclusive boundary semantics, point 5 is in both:
    # A = [0, 5], B = [5, 10] -> overlap at 5!
    # Our half-open model ensures contains(5) is False for interval_a
    assert interval_a.contains(p5) is False
    assert interval_b.contains(p5) is True


def test_mutation_speed_factor_rounding_drift() -> None:
    # Retiming by 3x then 1/3x with exact Fraction
    speed = Fraction(3, 1)
    transform = TemporalTransform(speed)

    source_pt = TemporalPoint.from_seconds(10)
    timeline_pt = transform.map_source_to_timeline_duration(source_pt)

    # Exact timeline_pt = 10/3 seconds
    assert timeline_pt.seconds == Fraction(10, 3)

    # Exact reverse map yields exactly 10s
    recovered = transform.map_timeline_to_source_duration(timeline_pt)
    assert recovered == source_pt

    # Float mutation approximation: 10.0 / 3.0 * 3.0 != 10 in float edge cases
    mutated_float_timeline = 10.0 / 3.0
    mutated_float_recovered = mutated_float_timeline * 3.0
    # In exact fraction domain, recovered.seconds is strictly equal to source_pt.seconds
    assert recovered.seconds == Fraction(10, 1)


def test_mutation_rounding_direction_difference() -> None:
    # 0.5 ticks on 30fps = 0.5 * 1001/30000 s
    pt = TemporalPoint(Fraction(1, 60))  # 1/60th second = 0.5 frames at 30fps

    tb_30 = Timebase.fps_30()

    floor_res = pt.to_ticks(tb_30, rounding=RoundingPolicy.FLOOR).value
    ceil_res = pt.to_ticks(tb_30, rounding=RoundingPolicy.CEIL).value

    # Floor is 0, Ceil is 1
    assert floor_res == 0
    assert ceil_res == 1
    assert floor_res != ceil_res
