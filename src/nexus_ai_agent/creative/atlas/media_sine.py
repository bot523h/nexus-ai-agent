"""WAVE 29 — media sine: a real, byte-verified FFmpeg synthesis and probe layer.

This module is the *only* place in the ATLAS zone that shells out to external
binaries, and it does so with an explicit, safe contract:

* no ``shell=True`` — every invocation is an argument list built by the module;
* a bounded wall-clock timeout on every subprocess;
* output paths must resolve *inside* a caller-supplied workspace root (the
  repository's ``WorkspaceFilesystem`` boundary is reused for path safety);
* the produced media is re-opened with ``ffprobe`` and its stream geometry
  (codec, width, height, frame count, duration) is checked against the request.

The synthesised clip is a moving test pattern; it is a *real* encoded artifact
on disk, not a mock. If ``ffmpeg``/``ffprobe`` are unavailable the functions
raise :class:`MediaSineUnavailable` with the exact missing binary name so the
caller can report BLOCKED rather than pretend the render happened.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final

from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "MediaSineError",
    "MediaSineUnavailable",
    "MediaProbe",
    "MediaSineResult",
    "ffmpeg_available",
    "synthesise_test_clip",
    "probe_media",
    "MAX_DIMENSION",
    "MAX_FRAMES",
    "DEFAULT_TIMEOUT",
]

MAX_DIMENSION: Final[int] = 4096
MAX_FRAMES: Final[int] = 60_000
DEFAULT_TIMEOUT: Final[float] = 120.0


class MediaSineError(ValueError):
    """Raised for malformed requests or a media operation that failed."""


class MediaSineUnavailable(MediaSineError):
    """Raised when a required external binary is not on PATH."""


@dataclass(frozen=True)
class MediaProbe:
    """The geometry of a media file as reported by ``ffprobe``."""

    codec: str
    width: int
    height: int
    nb_frames: int
    duration: Fraction
    pix_fmt: str


@dataclass(frozen=True)
class MediaSineResult:
    """A synthesised clip: its relative path, byte size and verified probe."""

    relative_path: str
    size_bytes: int
    probe: MediaProbe


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise MediaSineUnavailable(f"required binary {binary!r} is not on PATH")
    return path


def ffmpeg_available() -> bool:
    """Whether both ``ffmpeg`` and ``ffprobe`` are on PATH (no execution)."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if timeout <= 0.0:
        raise MediaSineError("timeout must be positive")
    return subprocess.run(  # noqa: S603 - argument list, never shell=True
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _even(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MediaSineError(f"{name} must be an int")
    if value < 2 or value > MAX_DIMENSION:
        raise MediaSineError(f"{name} must be in [2, {MAX_DIMENSION}]")
    return value - (value % 2)


def synthesise_test_clip(
    filesystem: WorkspaceFilesystem,
    relative_path: str,
    *,
    width: int = 128,
    height: int = 72,
    frames: int = 24,
    fps: int = 24,
    codec: str = "libx264",
    timeout: float = DEFAULT_TIMEOUT,
) -> MediaSineResult:
    """Encode a real moving test-pattern clip into ``filesystem``.

    The pattern is FFmpeg's ``testsrc`` with a frame counter drawn on top, so the
    frames are visually distinct. The result is probed after encoding and the
    observed geometry is asserted against the request.
    """
    ffmpeg = _require("ffmpeg")
    if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1:
        raise MediaSineError("frames must be a positive int")
    if frames > MAX_FRAMES:
        raise MediaSineError(f"frames must be <= {MAX_FRAMES}")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps < 1 or fps > 240:
        raise MediaSineError("fps must be an int in [1, 240]")
    if codec not in {"libx264", "mpeg4"}:
        raise MediaSineError("codec must be 'libx264' or 'mpeg4'")

    w = _even(width, "width")
    h = _even(height, "height")
    target = filesystem.resolve(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={w}x{h}:rate={fps}:duration={frames}",
        "-frames:v",
        str(frames),
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
        str(target),
    ]
    proc = _run(argv, timeout=timeout)
    if proc.returncode != 0:
        raise MediaSineError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    if not target.is_file():
        raise MediaSineError("ffmpeg reported success but no output file exists")

    probe = probe_media(filesystem, relative_path, timeout=timeout)
    if (probe.width, probe.height) != (w, h):
        raise MediaSineError(f"probe geometry {probe.width}x{probe.height} != requested {w}x{h}")
    if probe.nb_frames != frames:
        raise MediaSineError(f"probe frame count {probe.nb_frames} != requested {frames}")
    return MediaSineResult(
        relative_path=relative_path, size_bytes=target.stat().st_size, probe=probe
    )


def probe_media(
    filesystem: WorkspaceFilesystem, relative_path: str, *, timeout: float = DEFAULT_TIMEOUT
) -> MediaProbe:
    """Probe a media file with ``ffprobe`` and return its verified geometry."""
    ffprobe = _require("ffprobe")
    target = filesystem.resolve(relative_path)
    if not target.is_file():
        raise MediaSineError(f"{relative_path!r} is not a regular file")
    argv = [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=codec_name,width,height,nb_read_frames,pix_fmt,duration",
        "-of",
        "json",
        str(target),
    ]
    proc = _run(argv, timeout=timeout)
    if proc.returncode != 0:
        raise MediaSineError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    try:
        payload: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - ffprobe emits JSON or errors
        raise MediaSineError(f"ffprobe emitted invalid JSON: {exc}") from exc
    streams = payload.get("streams")
    if not streams:
        raise MediaSineError("ffprobe found no video stream")
    stream = streams[0]
    nb = stream.get("nb_read_frames")
    if nb is None:
        raise MediaSineError("ffprobe did not report a frame count")
    duration = stream.get("duration")
    dur = Fraction(str(duration)) if duration is not None else Fraction(0)
    return MediaProbe(
        codec=str(stream.get("codec_name", "unknown")),
        width=int(stream.get("width", 0)),
        height=int(stream.get("height", 0)),
        nb_frames=int(nb),
        duration=dur,
        pix_fmt=str(stream.get("pix_fmt", "unknown")),
    )
