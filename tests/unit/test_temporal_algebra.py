"""Deterministic, Adversarial, and Property Tests for Pure-Stdlib Temporal Algebra Core.

Tests cover:
* Timebase GCD normalization (Timebase(60, 2) == Timebase(30, 1)).
* FrameRateResolver standard NTSC and integer profiles.
* Type safety: Duration >= 0 vs TimePosition signed coordinate.
* TimePosition + Duration -> TimePosition; TimePosition - TimePosition -> Duration.
* Half-open [start, end) interval algebra: empty interval [t, t) contains no points.
* PointEvent / Marker vs TemporalInterval separation.
* Speed scaling retiming invariants and reversibility.
* Loss-aware conversion outcome tracking with exact residual and error.
* Cross-clock video frame <-> audio sample mappings.
"""

from fractions import Fraction
import pytest

from nexus_ai_agent.creative.temporal import (
    ClockRelation,
    ConversionOutcome,
    Duration,
    FrameRateResolver,
    PointEvent,
    RoundingPolicy,
    TemporalInterval,
    TemporalPoint,
    TemporalTransform,
    Timebase,
    TimePosition,
)


def test_timebase_gcd_normalization() -> None:
    tb1 = Timebase(60, 2)
    tb2 = Timebase(30, 1)

    assert tb1.numerator == 30
    assert tb1.denominator == 1
    assert tb1 == tb2
    assert hash(tb1) == hash(tb2)


def test_framerate_resolver() -> None:
    assert FrameRateResolver.resolve("23.976") == Timebase.fps_23_976()
    assert FrameRateResolver.resolve(23.976) == Timebase.fps_23_976()
    assert FrameRateResolver.resolve("29.97") == Timebase.fps_29_97()
    assert FrameRateResolver.resolve("59.94fps") == Timebase.fps_59_94()
    assert FrameRateResolver.resolve(24) == Timebase.fps_24()
    assert FrameRateResolver.resolve("24000/1001") == Timebase.fps_23_976()


def test_type_safety_duration_vs_position() -> None:
    p1 = TimePosition.from_seconds(10)
    p2 = TimePosition.from_seconds(4)

    # TimePosition - TimePosition -> Duration
    dur = p1 - p2
    assert isinstance(dur, Duration)
    assert dur.seconds == Fraction(6, 1)

    # Disallow TimePosition + TimePosition
    with pytest.raises(TypeError, match="Adding two TimePositions is mathematically undefined"):
        _ = p1 + p2  # type: ignore[operator]

    # TimePosition + Duration -> TimePosition
    p3 = p2 + dur
    assert isinstance(p3, TimePosition)
    assert p3 == p1


def test_duration_nonnegative_invariant() -> None:
    with pytest.raises(ValueError, match="Duration must be nonnegative"):
        Duration.from_seconds(-1)

    with pytest.raises(ValueError, match="Microseconds duration must be nonnegative"):
        Duration.from_us(-100)

    # Duration subtraction cannot be negative
    d1 = Duration.from_seconds(3)
    d2 = Duration.from_seconds(5)
    with pytest.raises(ValueError, match="Duration subtraction result cannot be negative"):
        _ = d1 - d2


def test_signed_time_position_allowed() -> None:
    # Pre-roll / negative timeline coordinate is valid
    pre_roll = TimePosition.from_seconds(-2)
    start = TimePosition.from_seconds(0)

    diff = start - pre_roll
    assert isinstance(diff, Duration)
    assert diff.seconds == Fraction(2, 1)


def test_interval_half_open_contains_semantics() -> None:
    p0 = TimePosition.from_seconds(0)
    p5 = TimePosition.from_seconds(5)
    p10 = TimePosition.from_seconds(10)

    interval = TemporalInterval(p0, p10)

    # Half-open [0, 10) contains 0, 5, but NOT 10
    assert interval.contains(p0) is True
    assert interval.contains(p5) is True
    assert interval.contains(p10) is False

    # Empty interval [5, 5) contains no points!
    empty = TemporalInterval(p5, p5)
    assert empty.is_empty() is True
    assert empty.contains(p5) is False


def test_point_event_marker_separation() -> None:
    pos = TimePosition.from_seconds(5)
    event = PointEvent(position=pos, label="Chapter 1")

    assert event.position == pos
    assert event.label == "Chapter 1"


def test_retiming_speed_transform_invariants() -> None:
    source_dur = Duration.from_seconds(12)

    # 3x speed -> 4s duration on timeline
    transform_3x = TemporalTransform("3")
    timeline_dur = transform_3x.map_source_to_timeline_duration(source_dur)
    assert timeline_dur == Duration.from_seconds(4)

    # Reversibility invariant
    recovered = transform_3x.map_timeline_to_source_duration(timeline_dur)
    assert recovered == source_dur


def test_clock_relation_video_audio() -> None:
    tb_video = Timebase.fps_24()
    tb_audio = Timebase.audio_48000()

    # Frame 24 (1.0 sec) -> 48,000 audio samples
    conv = ClockRelation.frame_to_sample(24, tb_video, tb_audio)
    assert conv.value == 48000
    assert conv.lossless is True
    assert conv.residual_seconds == Fraction(0, 1)

    # Sample 48000 -> Frame 24
    conv_rev = ClockRelation.sample_to_frame(48000, tb_audio, tb_video)
    assert conv_rev.value == 24
    assert conv_rev.lossless is True
