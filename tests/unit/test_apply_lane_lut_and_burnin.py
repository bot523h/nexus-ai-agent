"""Visible proofs: identity LUT + Persian drawtext through the real executor."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.rendering import LutOp, TitleOp, compile_lane, render_lane
from nexus_ai_agent.creative.rendering.ir import LaneIR, LaneProfile, LaneSource
from nexus_ai_agent.creative.rendering.lifecycle import shipped_identity_lut, shipped_persian_font
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available")


def _make_clip(path: Path, duration_s: float, binary: str) -> None:
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
            f"sine=frequency=440:duration={duration_s}",
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


def test_lut_golden_argv_contains_lut3d() -> None:
    cube = str(shipped_identity_lut())
    compiled = compile_lane(
        LaneIR(
            main=LaneSource("main", "/media/main.mp4", "video", 2_000_000),
            ops=(LutOp(cube_path=cube),),
        )
    )
    assert "lut3d=file=" in compiled.filtergraph
    assert "interp=tetrahedral" in compiled.filtergraph


def test_lut_intensity_not_one_is_typed_refusal() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LutOp(cube_path="/x.cube", intensity=0.5)


def test_real_identity_lut_encode(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    cube = str(shipped_identity_lut())
    artifact = render_lane(
        LaneIR(
            main=LaneSource("main", str(src), "video", 1_000_000),
            ops=(LutOp(cube_path=cube),),
            profile=LaneProfile(width=320, height=240),
        ),
        tmp_path / "lut.mp4",
        binary=binary,
    )
    assert artifact.has_audio is True
    assert artifact.ops == ("lut",)
    assert Path(artifact.path).stat().st_size > 1000
    assert abs(artifact.duration_us - 1_000_000) < 250_000


def test_real_persian_burnin_with_shipped_font(tmp_path: Path) -> None:
    binary = _ffmpeg()
    from nexus_ai_agent.creative.rendering.executor import activate_runtime

    runtime = activate_runtime(binary)
    if "drawtext" not in runtime.filters:
        pytest.skip("this FFmpeg build has no drawtext (no libfreetype)")
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    font = str(shipped_persian_font())
    artifact = render_lane(
        LaneIR(
            main=LaneSource("main", str(src), "video", 1_000_000),
            ops=(TitleOp(text="سلام دنیا", font_size=32),),
            profile=LaneProfile(width=320, height=240),
        ),
        tmp_path / "burnin.mp4",
        binary=binary,
        fontfile=font,
    )
    assert artifact.ops == ("title",)
    assert Path(artifact.path).stat().st_size > 1000
