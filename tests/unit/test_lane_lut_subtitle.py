"""Golden pins for the session-3 lane twins: ``lut`` and ``subtitle``.

Pure tests — the compiler never runs a process, so every assertion here is a
byte-exact statement about the argv a real encode would receive.  The real
encodes (``lut3d`` grading, libass Persian burn-in) are proven separately in
``tests/unit/test_assembly_render_proof.py`` — these goldens pin the
*shape* of the emitted stages so a refactor cannot silently swap ``lut3d``
for an approximation or drop ``fontsdir``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.rendering import (
    LaneIR,
    LaneSource,
    LutOp,
    SubtitleOp,
    compile_lane,
)
from nexus_ai_agent.creative.rendering.ir import LaneError

MAIN = LaneSource("main", "/media/main.mp4", "video", 10_000_000)
AUDIO_MAIN = LaneSource("main", "/media/main.m4a", "audio", 10_000_000)


def _compiled(*ops: object, main: LaneSource = MAIN, **kwargs: object) -> str:
    compiled = compile_lane(LaneIR(main=main, ops=ops), **kwargs)  # type: ignore[arg-type]
    return compiled.filtergraph


# ---------------------------------------------------------------------------
# LUT emission
# ---------------------------------------------------------------------------


def test_lut_full_intensity_is_direct_lut3d() -> None:
    graph = _compiled(LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=1.0))
    assert "[vbase]lut3d=file='/stage/warm.cube'[v1]" in graph
    assert "blend=" not in graph  # no pointless split/blend at 1.0


def test_lut_partial_intensity_splits_and_blends() -> None:
    graph = _compiled(LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=0.5))
    assert "split[" in graph
    assert "lut3d=file='/stage/warm.cube'" in graph
    assert "blend=all_mode='normal':all_opacity=0.500000" in graph


def test_lut_zero_intensity_still_honest() -> None:
    """Intensity 0.0 blends 0% graded — pixels provably untouched, stage visible."""
    graph = _compiled(LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=0.0))
    assert "all_opacity=0.000000" in graph


def test_lut_intensity_bounds_are_refused() -> None:
    with pytest.raises(Exception, match="intensity"):
        LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=1.5)
    with pytest.raises(Exception, match="intensity"):
        LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=-0.1)


def test_lut_needs_a_video_main_asset() -> None:
    """A LUT on an audio-only lane is a plan bug — fail closed, name the op."""
    with pytest.raises(LaneError, match="lut op needs a video main asset"):
        _compiled(
            LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=1.0),
            main=AUDIO_MAIN,
        )


def test_lut_path_with_quote_is_escaped() -> None:
    graph = _compiled(LutOp(lut_name="x", lut_path="/st'age/x.cube", intensity=1.0))
    # The session-2 fontfile escaper doubles backslashes and backslash-escapes
    # the quote; the golden pins that a quote cannot break out of the filter arg.
    assert r"lut3d=file='/st\'age/x.cube'" in graph
    assert "'age" not in graph.replace(r"\'age", "")


# ---------------------------------------------------------------------------
# subtitle emission
# ---------------------------------------------------------------------------


def test_subtitle_stage_carries_filename_only_by_default() -> None:
    graph = _compiled(SubtitleOp(subtitle_path="/stage/cap.srt"))
    assert "[vbase]subtitles=filename='/stage/cap.srt'[v1]" in graph


def test_subtitle_stage_threads_fontsdir() -> None:
    graph = _compiled(SubtitleOp(subtitle_path="/stage/cap.srt"), fontsdir="/assets/fonts")
    assert "subtitles=filename='/stage/cap.srt':fontsdir='/assets/fonts'" in graph


def test_subtitle_force_style_is_appended_last() -> None:
    graph = _compiled(
        SubtitleOp(subtitle_path="/stage/cap.srt", force_style="FontName=Vazirmatn"),
        fontsdir="/assets/fonts",
    )
    assert (
        "subtitles=filename='/stage/cap.srt':fontsdir='/assets/fonts'"
        ":force_style='FontName=Vazirmatn'" in graph
    )


def test_subtitle_needs_a_video_main_asset() -> None:
    with pytest.raises(LaneError, match="subtitle op needs a video main asset"):
        _compiled(SubtitleOp(subtitle_path="/stage/cap.srt"), main=AUDIO_MAIN)


# ---------------------------------------------------------------------------
# duration neutrality
# ---------------------------------------------------------------------------


def test_lut_and_subtitle_never_move_the_clock() -> None:
    """Both twins are pixel ops: the compiled duration equals the main duration."""
    compiled = compile_lane(
        LaneIR(
            main=MAIN,
            ops=(
                LutOp(lut_name="warm", lut_path="/stage/warm.cube", intensity=0.5),
                SubtitleOp(subtitle_path="/stage/cap.srt"),
            ),
        )
    )
    assert compiled.duration_us == MAIN.duration_us
    argv = compiled.argv(Path("/stage/out.mp4"))
    index = argv.index("-t")
    assert argv[index + 1] == "10.000000"


def test_ir_hash_distinguishes_twin_payloads() -> None:
    """Different LUT paths / intensities / fontsdirs hash differently (M6 bait)."""
    base = compile_lane(
        LaneIR(main=MAIN, ops=(LutOp(lut_name="a", lut_path="/a.cube", intensity=1.0),))
    )
    other_path = compile_lane(
        LaneIR(main=MAIN, ops=(LutOp(lut_name="a", lut_path="/b.cube", intensity=1.0),))
    )
    other_intensity = compile_lane(
        LaneIR(main=MAIN, ops=(LutOp(lut_name="a", lut_path="/a.cube", intensity=0.5),))
    )
    other_fonts = compile_lane(
        LaneIR(main=MAIN, ops=(SubtitleOp(subtitle_path="/s.srt"),)), fontsdir="/f"
    )
    no_fonts = compile_lane(LaneIR(main=MAIN, ops=(SubtitleOp(subtitle_path="/s.srt"),)))
    hashes = {c.ir_hash for c in (base, other_path, other_intensity, other_fonts, no_fonts)}
    assert len(hashes) == 5
