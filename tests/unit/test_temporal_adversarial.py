"""Adversarial and Edge Case Test Suite for Temporal Algebra Core & Delivery OTIO.

Tests exact boundaries, near-canonical floats, rejection of malformed/infinite inputs,
negative values, quantization, and OTIO conversion alignment.
"""

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.packs.delivery.models import ExportOtioInput
from nexus_ai_agent.creative.packs.delivery.operations import _export_otio
from nexus_ai_agent.creative.studio.capabilities import OperationContext
from nexus_ai_agent.creative.studio.models import AssetRecord, Project, Timeline, TypedCommand
from nexus_ai_agent.creative.temporal import (
    ClockRelation,
    FrameRateResolver,
    Timebase,
)


def test_frame_rate_resolver_classification() -> None:
    # Canonical Aliases
    assert FrameRateResolver.resolve("23.976") == Timebase(24000, 1001)
    assert FrameRateResolver.resolve("29.97") == Timebase(30000, 1001)
    assert FrameRateResolver.resolve("59.94") == Timebase(60000, 1001)
    assert FrameRateResolver.resolve(23.976) == Timebase(24000, 1001)

    # Exact Rationals
    assert FrameRateResolver.resolve("24000/1001") == Timebase(24000, 1001)
    assert FrameRateResolver.resolve(Fraction(30000, 1001)) == Timebase(30000, 1001)

    # Near-canonical values must NOT collapse into standard profiles
    near_23 = FrameRateResolver.resolve(23.9760001)
    assert near_23 != Timebase(24000, 1001)
    near_59 = FrameRateResolver.resolve("59.9400001")
    assert near_59 != Timebase(60000, 1001)
    near_24 = FrameRateResolver.resolve("24.0000001")
    assert near_24 != Timebase(24, 1)


def test_frame_rate_resolver_invalid_rejections() -> None:
    # NaN, Inf, -Inf
    for bad_float in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="positive and finite"):
            FrameRateResolver.resolve(bad_float)

    # Malformed ratios
    for bad_ratio in ("24/", "24/1/2", "a/b", "24/0", "-24/1", "0/24"):
        with pytest.raises((ValueError, TypeError)):
            FrameRateResolver.resolve(bad_ratio)

    # Negative & zero values
    with pytest.raises(ValueError):
        FrameRateResolver.resolve(-24)
    with pytest.raises(ValueError):
        FrameRateResolver.resolve(0)

    # Boolean
    with pytest.raises(TypeError, match="Boolean"):
        FrameRateResolver.resolve(True)


def test_clock_relation_audio_video_conversions() -> None:
    # 24fps -> 48kHz (1 frame @ 24fps = 2000 samples @ 48kHz)
    c1 = ClockRelation.frame_to_sample(1, Timebase.fps_24(), Timebase.audio_48000())
    assert c1.value == 2000
    assert c1.lossless is True

    # 25fps -> 44.1kHz (1 frame @ 25fps = 1764 samples @ 44.1kHz)
    c2 = ClockRelation.frame_to_sample(1, Timebase.fps_25(), Timebase.audio_44100())
    assert c2.value == 1764
    assert c2.lossless is True

    # 29.97fps (30000/1001) -> 48kHz
    # 1000 frames = 1001000/30000 sec = 1001/3 sec * 48000 = 1601600 samples
    c3 = ClockRelation.frame_to_sample(1000, Timebase.fps_29_97(), Timebase.audio_48000())
    assert c3.value == 1601600
    assert c3.lossless is True

    # 23.976fps (24000/1001) -> 48kHz (1000 frames = 1001000/24000 sec * 48000 = 2002000 samples)
    c4 = ClockRelation.frame_to_sample(1000, Timebase.fps_23_976(), Timebase.audio_48000())
    assert c4.value == 2002000
    assert c4.lossless is True


def test_otio_export_no_float_bypass_regression() -> None:
    proj = Project(
        project_id="test_p1",
        name="Test Project",
        timeline=Timeline(timeline_id="main", duration_us=1_000_000),
        assets=[
            AssetRecord(
                asset_id="v1",
                media_kind="video",
                content_sha256="sha256:1234",
                duration_us=1_000_000,
            )
        ],
    )
    cmd = TypedCommand(command_id="cmd_1", operation="delivery.export_otio")
    ctx = OperationContext(
        command=cmd,
        input_data=ExportOtioInput(frame_rate=23.976).model_dump(mode="json"),
        history=(),
    )

    outcome = _export_otio(proj, ctx)
    otio_json = outcome.output["otio_json"]
    assert "24000" in otio_json
    assert "1001" in otio_json
