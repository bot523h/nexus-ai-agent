"""Deterministic, Adversarial, and Property Tests for Canonical Temporal Algebra.

Tests cover:
* Standard & non-integer frame rates (23.976, 24, 25, 29.97, 30, 50, 59.94, 60 fps).
* Audio sample rates (44.1 kHz, 48 kHz).
* Large and tiny duration boundaries.
* Zero-length markers vs non-zero media interval invariants.
* Half-open [start, end) interval split/join algebraic laws.
* Speed scaling retiming invariants and reversibility.
* Loss-aware conversion outcome tracking (exact vs lossy remainders).
* Cross-clock video frame <-> audio sample mappings.
"""

from fractions import Fraction
import pytest

from nexus_ai_agent.creative.temporal import (
    ClockRelation,
    ConversionOutcome,
    RoundingPolicy,
    TemporalInterval,
    TemporalPoint,
    TemporalTransform,
    Timebase,
)


def test_timebase_standard_factories() -> None:
    tb_23_976 = Timebase.fps_23_976()
    assert tb_23_976.rate == Fraction(24000, 1001)

    tb_29_97 = Timebase.fps_29_97()
    assert tb_29_97.rate == Fraction(30000, 1001)

    tb_59_94 = Timebase.fps_59_94()
    assert tb_59_94.rate == Fraction(60000, 1001)

    assert Timebase.fps_24().rate == Fraction(24, 1)
    assert Timebase.fps_25().rate == Fraction(25, 1)
    assert Timebase.fps_30().rate == Fraction(30, 1)
    assert Timebase.fps_50().rate == Fraction(50, 1)
    assert Timebase.fps_60().rate == Fraction(60, 1)

    assert Timebase.audio_44100().rate == Fraction(44100, 1)
    assert Timebase.audio_48000().rate == Fraction(48000, 1)


@pytest.mark.parametrize(
    "timebase, fps_name",
    [
        (Timebase.fps_23_976(), "23.976"),
        (Timebase.fps_24(), "24"),
        (Timebase.fps_25(), "25"),
        (Timebase.fps_29_97(), "29.97"),
        (Timebase.fps_30(), "30"),
        (Timebase.fps_50(), "50"),
        (Timebase.fps_59_94(), "59.94"),
        (Timebase.fps_60(), "60"),
    ],
)
def test_exact_frame_boundary_conversions(timebase: Timebase, fps_name: str) -> None:
    # 1000 frames on its exact native rate must convert to/from ticks with zero loss
    for frame_count in (1, 24, 1000, 86400):
        pt = TemporalPoint.from_ticks(frame_count, timebase)
        outcome = pt.to_ticks(timebase, rounding=RoundingPolicy.EXACT)
        assert outcome.value == frame_count
        assert outcome.lossless is True
        assert outcome.remainder_seconds == Fraction(0, 1)


def test_lossy_conversion_tracking() -> None:
    # 1 microsecond converted to 24fps is lossy
    pt = TemporalPoint.from_us(1)
    tb_24 = Timebase.fps_24()

    outcome = pt.to_ticks(tb_24, rounding=RoundingPolicy.NEAREST)
    assert outcome.value == 0
    assert outcome.lossless is False
    assert outcome.remainder_seconds == Fraction(1, 1_000_000)

    # EXACT policy must raise ValueError when loss occurs
    with pytest.raises(ValueError, match="is lossy"):
        pt.to_ticks(tb_24, rounding=RoundingPolicy.EXACT)


def test_large_and_tiny_durations() -> None:
    # Large duration: 1,000,000 seconds (~11.5 days)
    large_pt = TemporalPoint.from_seconds(1_000_000)
    tb_29_97 = Timebase.fps_29_97()
    outcome_large = large_pt.to_ticks(tb_29_97, rounding=RoundingPolicy.NEAREST)
    # Exact frame count = 1_000_000 * 30000 / 1001 = 29_970_029_970 / 1001 = 29970029.97002997 -> 29970030
    assert outcome_large.value == 29970030

    # Tiny duration: 1 nanosecond
    tiny_pt = TemporalPoint.from_ticks(1, Timebase.nanoseconds())
    outcome_tiny = tiny_pt.to_ticks(Timebase.audio_48000(), rounding=RoundingPolicy.FLOOR)
    assert outcome_tiny.value == 0
    assert outcome_tiny.lossless is False


def test_interval_half_open_algebra() -> None:
    p0 = TemporalPoint.from_seconds(0)
    p5 = TemporalPoint.from_seconds(5)
    p10 = TemporalPoint.from_seconds(10)

    interval = TemporalInterval(p0, p10)

    # Invariant: half-open [0, 10) contains 0, 5, 9.999 but NOT 10
    assert interval.contains(p0) is True
    assert interval.contains(p5) is True
    assert interval.contains(p10) is False

    # Split property
    left, right = interval.split(p5)
    assert left.start == p0
    assert left.end == p5
    assert right.start == p5
    assert right.end == p10

    # Adjacent composition invariant: join(split(x)) == x
    joined = left.join(right)
    assert joined == interval


def test_zero_length_interval_policy() -> None:
    p3 = TemporalPoint.from_seconds(3)

    # Allowed by default for point events / markers
    marker = TemporalInterval(p3, p3, allow_zero=True)
    assert marker.contains_point() is True
    assert marker.duration == TemporalPoint.zero()

    # Forbidden when allow_zero=False
    with pytest.raises(ValueError, match="Zero-length interval is prohibited"):
        TemporalInterval(p3, p3, allow_zero=False)


def test_negative_duration_prohibition() -> None:
    p5 = TemporalPoint.from_seconds(5)
    neg_duration = TemporalPoint.from_seconds(-2)

    with pytest.raises(ValueError, match="Negative duration"):
        TemporalInterval.from_start_duration(p5, neg_duration)


def test_negative_start_timestamp_allowed() -> None:
    # Pre-roll or offset at -2s is valid
    neg_start = TemporalPoint.from_seconds(-2)
    pos_end = TemporalPoint.from_seconds(3)

    interval = TemporalInterval(neg_start, pos_end)
    assert interval.duration == TemporalPoint.from_seconds(5)


def test_retiming_speed_transform_invariants() -> None:
    source_dur = TemporalPoint.from_seconds(10)

    # 2x speed -> timeline duration = 5s
    transform_2x = TemporalTransform("2")
    timeline_dur = transform_2x.map_source_to_timeline_duration(source_dur)
    assert timeline_dur == TemporalPoint.from_seconds(5)

    # Reversibility invariant: map_timeline_to_source(map_source_to_timeline(d)) == d
    reconciled_source = transform_2x.map_timeline_to_source_duration(timeline_dur)
    assert reconciled_source == source_dur


def test_clock_relation_video_audio() -> None:
    tb_video = Timebase.fps_24()
    tb_audio = Timebase.audio_48000()

    # Frame 24 (exactly 1.0 sec) -> 48,000 audio samples
    conv = ClockRelation.frame_to_sample(24, tb_video, tb_audio)
    assert conv.value == 48000
    assert conv.lossless is True

    # Audio sample 48000 -> Video frame 24
    conv_rev = ClockRelation.sample_to_frame(48000, tb_audio, tb_video)
    assert conv_rev.value == 24
    assert conv_rev.lossless is True
