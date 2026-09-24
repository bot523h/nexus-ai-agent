"""Multi-segment assembly: plan → LaneAssembly → one compiled process (task-176).

Session 1 proved the LaneIR limitation (one main source per IR, so a K-clip
track yields ``concat_required=True``).  This suite proves the closure:

* :func:`assemble_execution_plan` turns segments + editorial gaps into one
  timeline-ordered :class:`LaneAssembly` (pure, deterministic);
* :func:`compile_assembly` compiles it into ONE filtergraph with ONE ``concat``
  stage — executed by the unchanged :func:`encode_lane` in ONE process;
* the A / gap / B acceptance scenario renders real media (or skips as
  UNVERIFIED when no FFmpeg binary exists — a skip is not a pass).

Duration math is asserted exactly (pure integer microseconds); probed media
durations use the repository's real-encode tolerance (container rounding).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.rendering.compiler import compile_assembly, compile_lane
from nexus_ai_agent.creative.rendering.executor import encode_lane
from nexus_ai_agent.creative.rendering.ir import (
    AssemblyGap,
    ExposureOp,
    LaneAssembly,
    LaneError,
    LaneIR,
    LaneProfile,
    LaneSource,
    LoudnormOp,
    SpeedOp,
    TrimOp,
    lane_ir_from_project,
)
from nexus_ai_agent.creative.rendering.plan import (
    PlanError,
    assemble_execution_plan,
    compile_execution_plan,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    probe_video,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    EffectLayerRef,
    MediaRef,
    Timeline,
    TimeRangeUS,
    Track,
    new_project,
)


def _media(asset_id: str, duration_us: int) -> MediaRef:
    return MediaRef(
        asset_id=asset_id,
        content_sha256=f"sha256:{asset_id}",
        media_kind="video",
        duration_us=duration_us,
    )


def _asset(asset_id: str, duration_us: int) -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        media_kind="video",
        content_sha256=f"sha256:{asset_id}",
        duration_us=duration_us,
    )


def _clip(
    clip_id: str,
    media: MediaRef,
    source: tuple[int, int],
    timeline: tuple[int, int],
    effects: list[EffectLayerRef] | None = None,
) -> Clip:
    return Clip(
        clip_id=clip_id,
        media_ref=media,
        source_range=TimeRangeUS(start_us=source[0], end_us=source[1]),
        timeline_range=TimeRangeUS(start_us=timeline[0], end_us=timeline[1]),
        effects=effects or [],
    )


def _gap_scenario() -> tuple[object, dict[str, str]]:
    """Segment A [0,2s), gap [2s,3s), segment B [3s,5s) on one track."""
    hero = _media("hero", 8_000_000)
    broll = _media("broll", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("clip_a", hero, (0, 2_000_000), (0, 2_000_000)),
            _clip("clip_b", broll, (1_000_000, 3_000_000), (3_000_000, 5_000_000)),
        ],
    )
    timeline = Timeline(timeline_id="tl", duration_us=5_000_000, tracks=[track])
    project = new_project("p", "Assembly", timeline)
    project = project.model_copy(update={"assets": [_asset("hero", 8_000_000), _asset("broll", 8_000_000)]})
    return project, {"hero": "/stage/hero.mp4", "broll": "/stage/broll.mp4"}


def test_assemble_builds_ordered_pieces_with_an_exact_gap() -> None:
    project, media = _gap_scenario()
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assert plan.concat_required is True
    assembly = assemble_execution_plan(plan, media, project=project)
    assert len(assembly.pieces) == 3
    first, gap, second = assembly.pieces
    assert isinstance(first, LaneIR) and isinstance(second, LaneIR)
    assert isinstance(gap, AssemblyGap)
    assert gap.duration_us == 1_000_000
    assert first.main.asset_id == "hero"
    assert second.main.asset_id == "broll"
    assert assembly.segments == (first, second)
    assert assembly.gaps == (gap,)


def test_adjacent_segments_produce_no_gap_piece() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("clip_a", hero, (0, 2_000_000), (0, 2_000_000)),
            _clip("clip_b", hero, (2_000_000, 4_000_000), (2_000_000, 4_000_000)),
        ],
    )
    timeline = Timeline(timeline_id="tl", duration_us=4_000_000, tracks=[track])
    project = new_project("p", "Adjacent", timeline)
    project = project.model_copy(update={"assets": [_asset("hero", 8_000_000)]})
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assembly = assemble_execution_plan(plan, {"hero": "/stage/hero.mp4"}, project=project)
    assert len(assembly.pieces) == 2
    assert assembly.gaps == ()


def test_assemble_rejects_empty_and_overlapping_plans() -> None:
    timeline = Timeline(
        timeline_id="tl",
        duration_us=0,
        tracks=[Track(track_id="video_01", name="V", kind="video", clips=[])],
    )
    project = new_project("p", "Empty", timeline)
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="no segments"):
        assemble_execution_plan(plan, {}, project=project)

    hero = _media("hero", 8_000_000)
    overlapping = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("clip_a", hero, (0, 3_000_000), (0, 3_000_000)),
            _clip("clip_b", hero, (0, 3_000_000), (2_000_000, 5_000_000)),
        ],
    )
    timeline2 = Timeline(timeline_id="tl", duration_us=5_000_000, tracks=[overlapping])
    project2 = new_project("p", "Overlap", timeline2)
    with pytest.raises(PlanError, match="overlap"):
        compile_execution_plan(project2, track_id="video_01")  # type: ignore[arg-type]


def test_assembly_duration_algebra_is_exact() -> None:
    project, media = _gap_scenario()
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assembly = assemble_execution_plan(plan, media, project=project)
    compiled = compile_assembly(assembly)
    # A trims to 2s, gap is 1s, B trims to 2s → exactly 5s.
    assert compiled.duration_us == 5_000_000
    assert compiled.ir_hash.startswith("sha256:")
    again = compile_assembly(assembly)
    assert again.ir_hash == compiled.ir_hash
    assert again.filtergraph == compiled.filtergraph


def test_compiled_assembly_is_one_concat_in_timeline_order() -> None:
    project, media = _gap_scenario()
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assembly = assemble_execution_plan(plan, media, project=project)
    compiled = compile_assembly(assembly)
    assert compiled.inputs == ("/stage/hero.mp4", "/stage/broll.mp4")
    assert compiled.filtergraph.count("concat=") == 1
    assert "concat=n=3:v=1:a=1" in compiled.filtergraph
    assert compiled.video_out == "vout"
    assert compiled.audio_out == "aout"
    argv = compiled.argv(Path("/stage/out.mp4"), binary="ffmpeg")
    assert argv.count("-filter_complex") == 1
    assert argv.count("-i") == 2
    assert argv[argv.index("-t") + 1] == "5.000000"
    assert argv[-2:] == ["-y", "/stage/out.mp4"]


def test_per_piece_trim_and_effects_survive_the_assembly() -> None:
    hero = _media("hero", 8_000_000)
    graded = EffectLayerRef(
        operation="color.adjust_exposure",
        parameters={"exposure_ev": 1.0},
        range=TimeRangeUS(start_us=0, end_us=2_000_000),
    )
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("clip_a", hero, (1_000_000, 3_000_000), (0, 2_000_000), [graded]),
            _clip("clip_b", hero, (0, 2_000_000), (2_000_000, 4_000_000)),
        ],
    )
    timeline = Timeline(timeline_id="tl", duration_us=4_000_000, tracks=[track])
    project = new_project("p", "Graded", timeline)
    project = project.model_copy(update={"assets": [_asset("hero", 8_000_000)]})
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assembly = assemble_execution_plan(plan, {"hero": "/stage/hero.mp4"}, project=project)
    compiled = compile_assembly(assembly)
    # Both trims travel; the exposure stage lands on the first piece only
    # (after its trim: [s0_v1] is the post-trim label of piece 0).
    assert "trim=start=1.000000:end=3.000000" in compiled.filtergraph
    assert "trim=start=0.000000:end=2.000000" in compiled.filtergraph
    assert compiled.filtergraph.count("eq=gamma=2.000000") == 1
    assert "[s0_v1]eq=gamma=2.000000" in compiled.filtergraph


def test_single_segment_assembly_matches_single_lane_duration() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("clip_a", hero, (1_000_000, 3_000_000), (0, 2_000_000))],
    )
    timeline = Timeline(timeline_id="tl", duration_us=2_000_000, tracks=[track])
    project = new_project("p", "Single", timeline)
    project = project.model_copy(update={"assets": [_asset("hero", 8_000_000)]})
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assert plan.concat_required is False
    assembly = assemble_execution_plan(plan, {"hero": "/stage/hero.mp4"}, project=project)
    assert len(assembly.pieces) == 1
    from nexus_ai_agent.creative.rendering.plan import segment_lane_ir

    lane = segment_lane_ir(
        plan, plan.segments[0], {"hero": "/stage/hero.mp4"}, project=project
    )
    assert compile_assembly(assembly).duration_us == compile_lane(lane).duration_us == 2_000_000


def test_assembly_is_fail_closed() -> None:
    profile = LaneProfile()
    video = LaneSource("v", "/stage/v.mp4", "video", 2_000_000)
    audio = LaneSource("a", "/stage/a.mp3", "audio", 2_000_000)
    video_ir = LaneIR(main=video, ops=(TrimOp(in_us=0, out_us=2_000_000),), profile=profile)
    audio_ir = LaneIR(main=audio, ops=(TrimOp(in_us=0, out_us=2_000_000),), profile=profile)

    with pytest.raises(LaneError, match="at least one lane segment"):
        compile_assembly(LaneAssembly(pieces=(), profile=profile))
    with pytest.raises(LaneError, match="at least one lane segment"):
        compile_assembly(LaneAssembly(pieces=(AssemblyGap(duration_us=1),), profile=profile))
    with pytest.raises(LaneError, match="adjacent gaps"):
        compile_assembly(
            LaneAssembly(
                pieces=(video_ir, AssemblyGap(duration_us=1), AssemblyGap(duration_us=1)),
                profile=profile,
            )
        )
    with pytest.raises(LaneError, match="positive"):
        compile_assembly(
            LaneAssembly(pieces=(video_ir, AssemblyGap(duration_us=0)), profile=profile)
        )
    with pytest.raises(LaneError, match="mixes segment media kinds"):
        compile_assembly(LaneAssembly(pieces=(video_ir, audio_ir), profile=profile))
    other_profile = LaneProfile(width=640, height=480)
    drifted = LaneIR(
        main=video, ops=(TrimOp(in_us=0, out_us=2_000_000),), profile=other_profile
    )
    with pytest.raises(LaneError, match="share the assembly profile"):
        compile_assembly(LaneAssembly(pieces=(video_ir, drifted), profile=profile))
    loud = LaneIR(main=video, ops=(LoudnormOp(),), profile=profile)
    with pytest.raises(LaneError, match="measure pass"):
        compile_assembly(LaneAssembly(pieces=(loud,), profile=profile))
    graded_audio = LaneIR(
        main=audio, ops=(ExposureOp(exposure_ev=1.0),), profile=profile
    )
    with pytest.raises(LaneError, match="video main asset"):
        compile_assembly(LaneAssembly(pieces=(graded_audio,), profile=profile))


def test_audio_only_assembly_has_no_video_chain() -> None:
    profile = LaneProfile()
    first = LaneSource("a1", "/stage/a1.mp3", "audio", 2_000_000)
    second = LaneSource("a2", "/stage/a2.mp3", "audio", 2_000_000)
    assembly = LaneAssembly(
        pieces=(
            LaneIR(main=first, ops=(TrimOp(in_us=0, out_us=2_000_000),), profile=profile),
            AssemblyGap(duration_us=1_000_000),
            LaneIR(main=second, ops=(SpeedOp(factor=2.0),), profile=profile),
        ),
        profile=profile,
    )
    compiled = compile_assembly(assembly)
    assert compiled.video_out is None
    assert "concat=n=3:v=0:a=1" in compiled.filtergraph
    assert compiled.duration_us == 4_000_000
    argv = compiled.argv(Path("/stage/out.mp4"), binary="ffmpeg")
    assert "-vn" in argv


def test_speed_effects_scale_piece_durations_in_the_total() -> None:
    profile = LaneProfile()
    main = LaneSource("v", "/stage/v.mp4", "video", 4_000_000)
    assembly = LaneAssembly(
        pieces=(
            LaneIR(
                main=main,
                ops=(TrimOp(in_us=0, out_us=4_000_000), SpeedOp(factor=2.0)),
                profile=profile,
            ),
        ),
        profile=profile,
    )
    assert compile_assembly(assembly).duration_us == 2_000_000


# ---------------------------------------------------------------------------
# real media: A / gap / B acceptance (UNVERIFIED without a binary — never PASS)
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        pytest.skip("UNVERIFIED: no FFmpeg binary available")


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
    assert path.is_file()


def test_real_media_gap_acceptance_renders_one_artifact(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src_a = tmp_path / "a.mp4"
    src_b = tmp_path / "b.mp4"
    _make_clip(src_a, 4.0, binary, freq=440)
    _make_clip(src_b, 4.0, binary, freq=880)

    hero = _media("hero", 4_000_000)
    broll = _media("broll", 4_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("clip_a", hero, (0, 2_000_000), (0, 2_000_000)),
            _clip("clip_b", broll, (1_000_000, 3_000_000), (3_000_000, 5_000_000)),
        ],
    )
    timeline = Timeline(timeline_id="tl", duration_us=5_000_000, tracks=[track])
    project = new_project("p", "Acceptance", timeline)
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="hero",
                    media_kind="video",
                    content_sha256="sha256:hero",
                    duration_us=4_000_000,
                ),
                AssetRecord(
                    asset_id="broll",
                    media_kind="video",
                    content_sha256="sha256:broll",
                    duration_us=4_000_000,
                ),
            ]
        }
    )
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assembly = assemble_execution_plan(
        plan, {"hero": str(src_a), "broll": str(src_b)}, project=project
    )
    compiled = compile_assembly(assembly)
    assert compiled.duration_us == 5_000_000

    destination = tmp_path / "acceptance.mp4"
    artifact = encode_lane(compiled, destination, binary=binary)
    assert destination.is_file()
    # No silent segment loss: a dropped 2 s segment would miss by 2 s, far
    # outside the container-rounding tolerance.
    assert abs(artifact.duration_us - 5_000_000) < 300_000, artifact.duration_us
    assert artifact.width == 1280
    assert artifact.height == 720
    assert artifact.has_audio is True
    assert artifact.sha256 and artifact.size_bytes > 0
    info = probe_video(destination, binary=binary)
    assert info.has_audio is True
    assert abs(info.duration_us - 5_000_000) < 300_000


def test_real_media_single_segment_assembly_renders(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 4.0, binary, freq=440)
    profile = LaneProfile()
    assembly = LaneAssembly(
        pieces=(
            LaneIR(
                main=LaneSource("v", str(src), "video", 4_000_000),
                ops=(TrimOp(in_us=1_000_000, out_us=3_000_000),),
                profile=profile,
            ),
        ),
        profile=profile,
    )
    compiled = compile_assembly(assembly)
    assert compiled.duration_us == 2_000_000
    artifact = encode_lane(compiled, tmp_path / "single.mp4", binary=binary)
    assert abs(artifact.duration_us - 2_000_000) < 300_000, artifact.duration_us
