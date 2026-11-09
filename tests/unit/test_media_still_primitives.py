"""Unit tests for the still-image primitives the composition layer relies on.

The repository had only ``upscale_image``; a composition needs an **exact
geometry** resize and a **luma-only** grayscale conversion.  These tests pin
the contract of both, including the atomic staging/rename behaviour and the
refusals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    RenderError,
    build_grayscale_command,
    build_resize_command,
    grayscale_image,
    resize_image,
    resolve_ffmpeg_bin,
    sha256_file,
)


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"FFmpeg unavailable: {exc}")


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.png"
    Image.new("RGB", (320, 240), (200, 60, 30)).save(path)
    return path


# ---------------------------------------------------------------------------
# the argv builders (pure — nothing is executed)
# ---------------------------------------------------------------------------


def test_the_resize_argv_is_an_exact_geometry_lanczos_scale(tmp_path: Path) -> None:
    args = build_resize_command(
        tmp_path / "in.png", width=1280, height=720, output_path=tmp_path / "out.png"
    )
    assert args[0] == "ffmpeg"
    assert "-vf" in args
    assert "scale=1280:720:flags=lanczos" in args
    assert "-frames:v" in args and "1" in args
    assert args[-1].endswith("out.png")
    # no shell metacharacters can reach a shell: there is no shell
    assert not any(";" in part or "|" in part or "&" in part for part in args)


def test_the_grayscale_argv_converts_to_a_luma_plane(tmp_path: Path) -> None:
    args = build_grayscale_command(tmp_path / "in.png", output_path=tmp_path / "out.png")
    assert "format=gray" in args
    assert "gray" in args


def test_the_resize_argv_refuses_an_absurd_geometry(tmp_path: Path) -> None:
    with pytest.raises(RenderError):
        build_resize_command(
            tmp_path / "in.png", width=1, height=1, output_path=tmp_path / "out.png"
        )
    with pytest.raises(RenderError):
        build_resize_command(
            tmp_path / "in.png", width=99999, height=10, output_path=tmp_path / "out.png"
        )
    with pytest.raises(RenderError):
        build_resize_command(
            tmp_path / "in.png", width=8000, height=8000, output_path=tmp_path / "out.png"
        )


# ---------------------------------------------------------------------------
# the real primitives
# ---------------------------------------------------------------------------


def test_resize_produces_the_exact_geometry(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "resized.png"
    artifact = resize_image(source, out, width=1280, height=720, binary=_ffmpeg())
    assert out.is_file()
    with Image.open(out) as image:
        assert image.size == (1280, 720)
    assert artifact.width == 1280
    assert artifact.height == 720
    assert artifact.sha256 == sha256_file(out)


def test_resize_refuses_to_overwrite_its_source(source: Path) -> None:
    with pytest.raises(RenderError):
        resize_image(source, source, width=64, height=64, binary=_ffmpeg())


def test_resize_refuses_a_non_png_destination(source: Path, tmp_path: Path) -> None:
    with pytest.raises(RenderError):
        resize_image(source, tmp_path / "out.jpg", width=64, height=64, binary=_ffmpeg())


def test_resize_refuses_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(RenderError):
        resize_image(
            tmp_path / "absent.png",
            tmp_path / "out.png",
            width=64,
            height=64,
            binary=_ffmpeg(),
        )


def test_resize_does_not_overwrite_without_permission(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "resized.png"
    resize_image(source, out, width=64, height=64, binary=_ffmpeg())
    with pytest.raises(RenderError):
        resize_image(source, out, width=128, height=128, binary=_ffmpeg())


def test_a_failed_resize_leaves_no_artifact(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    out = tmp_path / "resized.png"
    with pytest.raises(RenderError):
        resize_image(broken, out, width=64, height=64, binary=_ffmpeg())
    assert not out.exists()
    assert not list(tmp_path.glob(".*.part*")), "no staging file may survive"


def test_grayscale_produces_a_luma_only_png(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "gray.png"
    artifact = grayscale_image(source, out, binary=_ffmpeg())
    assert out.is_file()
    with Image.open(out) as image:
        assert image.mode in ("L", "LA", "P")
        rgb = image.convert("RGB")
        assert all(low == high for low, high in rgb.getextrema())
    assert artifact.width == 320
    assert artifact.height == 240


def test_grayscale_refuses_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(RenderError):
        grayscale_image(tmp_path / "absent.png", tmp_path / "out.png", binary=_ffmpeg())
