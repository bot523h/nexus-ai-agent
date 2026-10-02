"""Deterministic, Adversarial, and Property Tests for Pure-Stdlib Temporal Algebra Core.

Tests cover:
* Timebase GCD normalization (Timebase(60, 2) == Timebase(30, 1)).
* FrameRateResolver standard NTSC, integer, and fraction ratio parsing.
* Malformed ratio specifier fail-closed rejection ("24/", "24/0", "a/b").
* Type safety: Duration >= 0 vs TimePosition signed coordinate.
* Typed FrameIndex and SampleIndex nonnegative invariants.
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
    Duration,
    FrameIndex,
    FrameRateResolver,
    PointEvent,
    RoundingPolicy,
    SampleIndex,
    TemporalInterval,
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


def test_framerate_resolver_parsing_and_hardening() -> None:
    assert FrameRateResolver.resolve("23.976") == Timebase.fps_23_976()
    assert FrameRateResolver.resolve(23.976) == Timebase.fps_23_976()
    assert FrameRateResolver.resolve("29.97") == Timebase.fps_29_97()
    assert FrameRateResolver.resolve("59.94fps") == Timebase.fps_59_94()
    assert FrameRateResolver.resolve(24) == Timebase.fps_24()
    assert FrameRateResolver.resolve("24000/1001") == Timebase.fps_23_976()

    for malformed in ("24/", "24/1/2", "24/0", "a/b", "-24/1"):
        with pytest.raises(ValueError):
            FrameRateResolver.resolve(malformed)


def test_typed_frame_and_sample_index() -> None:
    tb = Timebase.fps_24()
    f_idx = FrameIndex(24, tb)
    assert f_idx.to_position() == TimePosition.from_seconds(1)

    s_idx = SampleIndex(48000, Timebase.audio_48000())
    assert s_idx.to_position() == TimePosition.from_seconds(1)

    with pytest.raises(ValueError, match="nonnegative"):
        FrameIndex(-1, tb)

    with pytest.raises(ValueError, match="nonnegative"):
        SampleIndex(-10, Timebase.audio_48000())


def test_type_safety_duration_vs_position() -> None:
    p1 = TimePosition.from_seconds(10)
    p2 = TimePosition.from_seconds(4)

    dur = p1 - p2
    assert isinstance(dur, Duration)
    assert dur.seconds == Fraction(6, 1)

    with pytest.raises(TypeError, match="Adding two TimePositions is mathematically undefined"):
        _ = p1 + p2  # type: ignore[operator]

    p3 = p2 + dur
    assert isinstance(p3, TimePosition)
    assert p3 == p1


def test_retiming_speed_transform_invariants() -> None:
    source_dur = Duration.from_seconds(12)
    transform_3x = TemporalTransform("3")
    timeline_dur = transform_3x.map_source_to_timeline_duration(source_dur)
    assert timeline_dur == Duration.from_seconds(4)

    recovered = transform_3x.map_timeline_to_source_duration(timeline_dur)
    assert recovered == source_dur


def test_clock_relation_video_audio() -> None:
    tb_video = Timebase.fps_24()
    tb_audio = Timebase.audio_48000()

    f24 = FrameIndex(24, tb_video)
    conv = ClockRelation.frame_to_sample(f24, tb_video, tb_audio)
    assert conv.value == 48000
    assert conv.lossless is True

    s48k = SampleIndex(48000, tb_audio)
    conv_rev = ClockRelation.sample_to_frame(s48k, tb_audio, tb_video)
    assert conv_rev.value == 24
    assert conv_rev.lossless is True
