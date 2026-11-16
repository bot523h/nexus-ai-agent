"""Filter-graph compiler tests: valid graphs, refusals and injection defence."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.hyper_compiler import ffmpeg_filter_graph_compiler as fgc
from nexus_ai_agent.creative.hyper_compiler.errors import InvalidFilterGraphError
from nexus_ai_agent.creative.hyper_compiler.ffmpeg_filter_graph_compiler import (
    ColorGradeEqOp,
    CompiledFilterGraph,
    CrossfadeAudioOp,
    CrossfadeVideoOp,
    FFmpegFilterGraphCompiler,
    FilmGrainOp,
    FilterOp,
    GraphInput,
    LoudnessNormOp,
    LUT3DOp,
    ScalePadOp,
    StreamKind,
    TrimOp,
    TrimStartOp,
    VignetteOp,
)


def video_input(label: str = "v0") -> GraphInput:
    return GraphInput(label=label, kind=StreamKind.VIDEO)


def audio_input(label: str = "a0") -> GraphInput:
    return GraphInput(label=label, kind=StreamKind.AUDIO)


# ── valid compilation ───────────────────────────────────────────────────────
def test_compile_video_chain_emits_topological_graph() -> None:
    graph = FFmpegFilterGraphCompiler().compile(
        [video_input()],
        [
            TrimOp("v0", "v1", 0, 48, 24.0),
            ScalePadOp("v1", "v2", 1920, 1080),
            ColorGradeEqOp("v2", "v3", 0.05, 1.1, 1.0, 1.0),
            LUT3DOp("v3", "v4", "luts/film.cube"),
            VignetteOp("v4", "v5", 0.5),
            FilmGrainOp("v5", "vout", 8.0),
        ],
        ["vout"],
    )
    assert graph.output_pads == ("vout",)
    assert graph.input_pads == ("v0",)
    assert graph.filter_complex.startswith("[v0]trim=")
    assert graph.filter_complex.endswith("[vout]")
    assert ";[v1]scale=" in graph.filter_complex


def test_compile_audio_chain() -> None:
    graph = FFmpegFilterGraphCompiler().compile(
        [audio_input("a0"), audio_input("a1")],
        [
            CrossfadeAudioOp("a0", "a1", "amix", 1.5),
            LoudnessNormOp("amix", "aout"),
        ],
        ["aout"],
    )
    assert "[a0][a1]acrossfade=" in graph.filter_complex
    assert "[amix]loudnorm=" in graph.filter_complex


def test_compile_video_crossfade_two_inputs() -> None:
    graph = FFmpegFilterGraphCompiler().compile(
        [video_input("v0"), video_input("v1")],
        [CrossfadeVideoOp("v0", "v1", "vout", 1.0, 2.0, "fade")],
        ["vout"],
    )
    assert graph.filter_complex == "[v0][v1]xfade=transition=fade:duration=1:offset=2[vout]"


def test_ffmpeg_argv_is_a_list_and_maps_outputs() -> None:
    graph = FFmpegFilterGraphCompiler().compile(
        [video_input()], [FilmGrainOp("v0", "vout", 1.0)], ["vout"]
    )
    argv = graph.ffmpeg_argv(["-i", "in.mp4"], ["out.mp4"])
    assert isinstance(argv, list)
    assert argv[0] == "ffmpeg"
    assert "-filter_complex" in argv
    assert argv[argv.index("-filter_complex") + 1] == graph.filter_complex
    assert ["-map", "[vout]"] == argv[argv.index("-map") : argv.index("-map") + 2]
    assert argv[-1] == "out.mp4"


def test_ffmpeg_argv_custom_binary() -> None:
    graph = CompiledFilterGraph("", ("vout",), ())
    argv = graph.ffmpeg_argv([], [], ffmpeg_bin="/usr/bin/ffmpeg")
    assert argv[0] == "/usr/bin/ffmpeg"


# ── numeric validation ──────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_num_rejects_non_finite(bad: float) -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._num(bad)


def test_num_renders_negative_zero_as_zero() -> None:
    assert fgc._num(-0.0) == "0"


def test_num_trims_trailing_zeros() -> None:
    assert fgc._num(1.5) == "1.5"
    assert fgc._num(2.0) == "2"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_check_range_rejects_non_finite(value: float) -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_range(value, 0.0, 1.0, what="x")


def test_check_range_rejects_out_of_band() -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_range(2.0, 0.0, 1.0, what="x")


def test_check_int_rejects_bool() -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_int(True, 0, 10, what="x")


def test_check_int_rejects_out_of_band() -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_int(11, 0, 10, what="x")


def test_as_float_narrows_optional() -> None:
    assert fgc._as_float(-3.0) == -3.0


# ── label / path validation (injection defence) ─────────────────────────────
@pytest.mark.parametrize(
    "label",
    ["1bad", "", "bad-label", "has space", "[v0]", "a;b", "a\nb", "x" * 100],
)
def test_check_label_rejects_unsafe(label: str) -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_label(label, what="pad")


def test_check_label_accepts_safe() -> None:
    assert fgc._check_label("v0", what="pad") == "v0"
    assert fgc._check_label("out_pad9", what="pad") == "out_pad9"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "a;rm -rf /",
        "a`b`",
        "a$(b)",
        "a|b",
        "a&b",
        "a'b",
        'a"b',
        "a\nb",
        "a b",
        "a\\b",
        "../etc/passwd",
        "luts/../../secret",
        "luts/film cube.cube",
    ],
)
def test_check_path_rejects_unsafe(path: str) -> None:
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_path(path, what="lut3d")


def test_check_path_rejects_non_ascii() -> None:
    # Not a shell metacharacter, but outside the allow-list alphabet.
    with pytest.raises(InvalidFilterGraphError):
        fgc._check_path("luts/café.cube", what="lut3d")


def test_check_path_accepts_relative() -> None:
    assert fgc._check_path("luts/film-2024.cube", what="lut3d") == "luts/film-2024.cube"


def test_lut_op_rejects_injection_path() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LUT3DOp("v0", "v1", "x;rm -rf /")


def test_lut_op_rejects_unknown_interp() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LUT3DOp("v0", "v1", "luts/a.cube", "bicubic")


def test_graph_input_rejects_unsafe_path() -> None:
    with pytest.raises(InvalidFilterGraphError):
        GraphInput(label="v0", kind=StreamKind.VIDEO, path="a;b")


def test_graph_input_rejects_unsafe_label() -> None:
    with pytest.raises(InvalidFilterGraphError):
        GraphInput(label="bad label", kind=StreamKind.VIDEO)


# ── per-op validation ───────────────────────────────────────────────────────
def test_trim_op_rejects_bad_range() -> None:
    with pytest.raises(InvalidFilterGraphError):
        TrimOp("v0", "v1", 10, 10, 24.0)


def test_trim_op_rejects_non_finite_fps() -> None:
    with pytest.raises(InvalidFilterGraphError):
        TrimOp("v0", "v1", 0, 10, float("nan"))


def test_trim_op_rejects_negative_start() -> None:
    with pytest.raises(InvalidFilterGraphError):
        TrimOp("v0", "v1", -1, 10, 24.0)


def test_trim_start_op_renders_open_ended_trim() -> None:
    op = TrimStartOp("v0", "v1", 3)
    assert op.render() == "trim=start_frame=3,setpts=PTS-STARTPTS"
    assert op.input_pads() == ("v0",)
    assert op.output_pads() == ("v1",)
    assert op.kind is StreamKind.VIDEO


def test_trim_start_op_rejects_zero_start() -> None:
    # A zero start would be a no-op; the caller must not build one.
    with pytest.raises(InvalidFilterGraphError):
        TrimStartOp("v0", "v1", 0)


def test_trim_start_op_rejects_unsafe_label() -> None:
    with pytest.raises(InvalidFilterGraphError):
        TrimStartOp("bad label", "v1", 3)


def test_scale_pad_op_rejects_zero_size() -> None:
    with pytest.raises(InvalidFilterGraphError):
        ScalePadOp("v0", "v1", 0, 1080)


def test_color_grade_rejects_out_of_range() -> None:
    with pytest.raises(InvalidFilterGraphError):
        ColorGradeEqOp("v0", "v1", 2.0, 1.0, 1.0, 1.0)


def test_vignette_rejects_too_large_angle() -> None:
    with pytest.raises(InvalidFilterGraphError):
        VignetteOp("v0", "v1", math.pi)


def test_film_grain_rejects_negative_strength() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FilmGrainOp("v0", "v1", -1.0)


def test_crossfade_video_rejects_unknown_transition() -> None:
    with pytest.raises(InvalidFilterGraphError):
        CrossfadeVideoOp("v0", "v1", "vout", 1.0, 0.0, "spiral")


def test_crossfade_video_rejects_zero_duration() -> None:
    with pytest.raises(InvalidFilterGraphError):
        CrossfadeVideoOp("v0", "v1", "vout", 0.0, 0.0)


def test_crossfade_audio_rejects_unknown_curve() -> None:
    with pytest.raises(InvalidFilterGraphError):
        CrossfadeAudioOp("a0", "a1", "aout", 1.0, curve1="weird")


def test_loudness_rejects_out_of_range() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LoudnessNormOp("a0", "aout", integrated=0.0)


def test_ops_declare_expected_pads_and_kinds() -> None:
    assert TrimOp("v0", "v1", 0, 1, 24.0).kind is StreamKind.VIDEO
    assert CrossfadeAudioOp("a0", "a1", "ao", 1.0).kind is StreamKind.AUDIO
    assert CrossfadeVideoOp("v0", "v1", "vo", 1.0, 0.0).input_pads() == ("v0", "v1")


def test_base_filter_op_methods_raise() -> None:
    base = FilterOp()
    with pytest.raises(NotImplementedError):
        base.input_pads()
    with pytest.raises(NotImplementedError):
        base.output_pads()
    with pytest.raises(NotImplementedError):
        base.render()


# ── loudnorm measured_* / linear mode (P2) ──────────────────────────────────
def test_loudnorm_dynamic_render() -> None:
    op = LoudnessNormOp("a0", "aout")
    assert op.render() == "loudnorm=I=-16:TP=-1.5:LRA=11"


def test_loudnorm_linear_render_emits_measurements() -> None:
    op = LoudnessNormOp(
        "a0",
        "aout",
        measured_i=-20.5,
        measured_tp=-3.0,
        measured_lra=6.0,
        measured_thresh=-30.0,
        linear=True,
    )
    rendered = op.render()
    assert "measured_I=-20.5" in rendered
    assert "measured_TP=-3" in rendered
    assert "measured_LRA=6" in rendered
    assert "measured_thresh=-30" in rendered
    assert "linear=true" in rendered


def test_loudnorm_measurements_without_linear_render_false() -> None:
    op = LoudnessNormOp(
        "a0",
        "aout",
        measured_i=-20.0,
        measured_tp=-2.0,
        measured_lra=5.0,
        measured_thresh=-30.0,
    )
    assert "linear=false" in op.render()


def test_loudnorm_rejects_partial_measurements() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LoudnessNormOp("a0", "aout", measured_i=-20.0)


def test_loudnorm_rejects_linear_without_measurements() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LoudnessNormOp("a0", "aout", linear=True)


def test_loudnorm_rejects_out_of_range_measurements() -> None:
    with pytest.raises(InvalidFilterGraphError):
        LoudnessNormOp(
            "a0",
            "aout",
            measured_i=-100.0,
            measured_tp=-2.0,
            measured_lra=5.0,
            measured_thresh=-30.0,
        )


# ── compile-time graph refusals ─────────────────────────────────────────────
def test_compile_rejects_unsafe_output_label() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile([video_input()], [], ["bad label"])


def test_compile_rejects_duplicate_outputs() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile([video_input()], [], ["v0", "v0"])


def test_compile_rejects_duplicate_input_pads() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile([video_input(), video_input()], [], [])


def test_compile_rejects_unknown_input_pad() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0")], [FilmGrainOp("vX", "vout", 1.0)], ["vout"]
        )


def test_compile_rejects_reused_input_pad_in_op() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0")],
            [CrossfadeVideoOp("v0", "v0", "vout", 1.0, 0.0)],
            ["vout"],
        )


def test_compile_rejects_mixed_kind_inputs() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0"), audio_input("a0")],
            [CrossfadeAudioOp("v0", "a0", "aout", 1.0)],
            ["aout"],
        )


def test_compile_rejects_wrong_kind_for_op() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [audio_input("a0")], [FilmGrainOp("a0", "vout", 1.0)], ["vout"]
        )


def test_compile_rejects_duplicate_produced_pad() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0"), video_input("v1")],
            [FilmGrainOp("v0", "vout", 1.0), FilmGrainOp("v1", "vout", 1.0)],
            ["vout"],
        )


def test_compile_rejects_dangling_pad() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0"), video_input("v1")],
            [FilmGrainOp("v0", "vout", 1.0)],
            ["vout"],
        )


def test_compile_rejects_unbound_output_pad() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile([], [], ["vout"])


def test_compile_rejects_already_consumed_pad() -> None:
    with pytest.raises(InvalidFilterGraphError):
        FFmpegFilterGraphCompiler().compile(
            [video_input("v0")],
            [FilmGrainOp("v0", "v1", 1.0), FilmGrainOp("v0", "v2", 1.0)],
            ["v1", "v2"],
        )
