"""Media workers that back capability execution.

A worker maps a *capability id* to a concrete operation implemented by an
external tool (FFmpeg, ImageMagick). The worker is the only place in the
composition layer that touches the filesystem or spawns processes; the
compiler and proof layer stay pure.

The builtin worker implements a small set of *deterministic* operations so
compositions can be reproduced byte-for-byte (modulo container metadata,
which the proof layer strips by re-hashing only the elementary streams'
structural properties).

Security: every subprocess is launched with ``shell=False``, an explicit
argument list, a working directory inside the composition's scratch dir, and
a timeout. Input paths are resolved against the scratch dir and refused if
they escape it (path-traversal guard).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .spec import CapabilitySpec


class WorkerError(RuntimeError):
    """A worker failed to execute a capability."""


@runtime_checkable
class MediaWorker(Protocol):
    """Execute one capability instance and return output paths.

    ``inputs`` maps input port name -> absolute path of the produced media.
    ``params`` is the node's parameter dict. ``output_dir`` is a per-node
    scratch directory the worker owns. Returns a map of output port name ->
    absolute output path.
    """

    def run(
        self,
        capability_id: str,
        inputs: dict[str, str],
        params: dict,
        output_dir: Path,
    ) -> dict[str, str]: ...


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


def _safe_resolve(base: Path, candidate: str) -> Path:
    """Resolve *candidate* under *base*, refusing anything that escapes.

    Both absolute and relative candidates are resolved against *base*. The
    result must be *base* or a child of it; otherwise :class:`WorkerError`
    is raised. This is the composition layer's path-traversal guard: a
    capability param can never name a file outside its scratch directory.
    """
    base = base.resolve()
    candidate_path = Path(candidate)
    if candidate_path.is_absolute():
        resolved = candidate_path.resolve()
    else:
        resolved = (base / candidate_path).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise WorkerError(
            f"path escapes scratch directory: {candidate!r} (resolved to {resolved})"
        ) from None
    return resolved


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=False,
        cwd=str(cwd),
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Builtin worker (FFmpeg + ImageMagick)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolVersions:
    ffmpeg: str
    imagemagick: str

    def to_dict(self) -> dict[str, str]:
        return {"ffmpeg": self.ffmpeg, "imagemagick": self.imagemagick}


def detect_tool_versions() -> ToolVersions:
    def _v(cmd: list[str]) -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            first = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else "unknown"
            return first.strip()
        except (OSError, subprocess.SubprocessError):
            return "missing"

    return ToolVersions(ffmpeg=_v(["ffmpeg", "-version"]), imagemagick=_v(["convert", "-version"]))


class BuiltinMediaWorker:
    """The default worker: deterministic operations over FFmpeg + ImageMagick.

    Implemented capability ids:

    * ``nagar.image.resize``     -- ImageMagick ``-resize WxH``
    * ``nagar.image.convert``    -- ImageMagick format conversion
    * ``nagar.image.grayscale``  -- ImageMagick ``-colorspace Gray``
    * ``nagar.image.blur``       -- ImageMagick ``-blur``
    * ``nagar.video.from_image`` -- FFmpeg image loop -> mp4 (fps + duration)
    * ``nagar.video.trim``       -- FFmpeg ``-ss`` / ``-to`` trim
    * ``nagar.video.add_text``   -- FFmpeg ``drawtext`` burn-in
    """

    def __init__(self, *, ffmpeg: str = "ffmpeg", convert: str = "convert") -> None:
        self._ffmpeg = ffmpeg
        self._convert = convert

    def run(
        self,
        capability_id: str,
        inputs: dict[str, str],
        params: dict,
        output_dir: Path,
    ) -> dict[str, str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        handler = getattr(self, f"_op_{capability_id.replace('.', '_')}", None)
        if handler is None:
            raise WorkerError(f"no builtin worker for capability {capability_id!r}")
        return handler(inputs, params, output_dir)

    # -- image ops (ImageMagick) -------------------------------------------

    def _op_nagar_image_resize(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        width = int(params.get("width", 1280))
        height = int(params.get("height", 720))
        out = output_dir / "resized.png"
        cmd = [self._convert, str(src), "-resize", f"{width}x{height}!", str(out)]
        r = _run(cmd, cwd=output_dir, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"resize failed: {r.stderr.strip()[:300]}")
        return {"resized": str(out)}

    def _op_nagar_image_convert(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        fmt = params.get("format", "jpeg").lower()
        ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
        out = output_dir / f"converted.{ext}"
        quality = str(params.get("quality", 90))
        cmd = [self._convert, str(src), "-quality", quality, str(out)]
        r = _run(cmd, cwd=output_dir, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"convert failed: {r.stderr.strip()[:300]}")
        return {"converted": str(out)}

    def _op_nagar_image_grayscale(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        out = output_dir / "gray.png"
        cmd = [self._convert, str(src), "-colorspace", "Gray", str(out)]
        r = _run(cmd, cwd=output_dir, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"grayscale failed: {r.stderr.strip()[:300]}")
        return {"grayscale": str(out)}

    def _op_nagar_image_blur(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        radius = float(params.get("radius", 2.0))
        sigma = float(params.get("sigma", 1.0))
        out = output_dir / "blurred.png"
        cmd = [self._convert, str(src), "-blur", f"{radius}x{sigma}", str(out)]
        r = _run(cmd, cwd=output_dir, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"blur failed: {r.stderr.strip()[:300]}")
        return {"blurred": str(out)}

    # -- video ops (FFmpeg) ------------------------------------------------

    def _op_nagar_video_from_image(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        fps = int(params.get("fps", 30))
        duration = float(params.get("duration_seconds", 3.0))
        out = output_dir / "video.mp4"
        cmd = [
            self._ffmpeg, "-y",
            "-loop", "1", "-i", str(src),
            "-t", str(duration),
            "-r", str(fps),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out),
        ]
        r = _run(cmd, cwd=output_dir, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"from_image failed: {r.stderr.strip()[:300]}")
        return {"video": str(out)}

    def _op_nagar_video_trim(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        start = float(params.get("start_seconds", 0.0))
        end = float(params.get("end_seconds", 1.0))
        out = output_dir / "trimmed.mp4"
        cmd = [
            self._ffmpeg, "-y",
            "-ss", str(start), "-to", str(end),
            "-i", str(src),
            "-c", "copy",
            str(out),
        ]
        r = _run(cmd, cwd=output_dir, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"trim failed: {r.stderr.strip()[:300]}")
        return {"video": str(out)}

    def _op_nagar_video_add_text(self, inputs, params, output_dir):
        src = _safe_resolve(output_dir, inputs["source"])
        text = str(params.get("text", ""))
        font_size = int(params.get("font_size", 32))
        out = output_dir / "text.mp4"
        # drawtext needs an escaping pass; keep it simple and safe.
        safe_text = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        cmd = [
            self._ffmpeg, "-y",
            "-i", str(src),
            "-vf",
            f"drawtext=text='{safe_text}':x=(w-text_w)/2:y=h-(text_h*2):"
            f"fontsize={font_size}:fontcolor=white:borderw=2",
            "-c:a", "copy",
            str(out),
        ]
        r = _run(cmd, cwd=output_dir, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise WorkerError(f"add_text failed: {r.stderr.strip()[:300]}")
        return {"video": str(out)}


__all__ = [
    "BuiltinMediaWorker",
    "MediaWorker",
    "ToolVersions",
    "WorkerError",
    "detect_tool_versions",
]
