"""Media workers that back capability execution.

A worker maps a *capability id* to a concrete operation implemented by an
external tool (FFmpeg, ImageMagick). The worker is the only place in the
composition layer that touches the filesystem or spawns processes; the
compiler and proof layer stay pure.

The builtin worker implements a small set of *deterministic* operations so
compositions can be reproduced byte-for-byte (modulo container metadata,
which the proof layer strips by re-hashing only the elementary streams'
structural properties).

Security (process boundary, Gate 4 of the hardening mission):

* **argv-only execution**, ``shell=False``, explicit argument list;
* **binary allow-list** -- FFmpeg is resolved through the codebase's single
  resolver (:func:`nexus_ai_agent.creative.composition.canonical.resolve_ffmpeg`)
  and ImageMagick through a fixed candidate list, so a capability can never
  name an arbitrary executable;
* **controlled environment** -- workers spawn with a minimal, allow-listed
  environment (:func:`_safe_env`); the parent's secrets (tokens, keys) are
  never inherited;
* **dedicated scratch dir** per node, per attempt;
* **path containment** -- inputs are resolved under the node scratch dir and
  refused if they escape (path-traversal + symlink guard);
* **wall-clock timeout** and an **output-size limit**.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Environment variables a worker subprocess may inherit. Anything else in the
#: parent environment (API tokens, credentials, proxies) is stripped.
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NEXUS_FFMPEG_BIN",
        "MAGICK_TMPDIR",
        "MAGICK_HOME",
    }
)

#: Default cap on a single produced output file.
_MAX_OUTPUT_BYTES = 512 * 1024 * 1024


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
# Process-boundary guards
# ---------------------------------------------------------------------------


def _safe_resolve(base: Path, candidate: str) -> Path:
    """Resolve *candidate* under *base*, refusing anything that escapes.

    Both absolute and relative candidates are resolved against *base*; the
    result must be *base* or a child of it. Symlinks are followed by
    ``resolve()`` and the *final* target is checked, so a symlink pointing
    outside the scratch dir is refused too. This is the composition layer's
    path-traversal + symlink-escape guard.
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


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal, allow-listed environment for a worker subprocess.

    Only variables in :data:`_ENV_ALLOWLIST` survive; everything else --
    including any credential the host process carries -- is dropped. ``extra``
    adds worker-specific entries (e.g. a tool's temp dir) on top.
    """
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if extra:
        env.update(extra)
    return env


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one allow-listed binary with a bounded capture and the safe env."""
    return subprocess.run(
        cmd,
        shell=False,
        cwd=str(cwd),
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
        env=env if env is not None else _safe_env(),
    )


def _enforce_output_limit(path: Path, limit: int = _MAX_OUTPUT_BYTES) -> None:
    size = path.stat().st_size
    if size > limit:
        path.unlink(missing_ok=True)
        raise WorkerError(f"worker output exceeded the {limit}-byte limit ({size} bytes)")


def _resolve_imagemagick() -> str:
    """Resolve ImageMagick's ``convert`` from a fixed candidate list only."""
    for name in ("convert", "magick"):
        found = shutil.which(name)
        if found:
            return found
    raise WorkerError("ImageMagick 'convert'/'magick' not found on PATH")


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
            r = _run(cmd, cwd=Path.cwd(), timeout=10)
            first = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else "unknown"
            return first.strip()
        except (OSError, subprocess.SubprocessError, WorkerError):
            return "missing"

    ffmpeg = "missing"
    try:
        from .canonical import resolve_ffmpeg

        ffmpeg = _v([resolve_ffmpeg(), "-version"])
    except Exception:  # pragma: no cover - env-level resolution failure
        pass
    imagemagick = "missing"
    try:
        imagemagick = _v([_resolve_imagemagick(), "-version"])
    except WorkerError:  # pragma: no cover - env-level
        pass
    return ToolVersions(ffmpeg=ffmpeg, imagemagick=imagemagick)


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

    def __init__(
        self,
        *,
        ffmpeg: str | None = None,
        convert: str | None = None,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        # FFmpeg always goes through the canonical allow-listed resolver.
        if ffmpeg is not None:
            self._ffmpeg = ffmpeg
        else:
            from .canonical import resolve_ffmpeg

            self._ffmpeg = resolve_ffmpeg()
        self._convert = convert or _resolve_imagemagick()
        self._max_output_bytes = max_output_bytes

    @property
    def ffmpeg_binary(self) -> str:
        return self._ffmpeg

    @property
    def imagemagick_binary(self) -> str:
        return self._convert

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
        outputs = handler(inputs, params, output_dir)
        for path in outputs.values():
            _enforce_output_limit(Path(path), self._max_output_bytes)
        return outputs

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
