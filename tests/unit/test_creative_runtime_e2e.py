"""Representative creative end-to-end (task-176, session 2 P2 / §17).

One real creative path, no Telegram UI in the loop::

    Project → CommandBus dispatch → Render Plan → Assembly → Executor
        → Artifact → integrity check

The test is split so the pure legs run everywhere while only the encode leg
needs FFmpeg (skipped as UNVERIFIED without a binary — never PASS):

* Level 1–2 (pure): bus-driven edit (``timeline.split_at_playhead``) then
  ``compile_execution_plan`` over the resulting state;
* Level 3 (pure): ``assemble_execution_plan`` + ``compile_assembly`` — one
  deterministic ``CompiledLane``;
* Level 4 (real media): the unchanged :func:`encode_lane` renders one artifact;
  duration/dimensions/audio/hash are verified from probes, not assumed.

Documented architecture gap (E1 — see ``docs/ops/CREATIVE_RUNTIME.md``): no
registered operation attaches pack effect layers to clips today (only
``slideshow.compose`` attaches its own internal layers), so the single
``color.adjust_exposure`` layer in this scenario is placed by state
construction — exactly what the plan bridge consumes.  The op→effect link is
the precise next bridge; nothing here fakes it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.execution import ExecutionClass, classify_operation
from nexus_ai_agent.creative.packs.availability import Availability
from nexus_ai_agent.creative.packs.runtime import build_pack_runtime
from nexus_ai_agent.creative.rendering.compiler import compile_assembly
from nexus_ai_agent.creative.rendering.executor import encode_lane
from nexus_ai_agent.creative.rendering.plan import (
    assemble_execution_plan,
    compile_execution_plan,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    probe_video,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    EffectLayerRef,
    MediaRef,
    Playhead,
    TargetRef,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)


def _asset(asset_id: str, duration_us: int) -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        media_kind="video",
        content_sha256=f"sha256:{asset_id}",
        duration_us=duration_us,
    )


def _scenario_project():  # type: ignore[no-untyped-def]
    """One 4 s clip of A, a 1 s editorial gap, one 2 s clip of B (graded)."""
    hero = MediaRef(
        asset_id="hero", content_sha256="sha256:hero", media_kind="video", duration_us=4_000_000
    )
    broll = MediaRef(
        asset_id="broll", content_sha256="sha256:broll", media_kind="video", duration_us=4_000_000
    )
    grade = EffectLayerRef(
        operation="color.adjust_exposure",
        parameters={"exposure_ev": 0.5},
        range=TimeRangeUS(start_us=5_000_000, end_us=7_000_000),
    )
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            Clip(
                clip_id="clip_a",
                media_ref=hero,
                source_range=TimeRangeUS(start_us=0, end_us=4_000_000),
                timeline_range=TimeRangeUS(start_us=0, end_us=4_000_000),
            ),
            Clip(
                clip_id="clip_b",
                media_ref=broll,
                source_range=TimeRangeUS(start_us=1_000_000, end_us=3_000_000),
                timeline_range=TimeRangeUS(start_us=5_000_000, end_us=7_000_000),
                effects=[grade],
            ),
        ],
    )
    timeline = Timeline(
        timeline_id="tl",
        duration_us=7_000_000,
        tracks=[track],
        playhead=Playhead(timecode_us=2_000_000, frame_number=60),
    )
    project = new_project("p_e2e", "Runtime E2E", timeline)
    return project.model_copy(
        update={"assets": [_asset("hero", 4_000_000), _asset("broll", 4_000_000)]}
    )


def _runtime_bus(project):  # type: ignore[no-untyped-def]
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    return CommandBus(project, registry=build_runtime_registry())


def test_bus_split_then_plan_is_pure_and_deterministic() -> None:
    bus = _runtime_bus(_scenario_project())
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd-split",
            operation="timeline.split_at_playhead",
            target=TargetRef(track_id="video_01", clip_id="clip_a"),
            input={},
        )
    )
    assert result.output["left"]["clip_id"] == "clip_a"
    assert result.output["source_unchanged"] is True

    project = bus.project
    plan = compile_execution_plan(project, track_id="video_01")
    assert [s.clip_id for s in plan.segments] == [
        "clip_a",
        result.output["right"]["clip_id"],
        "clip_b",
    ]
    assert plan.concat_required is True
    again = compile_execution_plan(project, track_id="video_01")
    assert again.plan_hash == plan.plan_hash


def test_plan_to_compiled_assembly_is_one_deterministic_process() -> None:
    bus = _runtime_bus(_scenario_project())
    bus.dispatch(
        TypedCommand(
            command_id="cmd-split",
            operation="timeline.split_at_playhead",
            target=TargetRef(track_id="video_01", clip_id="clip_a"),
            input={},
        )
    )
    plan = compile_execution_plan(bus.project, track_id="video_01")
    assembly = assemble_execution_plan(
        plan, {"hero": "/stage/a.mp4", "broll": "/stage/b.mp4"}, project=bus.project
    )
    assert len(assembly.pieces) == 4  # A1, A2, gap, B(graded)
    assert len(assembly.gaps) == 1
    compiled = compile_assembly(assembly)
    assert compiled.duration_us == 7_000_000
    assert compiled.filtergraph.count("concat=") == 1
    assert "concat=n=4:v=1:a=1" in compiled.filtergraph
    assert compiled.filtergraph.count("eq=gamma=") == 1
    assert compile_assembly(assembly).ir_hash == compiled.ir_hash


def test_execution_table_marks_the_rendered_effect_executable() -> None:
    record = classify_operation("color.adjust_exposure")
    assert record.execution_class is ExecutionClass.EXECUTABLE
    assert record.currently_executable is True


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        pytest.skip("UNVERIFIED: no FFmpeg binary available")


def _resolve_binary(name: str) -> str | None:
    if name == "ffmpeg":
        try:
            return resolve_ffmpeg_bin()
        except FfmpegUnavailableError:
            return None
    import shutil

    return shutil.which(name)


def _make_clip(path: Path, duration_s: float, binary: str, freq: int) -> None:
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=30:duration={duration_s}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration_s}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_full_path_renders_one_verified_artifact(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src_a = tmp_path / "a.mp4"
    src_b = tmp_path / "b.mp4"
    _make_clip(src_a, 4.0, binary, freq=440)
    _make_clip(src_b, 4.0, binary, freq=880)

    runtime = build_pack_runtime(activate=True)
    by_id = {row.package_id: row for row in runtime.availability(resolve_binary=_resolve_binary)}
    assert by_id["nexus.slideshow.compose"].availability == Availability.AVAILABLE

    bus = _runtime_bus(_scenario_project())
    bus.dispatch(
        TypedCommand(
            command_id="cmd-split",
            operation="timeline.split_at_playhead",
            target=TargetRef(track_id="video_01", clip_id="clip_a"),
            input={},
        )
    )
    plan = compile_execution_plan(bus.project, track_id="video_01")
    assembly = assemble_execution_plan(
        plan, {"hero": str(src_a), "broll": str(src_b)}, project=bus.project
    )
    compiled = compile_assembly(assembly)
    assert compiled.duration_us == 7_000_000

    destination = tmp_path / "e2e.mp4"
    artifact = encode_lane(compiled, destination, binary=binary)
    assert destination.is_file()
    assert abs(artifact.duration_us - 7_000_000) < 300_000, artifact.duration_us
    assert (artifact.width, artifact.height) == (1280, 720)
    assert artifact.has_audio is True
    assert artifact.lane_ir_hash == compiled.ir_hash
    assert artifact.sha256 and artifact.size_bytes > 0
    evidence = artifact.evidence()
    assert evidence["output_sha256"] == artifact.sha256
    assert evidence["duration_us"] == artifact.duration_us
    info = probe_video(destination, binary=binary)
    assert abs(info.duration_us - 7_000_000) < 300_000
