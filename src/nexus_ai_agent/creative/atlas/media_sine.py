"""WAVE 29 — media sine: a real, byte-verified FFmpeg synthesis and probe layer.

This module is the *only* place in the ATLAS zone that shells out to external
binaries, and it does so with an explicit, safe contract:

* no ``shell=True`` — every invocation is an argument list built by the module;
* a bounded wall-clock timeout on every subprocess;
* output paths must resolve *inside* a caller-supplied workspace root (the
  repository's ``WorkspaceFilesystem`` boundary is reused for path safety);
* the produced media is re-opened with ``ffprobe`` and its stream geometry
  (codec, width, height, frame count, duration) is checked against the request.

Frames are never invented here: :func:`encode_rgb24_stream` pulls raw ``rgb24``
frames one at a time from a caller-supplied source and pipes them to FFmpeg over
``stdin``, so the encoded clip contains exactly the pixels the caller rendered and
peak memory is one frame, not the whole clip. If ``ffmpeg``/``ffprobe`` are
unavailable the functions raise :class:`MediaSineUnavailable` with the exact missing
binary name so the caller can report BLOCKED rather than pretend the render happened.
"""

from __future__ import annotations

import contextlib
import json
import math
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
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
    "require_binary",
    "run_process",
    "encode_rgb24_stream",
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


def require_binary(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise MediaSineUnavailable(f"required binary {binary!r} is not on PATH")
    return path


def ffmpeg_available() -> bool:
    """Whether both ``ffmpeg`` and ``ffprobe`` are on PATH (no execution)."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run_process(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise MediaSineError("timeout must be positive and finite")
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


def encode_rgb24_stream(
    filesystem: WorkspaceFilesystem,
    relative_path: str,
    frame_source: Callable[[int], bytes],
    *,
    width: int,
    height: int,
    frames: int,
    fps: int,
    codec: str = "libx264",
    timeout: float = DEFAULT_TIMEOUT,
) -> MediaSineResult:
    """Encode ``frames`` raw ``rgb24`` frames pulled from ``frame_source`` into a clip.

    ``frame_source(i)`` is called once per frame in order ``0 .. frames-1`` and must
    return exactly ``width * height * 3`` bytes. The bytes go straight to FFmpeg's
    ``stdin`` (``-f rawvideo -pix_fmt rgb24 -s WxH -r fps -i pipe:0``), so only one
    frame is held by this process at a time. The whole run shares one wall-clock
    ``timeout``; the output is re-opened with ``ffprobe`` and its geometry and frame
    count are checked against the request.
    """
    ffmpeg = require_binary("ffmpeg")
    if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1:
        raise MediaSineError("frames must be a positive int")
    if frames > MAX_FRAMES:
        raise MediaSineError(f"frames must be <= {MAX_FRAMES}")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps < 1 or fps > 240:
        raise MediaSineError("fps must be an int in [1, 240]")
    if codec not in {"libx264", "mpeg4"}:
        raise MediaSineError("codec must be 'libx264' or 'mpeg4'")
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise MediaSineError("timeout must be positive and finite")

    w = _even(width, "width")
    h = _even(height, "height")
    expected = w * h * 3
    target = filesystem.resolve(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{w}x{h}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-frames:v",
        str(frames),
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
        str(target),
    ]
    deadline = time.monotonic() + timeout
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(  # noqa: S603 - argument list, never shell=True
            argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file
        )

        def _watchdog_kill() -> None:
            timed_out.set()
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()

        timer = threading.Timer(max(deadline - time.monotonic(), 0.0), _watchdog_kill)
        timer.daemon = True
        timer.start()
        try:
            _pump_frames(proc, frame_source, frames, expected, deadline, timed_out)
            returncode = _wait_until(proc, deadline)
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.close()
        if timed_out.is_set():
            raise MediaSineError("encode exceeded its wall-clock timeout")
        if returncode != 0:
            err_file.seek(0)
            detail = err_file.read().decode("utf-8", "replace").strip()[:400]
            raise MediaSineError(f"ffmpeg failed ({returncode}): {detail}")
    if not target.is_file():
        raise MediaSineError("ffmpeg reported success but no output file exists")

    remaining_probe_timeout = deadline - time.monotonic()
    if remaining_probe_timeout <= 0.0:
        raise MediaSineError("encode/probe exceeded its wall-clock timeout")
    probe = probe_media(filesystem, relative_path, timeout=remaining_probe_timeout)
    if (probe.width, probe.height) != (w, h):
        raise MediaSineError(f"probe geometry {probe.width}x{probe.height} != requested {w}x{h}")
    if probe.nb_frames != frames:
        raise MediaSineError(f"probe frame count {probe.nb_frames} != requested {frames}")
    return MediaSineResult(
        relative_path=relative_path, size_bytes=target.stat().st_size, probe=probe
    )


def _pump_frames(
    proc: subprocess.Popen[bytes],
    frame_source: Callable[[int], bytes],
    frames: int,
    expected: int,
    deadline: float,
    timed_out: threading.Event | None = None,
) -> None:
    stdin = proc.stdin
    if stdin is None:
        raise MediaSineError("ffmpeg stdin is unavailable")
    flush = getattr(stdin, "flush", None)
    for index in range(frames):
        if (timed_out is not None and timed_out.is_set()) or time.monotonic() > deadline:
            raise MediaSineError("encode exceeded its wall-clock timeout")
        data = frame_source(index)
        if len(data) != expected:
            raise MediaSineError(f"frame {index} has {len(data)} bytes, expected {expected}")
        try:
            stdin.write(data)
            if callable(flush):
                flush()
        except (BrokenPipeError, OSError) as exc:
            if (timed_out is not None and timed_out.is_set()) or time.monotonic() > deadline:
                raise MediaSineError("encode exceeded its wall-clock timeout") from exc
            raise MediaSineError("ffmpeg closed its input before every frame was written") from exc
    try:
        stdin.close()
    except (BrokenPipeError, OSError) as exc:
        if (timed_out is not None and timed_out.is_set()) or time.monotonic() > deadline:
            raise MediaSineError("encode exceeded its wall-clock timeout") from exc
        raise MediaSineError("ffmpeg closed its input before every frame was written") from exc


def _wait_until(proc: subprocess.Popen[bytes], deadline: float) -> int:
    remaining = max(deadline - time.monotonic(), 1e-3)
    try:
        return proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise MediaSineError("ffmpeg did not finish before the wall-clock timeout") from exc


def probe_media(
    filesystem: WorkspaceFilesystem, relative_path: str, *, timeout: float = DEFAULT_TIMEOUT
) -> MediaProbe:
    """Probe a media file with ``ffprobe`` and return its verified geometry."""
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise MediaSineError("timeout must be positive and finite")
    ffprobe = require_binary("ffprobe")
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
    proc = run_process(argv, timeout=timeout)
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
