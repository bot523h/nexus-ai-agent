"""Pytest fixtures for the hyper-compiler package.

Adds ``src`` to ``sys.path`` so the package imports without installation, and
makes the bundled FFmpeg binary discoverable on ``PATH`` so every module that
calls ``ffmpeg``/``ffprobe`` by name works in tests.  Real test clips are
rendered into ``/tmp`` (never into the repository).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Make ffmpeg discoverable by bare name.  imageio-ffmpeg ships the binary under
# a versioned filename, so expose a plain ``ffmpeg`` name on PATH.
_HC_BIN = Path(tempfile.mkdtemp(prefix="hyper_compiler_bin_"))
_ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
(_HC_BIN / "ffmpeg").symlink_to(_ffmpeg_src)
os.environ["PATH"] = str(_HC_BIN) + os.pathsep + os.environ.get("PATH", "")

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture
def ffmpeg_bin() -> str:
    return FFMPEG_BIN


@pytest.fixture
def clips_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="hyper_compiler_clips_"))


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        args,
        shell=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


@pytest.fixture
def make_clip(clips_dir: Path):
    """Factory: render a real test clip and return its path.

    Keywords: duration, width, height, fps, has_audio, rotate_metadata (0/90/180/270).
    """

    def factory(name: str = "clip.mp4", **kwargs) -> Path:
        duration = float(kwargs.get("duration", 3.0))
        width = int(kwargs.get("width", 320))
        height = int(kwargs.get("height", 180))
        fps = float(kwargs.get("fps", 30.0))
        has_audio = bool(kwargs.get("has_audio", True))
        rotate = int(kwargs.get("rotate_metadata", 0))

        path = clips_dir / name
        video_filter = f"scale={width}:{height},fps={fps}"
        args = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={width}x{height}:rate={fps}",
        ]
        if rotate:
            args += ["-metadata:s:v", f"rotate={rotate}"]
        if has_audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
        args += [
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
        ]
        if has_audio:
            args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-shortest"]
        args += ["-t", f"{duration}", str(path)]
        _run_ffmpeg(args)
        return path

    return factory
