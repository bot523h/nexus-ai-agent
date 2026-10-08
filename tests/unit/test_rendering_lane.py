"""Wave 8 apply lane: golden argv pins (pure) + real FFmpeg encodes (evidence).

The first section never executes anything: the compiler is pure, so every op
pins a byte-exact argv.  The last four tests run a real encode through the
static FFmpeg wheel and assert on probed evidence (measured, not assumed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.rendering import (
    CompiledLane,
    DuckOp,
    FreezeOp,
    LaneIR,
    LaneProfile,
    LaneSource,
    LoudnormOp,
    MeasuredLoudness,
    ReverseOp,
    SpeedOp,
    TitleOp,
    TrimOp,
    XfadeOp,
    compile_lane,
    compile_measure,
    encode_lane,
    lane_ir_from_project,
    render_lane,
)
from nexus_ai_agent.creative.rendering.executor import LaneExecutionError
from nexus_ai_agent.creative.rendering.ir import LaneError
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.studio.models import AssetRecord, Project, Timeline

MAIN = LaneSource("main", "/media/main.mp4", "video", 10_000_000)
OTHER = LaneSource("other", "/media/other.mp4", "video", 6_000_000)
VOICE = LaneSource("voice", "/media/voice.m4a", "audio", 8_000_000)
FONT = "/fonts/Vazirmatn.ttf"
MEASURED = MeasuredLoudness.model_validate(
    {
        "input_i": -20.5,
        "input_tp": -2.1,
        "input_lra": 7.3,
        "input_thresh": -31.2,
        "target_offset": 6.5,
    }
)


def _lane(*ops: object, main: LaneSource = MAIN) -> LaneIR:
    extras = [s for s in (OTHER, VOICE) if s.asset_id != main.asset_id]
    return LaneIR(main=main, ops=ops, extra_sources=tuple(extras))  # type: ignore[arg-type]


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available")


# ---------------------------------------------------------------------------
# golden argv pins (pure — no process)
# ---------------------------------------------------------------------------


def test_trim_golden_argv_is_byte_exact() -> None:
    compiled = compile_lane(_lane(TrimOp(in_us=1_000_000, out_us=5_000_000)))
    assert compiled.duration_us == 4_000_000
    argv = compiled.argv(Path("/stage/out.mp4"), binary="ffmpeg")
    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        "/media/main.mp4",
        "-i",
        "/media/other.mp4",
        "-i",
        "/media/voice.m4a",
        "-filter_complex",
        compiled.filtergraph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-t",
        "4.000000",
        "-y",
        "/stage/out.mp4",
    ]
    assert "trim=start=1.000000:end=5.000000,setpts=PTS-STARTPTS" in compiled.filtergraph
    assert "atrim=start=1.000000:end=5.000000,asetpts=PTS-STARTPTS" in compiled.filtergraph


def test_speed_golden_video_and_audio() -> None:
    compiled = compile_lane(_lane(SpeedOp(factor=2.0)))
    assert compiled.duration_us == 5_000_000
    assert "setpts=PTS/2.000000" in compiled.filtergraph
    assert "atempo=2.000000" in compiled.filtergraph


def test_speed_atempo_chain_decomposes_out_of_range_factors() -> None:
    fast = compile_lane(_lane(SpeedOp(factor=4.0)))
    assert "atempo=2.000000,atempo=2.000000" in fast.filtergraph
    assert fast.duration_us == 2_500_000
    slow = compile_lane(_lane(SpeedOp(factor=0.25)))
    assert "atempo=0.500000,atempo=0.500000" in slow.filtergraph
    assert slow.duration_us == 40_000_000


def test_reverse_golden() -> None:
    compiled = compile_lane(_lane(ReverseOp()))
    assert compiled.duration_us == 10_000_000
    assert "[vbase]reverse[v1]" in compiled.filtergraph
    assert "[abase]areverse[a1]" in compiled.filtergraph


def test_freeze_golden_holds_frame_30_for_two_seconds() -> None:
    compiled = compile_lane(_lane(FreezeOp(at_us=1_000_000, hold_us=2_000_000)))
    assert compiled.duration_us == 2_000_000
    assert "select='eq(n\\,30)',setpts=N/FRAME_RATE/TB" in compiled.filtergraph
    assert "tpad=stop_mode=clone:stop_duration=2.000000" in compiled.filtergraph


def test_xfade_golden_two_inputs_and_duration_math() -> None:
    compiled = compile_lane(
        _lane(XfadeOp(other_asset_id="other", offset_us=8_000_000, duration_us=1_000_000))
    )
    assert compiled.duration_us == 15_000_000  # 10 + 6 - 1
    assert compiled.inputs == ("/media/main.mp4", "/media/other.mp4", "/media/voice.m4a")
    assert "xfade=transition=fade:duration=1.000000:offset=8.000000" in compiled.filtergraph
    assert "apad=whole_dur=15.000000" in compiled.filtergraph


def test_xfade_rejects_overlap_beyond_either_clip() -> None:
    with pytest.raises(LaneError, match="exceeds main duration"):
        compile_lane(
            _lane(XfadeOp(other_asset_id="other", offset_us=9_500_000, duration_us=1_000_000))
        )
    with pytest.raises(LaneError, match="exceeds other clip duration"):
        compile_lane(
            _lane(XfadeOp(other_asset_id="other", offset_us=1_000_000, duration_us=7_000_000))
        )


def test_title_golden_escapes_filtergraph_metacharacters() -> None:
    compiled = compile_lane(
        _lane(
            TitleOp(
                text="50%: it's live, now: go",
                font_size=48,
                start_us=1_000_000,
                end_us=3_000_000,
            )
        ),
        fontfile=FONT,
    )
    assert "text='50\\%\\: it\\'s live\\, now\\: go'" in compiled.filtergraph
    assert "fontfile='/fonts/Vazirmatn.ttf'" in compiled.filtergraph
    assert "fontsize=48" in compiled.filtergraph
    assert "enable='between(t,1.000000,3.000000)'" in compiled.filtergraph


def test_title_requires_an_explicit_fontfile() -> None:
    with pytest.raises(LaneError, match="explicit fontfile"):
        compile_lane(_lane(TitleOp(text="hi")))


def test_loudnorm_is_fail_closed_without_measured_values() -> None:
    with pytest.raises(LaneError, match="run measure_loudness first"):
        compile_lane(_lane(LoudnormOp()))
    with pytest.raises(LaneError, match="at least one loudnorm op"):
        compile_measure(_lane(TrimOp(in_us=0, out_us=1_000_000)))


def test_loudnorm_apply_golden_uses_linear_two_pass_filter() -> None:
    compiled = compile_lane(_lane(LoudnormOp(target_lufs=-16.0)), measured=MEASURED)
    assert (
        "loudnorm=linear=true:I=-16.00:TP=-1.00:LRA=11.00:"
        "measured_I=-20.50:measured_TP=-2.10:measured_LRA=7.30:"
        "measured_thresh=-31.20:offset=6.50" in compiled.filtergraph
    )
    measure = compile_measure(_lane(LoudnormOp(target_lufs=-16.0)))
    assert "loudnorm=I=-16.00:TP=-1.00:LRA=11.00:print_format=json" in measure.filtergraph
    assert measure.measure_argv(binary="ffmpeg")[-3:] == ["-f", "null", "-"]


def test_duck_golden_sidechain_and_audible_voice() -> None:
    compiled = compile_lane(_lane(DuckOp(voice_asset_id="voice")))
    # -12 dB -> ratio 10^(12/20) = 3.98; voice input index 2 (sorted extras).
    assert "[2:a]asplit[vsc1][vmix1]" in compiled.filtergraph
    assert (
        "sidechaincompress=threshold=0.02:ratio=3.98:attack=100:release=400" in compiled.filtergraph
    )
    assert "amix=inputs=2:duration=longest:dropout_transition=0" in compiled.filtergraph


def test_compile_is_deterministic_and_hash_sensitive() -> None:
    first = compile_lane(_lane(TrimOp(in_us=0, out_us=2_000_000), SpeedOp(factor=1.5)))
    second = compile_lane(_lane(TrimOp(in_us=0, out_us=2_000_000), SpeedOp(factor=1.5)))
    assert first.argv(Path("/o.mp4")) == second.argv(Path("/o.mp4"))
    assert first.ir_hash == second.ir_hash
    changed = compile_lane(_lane(TrimOp(in_us=0, out_us=2_000_000), SpeedOp(factor=2.0)))
    assert changed.ir_hash != first.ir_hash


def test_bridge_from_project_is_fail_closed() -> None:
    project = Project(
        project_id="p1",
        name="demo",
        timeline=Timeline(timeline_id="t1", duration_us=10_000_000),
        assets=[
            AssetRecord(
                asset_id="main",
                media_kind="video",
                content_sha256="sha256:abc",
                duration_us=10_000_000,
            )
        ],
    )
    with pytest.raises(LaneError, match="unknown main asset"):
        lane_ir_from_project(project, {"main": "/m.mp4"}, "ghost", [])
    with pytest.raises(LaneError, match="no staged media path"):
        lane_ir_from_project(project, {}, "main", [])
    with pytest.raises(LaneError, match="unknown asset"):
        lane_ir_from_project(
            project,
            {"main": "/m.mp4"},
            "main",
            [XfadeOp(other_asset_id="ghost", offset_us=0, duration_us=100_000)],
        )
    lane = lane_ir_from_project(
        project, {"main": "/m.mp4"}, "main", [TrimOp(in_us=0, out_us=1_000_000)]
    )
    assert lane.main.duration_us == 10_000_000


def test_composed_trim_speed_title_golden() -> None:
    compiled: CompiledLane = compile_lane(
        _lane(
            TrimOp(in_us=2_000_000, out_us=8_000_000),
            SpeedOp(factor=2.0),
            TitleOp(text="cut", start_us=0, end_us=1_000_000),
        ),
        fontfile=FONT,
    )
    assert compiled.duration_us == 3_000_000  # (8-2)s at 2x
    graph = compiled.filtergraph
    assert graph.index("trim=start=2.000000") < graph.index("setpts=PTS/2.000000")
    assert graph.index("setpts=PTS/2.000000") < graph.index("drawtext=")
    assert "atempo=2.000000" in graph


def test_encode_refuses_to_replace_an_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"a master someone else made")
    compiled = compile_lane(_lane(TrimOp(in_us=0, out_us=1_000_000)))
    with pytest.raises(LaneExecutionError, match="already exists"):
        encode_lane(compiled, destination, binary="/nonexistent/ffmpeg")
    assert destination.read_bytes() == b"a master someone else made"


# ---------------------------------------------------------------------------
# real encodes (static FFmpeg wheel; probed evidence)
# ---------------------------------------------------------------------------


def _make_clip(path: Path, duration_s: float, binary: str, freq: int = 440) -> None:
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


def test_real_encode_trim_and_speed(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 4.0, binary)
    lane = LaneIR(
        main=LaneSource("main", str(src), "video", 4_000_000),
        ops=(TrimOp(in_us=1_000_000, out_us=3_000_000), SpeedOp(factor=2.0)),
    )
    artifact = render_lane(lane, tmp_path / "out.mp4", binary=binary)
    assert abs(artifact.duration_us - 1_000_000) < 200_000
    assert (artifact.width, artifact.height) == (1280, 720)
    assert artifact.has_audio is True
    assert artifact.ops == ("trim", "speed")
    assert Path(artifact.path).stat().st_size > 10_000
    assert list(tmp_path.glob(".out.part*")) == []


def test_real_encode_reverse_and_freeze(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 3.0, binary)
    lane = LaneIR(
        main=LaneSource("main", str(src), "video", 3_000_000),
        ops=(ReverseOp(), FreezeOp(at_us=1_000_000, hold_us=1_000_000)),
        profile=LaneProfile(width=640, height=480),
    )
    artifact = render_lane(lane, tmp_path / "out.mp4", binary=binary)
    assert abs(artifact.duration_us - 1_000_000) < 200_000
    assert (artifact.width, artifact.height) == (640, 480)
    assert artifact.sha256.startswith("sha256:")


def test_real_encode_xfade_two_clips(tmp_path: Path) -> None:
    binary = _ffmpeg()
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _make_clip(first, 2.0, binary, freq=440)
    _make_clip(second, 2.0, binary, freq=880)
    lane = LaneIR(
        main=LaneSource("a", str(first), "video", 2_000_000),
        ops=(XfadeOp(other_asset_id="b", offset_us=1_500_000, duration_us=500_000),),
        extra_sources=(LaneSource("b", str(second), "video", 2_000_000),),
        profile=LaneProfile(width=640, height=480),
    )
    artifact = render_lane(lane, tmp_path / "out.mp4", binary=binary)
    assert abs(artifact.duration_us - 3_500_000) < 250_000
    assert artifact.has_audio is True


def test_real_encode_loudnorm_two_pass(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 2.0, binary)
    lane = LaneIR(
        main=LaneSource("main", str(src), "video", 2_000_000),
        ops=(LoudnormOp(target_lufs=-16.0),),
        profile=LaneProfile(width=640, height=480),
    )
    artifact = render_lane(lane, tmp_path / "out.mp4", binary=binary)
    assert artifact.has_audio is True
    assert artifact.ops == ("loudnorm",)
    assert abs(artifact.duration_us - 2_000_000) < 250_000
