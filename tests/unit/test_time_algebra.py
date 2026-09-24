"""Integer-microsecond time algebra: no float timestamps cross the lane.

Session 3 pins what the codebase already practices: every duration, offset,
and window is an ``int`` count of microseconds from the Project model down
to the ``-t``/``-ss`` argv strings.  Floats appear in exactly two places —
``SpeedOp.factor`` (a ratio, applied with integer division) and
``LutOp.intensity`` (a blend weight, formatted ``%.6f``) — and neither ever
becomes a timestamp.  These tests fail the suite the moment a float leaks
into a clock.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.rendering import (
    LaneIR,
    LaneSource,
    SpeedOp,
    TrimOp,
    compile_lane,
)
from nexus_ai_agent.creative.rendering.compiler import _seconds
from nexus_ai_agent.creative.studio.models import TimeRangeUS


def test_time_range_rejects_fractional_floats() -> None:
    with pytest.raises(ValidationError):
        TimeRangeUS(start_us=1.5, end_us=2_000_000)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TimeRangeUS(start_us=0, end_us=999.9)  # type: ignore[arg-type]


def test_time_range_rejects_inverted_and_empty_windows() -> None:
    with pytest.raises(ValidationError):
        TimeRangeUS(start_us=5, end_us=5)
    with pytest.raises(ValidationError):
        TimeRangeUS(start_us=6, end_us=5)


def test_seconds_format_round_trips_single_microseconds() -> None:
    assert _seconds(1) == "0.000001"
    assert _seconds(1_000_000) == "1.000000"
    # Every formatted value parses back to the exact integer.
    for micros in (1, 41667, 999_999, 1_000_001, 7_000_000, 3_600_000_000):
        assert int(round(float(_seconds(micros)) * 1_000_000)) == micros


def test_trim_windows_are_integer_us_end_to_end() -> None:
    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=(TrimOp(in_us=1_234_567, out_us=8_765_432),)))
    assert compiled.duration_us == 7_530_865
    assert isinstance(compiled.duration_us, int)
    # The trim travels as exact filter timestamps, the total as -t.
    assert "trim=start=1.234567:end=8.765432" in compiled.filtergraph
    argv = compiled.argv(__import__("pathlib").Path("/o.mp4"))
    assert argv[argv.index("-t") + 1] == "7.530865"


def test_speed_applies_ratio_with_integer_division() -> None:
    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=(SpeedOp(factor=3.0),)))
    assert compiled.duration_us == 3_333_333  # int(10M / 3), never a float
    assert isinstance(compiled.duration_us, int)


def test_one_microsecond_durations_survive_compile() -> None:
    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=(TrimOp(in_us=0, out_us=1),)))
    assert compiled.duration_us == 1
    argv = compiled.argv(__import__("pathlib").Path("/o.mp4"))
    assert argv[argv.index("-t") + 1] == "0.000001"


def test_plan_segments_carry_integers_only() -> None:
    from nexus_ai_agent.creative.rendering.plan import compile_execution_plan
    from nexus_ai_agent.creative.studio.models import (
        Clip,
        MediaRef,
        Timeline,
        Track,
        new_project,
    )

    media = MediaRef(
        asset_id="a", content_sha256="sha256:a", media_kind="video", duration_us=8_000_000
    )
    clip = Clip(
        clip_id="c1",
        media_ref=media,
        source_range=TimeRangeUS(start_us=1_000_001, end_us=5_000_005),
        timeline_range=TimeRangeUS(start_us=0, end_us=4_000_004),
    )
    track = Track(track_id="v", name="V", kind="video", clips=[clip])
    project = new_project(
        "p", "T", Timeline(timeline_id="tl", duration_us=4_000_004, tracks=[track])
    )
    plan = compile_execution_plan(project, track_id="v")  # type: ignore[arg-type]
    segment = plan.segments[0]
    for field in ("source_in_us", "source_out_us", "timeline_start_us", "timeline_end_us"):
        value = getattr(segment, field)
        assert isinstance(value, int) and not isinstance(value, bool), (field, value)
    assert segment.trim_us == 4_000_004


def test_assembly_gap_arithmetic_is_exact() -> None:
    from nexus_ai_agent.creative.rendering.compiler import compile_assembly
    from nexus_ai_agent.creative.rendering.ir import AssemblyGap, LaneAssembly

    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)
    first = LaneIR(main=main, ops=(TrimOp(in_us=0, out_us=1_000_001),))
    second = LaneIR(main=main, ops=(TrimOp(in_us=0, out_us=2_000_002),))
    assembly = LaneAssembly(
        pieces=(first, AssemblyGap(duration_us=500_005), second),
    )
    compiled = compile_assembly(assembly)
    assert compiled.duration_us == 3_500_008
