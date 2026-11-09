"""The composition layer's media engine: one realization per canonical operation.

The engine is the *adapter* half of the split this repository already uses
for the slideshow pack: ``packs/slideshow/operations.py`` records the state,
``creative/slideshow/upscale.py`` produces the media.  Here the pack is
``packs/media/operations.py`` and the media producer is this module.

Three rules make this module an adapter and not an authority:

1. **It cannot grant permission.**  A realization exists only for an
   operation id that the live registry already knows; the compiler refuses
   everything else before a process is started, and the command bus applies
   the registry's permission ladder and the pack lifecycle gate afterwards.
2. **It spawns no unlisted binary.**  Every media operation runs through the
   canonical FFmpeg primitives (``resolve_ffmpeg_bin`` + ``resize_image`` /
   ``grayscale_image`` / ``encode``), which resolve one allow-listed binary
   and run it with ``shell=False`` and an argv list.
3. **It inherits no secret.**  The encoder subprocess runs with the canonical
   allow-listed environment (:func:`encoder_env`).

The engine also owns the *path* and *size* guards of the composition layer:
a node may only write inside its own scratch directory, and a produced file
above the output ceiling is deleted and refused.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    RenderError,
    RenderIR,
    RenderShot,
    encode,
    grayscale_image,
    probe_video,
    resize_image,
    resolve_ffmpeg_bin,
    sha256_file,
)

#: The canonical operation ids this engine can realise.  The mapping is an
#: *execution* table, not a registry: it grants no permission and it is
#: consulted only after the compiler resolved the same id in the live
#: registry.
RESIZE_OPERATION = "media.resize_image"
GRAYSCALE_OPERATION = "media.grayscale_image"
IMAGE_TO_VIDEO_OPERATION = "media.image_to_video"

#: The default ceiling for one produced file.
MAX_OUTPUT_BYTES = 512 * 1024 * 1024

#: The default ceiling for one consumed file.
MAX_INPUT_BYTES = 512 * 1024 * 1024

#: The template id recorded in the render IR of an image-to-video node.
RENDER_TEMPLATE_ID = "nagar.composition.media.transform"


class MediaEngineError(RuntimeError):
    """A media realization failed."""


@dataclass(frozen=True)
class MediaOutcome:
    """What a realization produced — measured from the bytes on disk."""

    path: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    duration_us: int
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "duration_us": self.duration_us,
            **self.facts,
        }


def contained_path(base: Path, candidate: str) -> Path:
    """Resolve *candidate* under *base*, refusing anything that escapes.

    Both absolute and relative candidates are resolved against *base*; the
    result must be *base* itself or a child of it.  Symlinks are followed by
    ``resolve()`` and the **final target** is checked, so a link pointing out
    of the scratch directory is refused too.  This is the composition layer's
    path-traversal and symlink-escape guard.
    """
    root = base.resolve()
    raw = Path(candidate)
    resolved = (
        raw.resolve(strict=False) if raw.is_absolute() else (root / raw).resolve(strict=False)
    )
    try:
        resolved.relative_to(root)
    except ValueError:
        raise MediaEngineError(
            f"path escapes the node scratch directory: {candidate!r} (resolved to {resolved})"
        ) from None
    return resolved


def _refuse_symlink(path: Path, label: str) -> None:
    """Refuse a path that is itself a symlink (``lstat``, not ``stat``)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MediaEngineError(f"{label} is unreadable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise MediaEngineError(f"{label} is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise MediaEngineError(f"{label} is not a regular file: {path}")


def enforce_output_limit(path: Path, limit: int = MAX_OUTPUT_BYTES) -> int:
    """Refuse (and delete) a produced file above the ceiling."""
    size = path.stat().st_size
    if size > limit:
        path.unlink(missing_ok=True)
        raise MediaEngineError(
            f"produced file exceeded the {limit}-byte ceiling ({size} bytes) and was removed"
        )
    return size


def enforce_input_limit(path: Path, limit: int = MAX_INPUT_BYTES) -> int:
    """Refuse a consumed file above the ceiling — before any process starts."""
    size = path.stat().st_size
    if size > limit:
        raise MediaEngineError(
            f"input file exceeded the {limit}-byte ceiling ({size} bytes): {path}"
        )
    return size


class MediaEngine:
    """Realise a canonical media operation with the canonical FFmpeg primitives."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        max_input_bytes: int = MAX_INPUT_BYTES,
        timeout_seconds: int = 300,
    ) -> None:
        self._ffmpeg = ffmpeg_bin or resolve_ffmpeg_bin()
        self._max_output_bytes = max_output_bytes
        self._max_input_bytes = max_input_bytes
        self._timeout = timeout_seconds

    @property
    def ffmpeg_binary(self) -> str:
        return self._ffmpeg

    def tool_versions(self) -> dict[str, str]:
        """The versions of the tools that produced the media — measured."""
        from nexus_ai_agent.creative.composition.license import ffmpeg_build_facts

        facts = ffmpeg_build_facts(self._ffmpeg)
        return {
            "ffmpeg": str(facts["version"]),
            "ffmpeg_binary": self._ffmpeg,
            "ffmpeg_buildconf_sha256": str(
                __import__("hashlib").sha256(str(facts["buildconf"]).encode()).hexdigest()
            ),
        }

    def realize(
        self,
        operation: str,
        *,
        source: Path,
        output_path: Path,
        params: dict[str, Any],
        scratch_dir: Path,
    ) -> MediaOutcome:
        """Produce the operation's output from *source*.

        ``output_path`` must resolve inside ``scratch_dir``: a node may only
        write into its own scratch directory.
        """
        destination = contained_path(scratch_dir, str(output_path))
        _refuse_symlink(source, "composition source")
        if not source.is_file():
            raise MediaEngineError(f"composition source not found: {source}")
        enforce_input_limit(source, self._max_input_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if operation == RESIZE_OPERATION:
            outcome = self._resize(source, destination, params)
        elif operation == GRAYSCALE_OPERATION:
            outcome = self._grayscale(source, destination)
        elif operation == IMAGE_TO_VIDEO_OPERATION:
            outcome = self._image_to_video(source, destination, params)
        else:
            raise MediaEngineError(
                f"no media realization for operation {operation!r}; the live registry "
                "resolved it but this engine does not implement it"
            )

        size = enforce_output_limit(destination, self._max_output_bytes)
        return MediaOutcome(
            path=str(destination),
            sha256=sha256_file(destination),
            size_bytes=size,
            width=outcome["width"],
            height=outcome["height"],
            duration_us=int(outcome.get("duration_us", 0)),
            facts={key: value for key, value in outcome.items() if key not in ("width", "height")},
        )

    # -- realizations -------------------------------------------------------

    def _resize(self, source: Path, destination: Path, params: dict[str, Any]) -> dict[str, Any]:
        width = int(params["width"])
        height = int(params["height"])
        try:
            artifact = resize_image(
                source,
                destination,
                width=width,
                height=height,
                binary=self._ffmpeg,
                timeout=self._timeout,
                overwrite=True,
            )
        except RenderError as exc:
            raise MediaEngineError(f"resize failed: {exc}") from exc
        return {
            "width": artifact.width,
            "height": artifact.height,
            "filter": "scale=lanczos",
        }

    def _grayscale(self, source: Path, destination: Path) -> dict[str, Any]:
        try:
            artifact = grayscale_image(
                source,
                destination,
                binary=self._ffmpeg,
                timeout=self._timeout,
                overwrite=True,
            )
        except RenderError as exc:
            raise MediaEngineError(f"grayscale failed: {exc}") from exc
        return {
            "width": artifact.width,
            "height": artifact.height,
            "color_space": "gray",
        }

    def _image_to_video(
        self, source: Path, destination: Path, params: dict[str, Any]
    ) -> dict[str, Any]:
        width = int(params["width"])
        height = int(params["height"])
        duration_us = int(params["duration_us"])
        fps = int(params["fps"])
        if width % 2 or height % 2:
            raise MediaEngineError(f"H.264 needs even dimensions, got {width}x{height}")
        ir = RenderIR(
            shots=(
                RenderShot(
                    evidence_id=sha256_file(source),
                    image_path=str(source),
                    duration_us=duration_us,
                ),
            ),
            template_id=RENDER_TEMPLATE_ID,
            target_duration_us=duration_us,
            width=width,
            height=height,
            fps=fps,
            crf=20,
            preset="veryfast",
            audio_bitrate="192k",
        )
        try:
            artifact = encode(
                ir,
                destination,
                binary=self._ffmpeg,
                timeout=self._timeout,
                overwrite=True,
            )
        except RenderError as exc:
            raise MediaEngineError(f"image-to-video failed: {exc}") from exc
        info = probe_video(destination, binary=self._ffmpeg)
        return {
            "width": int(artifact.width or width),
            "height": int(artifact.height or height),
            "duration_us": artifact.duration_us,
            "measured_duration_us": info.duration_us,
            "fps": fps,
            "encoder": "libx264",
            "has_audio": artifact.has_audio,
        }


__all__ = [
    "GRAYSCALE_OPERATION",
    "IMAGE_TO_VIDEO_OPERATION",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "RESIZE_OPERATION",
    "RENDER_TEMPLATE_ID",
    "MediaEngine",
    "MediaEngineError",
    "MediaOutcome",
    "contained_path",
    "enforce_input_limit",
    "enforce_output_limit",
]
