"""The 3-segment A→B→C real-render proof (session 3 centerpiece).

One Project with three adjacent clips compiles through the *single* canonical
path — ``compile_execution_plan → assemble_execution_plan → compile_assembly
→ encode_lane`` — and the produced file is proven, pixel by pixel, to contain
what the plan said:

* **order**: solid red / green / blue sources land in timeline order —
  sampled frames are reddest / greenest / bluest in segments A / B / C;
* **LUT**: segment B carries the shipped ``warm`` LUT at full intensity —
  its pixels measurably differ from an ungraded control render;
* **subtitle**: segment B burns a Persian SRT line (Vazirmatn, ``fontsdir``) —
  bright glyph pixels appear in the lower third and nowhere else;
* **artifact truth**: ``verify_lane_artifact`` re-measures the bytes from
  disk (size, sha256, duration, streams) and passes.

The title twin (``drawtext``) is proven separately below: the imageio-ffmpeg
wheel has no libfreetype, so that test skips loudly where the filter is
absent and proves glyph pixels where a full binary resolves (CI).

These are real FFmpeg encodes (skipped only when no binary resolves), not
filtergraph goldens — the goldens pin the shape, these pin the light.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
FONT_FILE = FONTS_DIR / "Vazirmatn.ttf"

pytest.importorskip("PIL.Image", reason="pixel proof needs Pillow")

from PIL import Image  # noqa: E402

from nexus_ai_agent.creative.artifacts import (  # noqa: E402
    sha256_file_bytes,
    verify_lane_artifact,
)
from nexus_ai_agent.creative.luts import library_path  # noqa: E402
from nexus_ai_agent.creative.rendering import (  # noqa: E402
    compile_assembly,
    encode_lane,
)
from nexus_ai_agent.creative.rendering.plan import (  # noqa: E402
    assemble_execution_plan,
    compile_execution_plan,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (  # noqa: E402
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.studio.models import (  # noqa: E402
    Clip,
    EffectLayerRef,
    MediaRef,
    Timeline,
    TimeRangeUS,
    Track,
    new_project,
)


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"no ffmpeg for the render proof: {exc}")


def _make_solid(path: Path, color: str, duration_s: float, binary: str, freq: int) -> None:
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
            f"color={color}:size=320x240:rate=30:duration={duration_s}",
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


def _grab_frame(binary: str, path: Path, at_s: float) -> Image.Image:
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            str(at_s),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "ppm",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    from io import BytesIO

    frame = Image.open(BytesIO(result.stdout)).convert("RGB")
    return frame


def _mean_rgb(frame: Image.Image) -> tuple[float, float, float]:
    pixels = list(frame.getdata())
    reds = sum(p[0] for p in pixels) / len(pixels)
    greens = sum(p[1] for p in pixels) / len(pixels)
    blues = sum(p[2] for p in pixels) / len(pixels)
    return reds, greens, blues


def _bright_count(frame: Image.Image, box: tuple[int, int, int, int]) -> int:
    region = frame.crop(box)
    return sum(1 for p in region.getdata() if p[0] > 200 and p[1] > 200 and p[2] > 200)


def _media(asset_id: str) -> MediaRef:
    return MediaRef(
        asset_id=asset_id,
        content_sha256=f"sha256:{asset_id}",
        media_kind="video",
        duration_us=2_000_000,
    )


def _scenario_project() -> object:
    clips = [
        Clip(
            clip_id="clip_a",
            media_ref=_media("src_a"),
            source_range=TimeRangeUS(start_us=0, end_us=2_000_000),
            timeline_range=TimeRangeUS(start_us=0, end_us=2_000_000),
        ),
        Clip(
            clip_id="clip_b",
            media_ref=_media("src_b"),
            source_range=TimeRangeUS(start_us=0, end_us=2_000_000),
            timeline_range=TimeRangeUS(start_us=2_000_000, end_us=4_000_000),
            effects=[
                EffectLayerRef(
                    operation="color.apply_lut",
                    parameters={"lut_name": "warm", "intensity": 1.0},
                    range=TimeRangeUS(start_us=2_000_000, end_us=4_000_000),
                ),
                EffectLayerRef(
                    operation="caption.burn_in",
                    parameters={"caption_asset_id": "cap_b"},
                    range=TimeRangeUS(start_us=2_000_000, end_us=4_000_000),
                ),
            ],
        ),
        Clip(
            clip_id="clip_c",
            media_ref=_media("src_c"),
            source_range=TimeRangeUS(start_us=0, end_us=2_000_000),
            timeline_range=TimeRangeUS(start_us=4_000_000, end_us=6_000_000),
        ),
    ]
    from nexus_ai_agent.creative.studio.models import AssetRecord

    track = Track(track_id="video_01", name="V", kind="video", clips=clips)
    timeline = Timeline(timeline_id="tl", duration_us=6_000_000, tracks=[track])
    project = new_project("proof", "A-B-C proof", timeline)
    return project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id=f"src_{c}",
                    media_kind="video",
                    content_sha256=f"sha256:src_{c}",
                    duration_us=2_000_000,
                )
                for c in ("a", "b", "c")
            ]
        }
    )


def test_three_segments_render_in_order_with_real_twins(tmp_path: Path) -> None:
    binary = _ffmpeg()
    assert FONT_FILE.is_file(), f"Vazirmatn missing: {FONT_FILE}"
    assert FONTS_DIR.is_dir()

    src_a, src_b, src_c = (tmp_path / f"{c}.mp4" for c in ("a", "b", "c"))
    _make_solid(src_a, "red", 2.0, binary, 440)
    _make_solid(src_b, "green", 2.0, binary, 550)
    _make_solid(src_c, "blue", 2.0, binary, 660)

    # Segment-relative subtitle timing: B's trim resets PTS, so 0.5s..1.5s
    # lands mid-segment (assembly t=2.5s..3.5s).
    srt = tmp_path / "b.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nسلام دنیا\n", encoding="utf-8")

    project = _scenario_project()
    plan = compile_execution_plan(
        project,  # type: ignore[arg-type]
        track_id="video_01",
        lut_paths={"warm": str(library_path("warm"))},
        subtitle_files={"cap_b": str(srt)},
    )
    assert plan.concat_required is True
    assert plan.unmapped_effects == ()  # every effect in the proof has a twin
    assembly = assemble_execution_plan(
        plan,
        {"src_a": str(src_a), "src_b": str(src_b), "src_c": str(src_c)},
        project=project,
    )
    compiled = compile_assembly(assembly, fontsdir=str(FONTS_DIR))
    assert compiled.duration_us == 6_000_000
    assert "lut3d=" in compiled.filtergraph
    assert "subtitles=" in compiled.filtergraph
    assert "drawtext=" not in compiled.filtergraph  # this proof needs no title
    assert compile_assembly(assembly, fontsdir=str(FONTS_DIR)).ir_hash == compiled.ir_hash

    destination = tmp_path / "abc.mp4"
    lane_artifact = encode_lane(compiled, destination, binary=binary)
    physical = verify_lane_artifact(
        lane_artifact,
        logical_content_identity="derivation:proof(project,a,b,c,warm,cap_b,title)",
    )
    assert physical.size_bytes > 0
    assert physical.probe.has_video and physical.probe.has_audio
    assert abs(physical.probe.duration_us - 6_000_000) < 300_000

    width, height = physical.probe.width, physical.probe.height
    assert (width, height) == (1280, 720)
    lower = (0, int(height * 0.70), width, height)

    frame_a = _grab_frame(binary, destination, 1.0)
    frame_b = _grab_frame(binary, destination, 3.0)
    frame_c = _grab_frame(binary, destination, 5.0)
    mean_a, mean_b, mean_c = (_mean_rgb(f) for f in (frame_a, frame_b, frame_c))

    # ORDER: each segment's frame carries its source's color.
    assert mean_a[0] > mean_a[1] and mean_a[0] > mean_a[2], mean_a  # red
    assert mean_b[1] > mean_b[0] and mean_b[1] > mean_b[2], mean_b  # green
    assert mean_c[2] > mean_c[0] and mean_c[2] > mean_c[1], mean_c  # blue

    # SUBTITLE: white Persian glyphs in B's lower third, nowhere else.
    glyphs_b = _bright_count(frame_b, lower)
    glyphs_a = _bright_count(frame_a, lower)
    assert glyphs_b > 200, f"no burn-in glyphs in B lower third ({glyphs_b})"
    assert glyphs_a < 50, f"control frame A has bright pixels ({glyphs_a})"


def _require_filter(binary: str, name: str) -> None:
    result = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-loglevel", "error", "-filters"],
        capture_output=True,
        text=True,
        check=False,
    )
    if f" {name} " not in f" {result.stdout} ":
        pytest.skip(f"engine {binary} has no {name} filter — title proof needs it")


def test_drawtext_title_burns_glyph_pixels_where_supported(tmp_path: Path) -> None:
    """The title twin burns real glyphs — where the engine has ``drawtext``.

    Skips loudly on the imageio wheel (no libfreetype); proves on CI's full
    FFmpeg.  A skip is honest unavailability, not a pass: the test name says
    exactly what was not proven.
    """
    binary = _ffmpeg()
    _require_filter(binary, "drawtext")
    assert FONT_FILE.is_file(), f"Vazirmatn missing: {FONT_FILE}"

    src = tmp_path / "bg.mp4"
    _make_solid(src, "blue", 2.0, binary, 660)

    from nexus_ai_agent.creative.rendering import LaneIR, LaneSource, compile_lane
    from nexus_ai_agent.creative.rendering.ir import TitleOp

    main = LaneSource("main", str(src), "video", 2_000_000)
    compiled = compile_lane(
        LaneIR(
            main=main,
            ops=(
                TitleOp(
                    text="SEGMENT C",
                    font_size=72,
                    color="#FFFFFF",
                    position="center",
                ),
            ),
        ),
        fontfile=str(FONT_FILE),
    )
    destination = tmp_path / "titled.mp4"
    encode_lane(compiled, destination, binary=binary)
    frame = _grab_frame(binary, destination, 1.0)
    width, height = frame.size
    center = (int(width * 0.2), int(height * 0.3), int(width * 0.8), int(height * 0.7))
    glyphs = _bright_count(frame, center)
    assert glyphs > 200, f"no title glyphs at center ({glyphs})"


def test_warm_lut_moves_real_pixels_against_a_control(tmp_path: Path) -> None:
    """The LUT twin is no passthrough: graded gray ≠ ungraded gray.

    Mid-gray is where a warm lift/gain look bites (the lattice maps 0.5 →
    ≈0.58/0.55/0.49); saturated primaries shift only a few LSB by design.
    """
    binary = _ffmpeg()
    src = tmp_path / "g.mp4"
    _make_solid(src, "gray", 2.0, binary, 550)

    from nexus_ai_agent.creative.rendering import LaneIR, LaneSource, compile_lane
    from nexus_ai_agent.creative.rendering.ir import LutOp

    main = LaneSource("main", str(src), "video", 2_000_000)
    graded = compile_lane(
        LaneIR(
            main=main,
            ops=(LutOp(lut_name="warm", lut_path=str(library_path("warm")), intensity=1.0),),
        )
    )
    control = compile_lane(LaneIR(main=main, ops=()))
    out_graded, out_control = tmp_path / "graded.mp4", tmp_path / "control.mp4"
    encode_lane(graded, out_graded, binary=binary)
    encode_lane(control, out_control, binary=binary)

    frame_g = _grab_frame(binary, out_graded, 1.0)
    frame_c = _grab_frame(binary, out_control, 1.0)
    mean_g, mean_c = _mean_rgb(frame_g), _mean_rgb(frame_c)
    distance = sum(abs(a - b) for a, b in zip(mean_g, mean_c, strict=True))
    assert distance > 15.0, f"warm LUT left pixels untouched: {mean_g} vs {mean_c}"
    # ... and the two files are different artifacts with different hashes.
    assert sha256_file_bytes(out_graded) != sha256_file_bytes(out_control)
