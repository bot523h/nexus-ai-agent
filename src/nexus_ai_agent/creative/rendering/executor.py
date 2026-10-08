"""The one place that runs a process: measure pass + single-process encode.

Rules (mirroring the proven Wave 2c lane):

* the binary is the same allow-listed resolution (override → ``NEXUS_FFMPEG_BIN``
  → ``PATH`` → ``imageio-ffmpeg`` wheel);
* no shell, one timeout, one process per call;
* the encoder writes ``.<name>.part.<ext>`` and publishes by atomic rename, so
  a crash never leaves a half-written master and an existing file is only
  replaced when ``overwrite=True`` says so;
* evidence comes from probing the produced file with the same binary.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.rendering.compiler import (
    CompiledLane,
    MeasuredLoudness,
    compile_lane,
    compile_measure,
)
from nexus_ai_agent.creative.rendering.ir import LaneError, LaneIR
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    RenderError,
    probe_video,
    resolve_ffmpeg_bin,
    sha256_file,
)

FFMPEG_TIMEOUT_SECONDS = 900
MEASURE_TIMEOUT_SECONDS = 600


class LaneExecutionError(RenderError):
    """The lane ran FFmpeg but could not produce a verifiable master."""


@dataclass(frozen=True)
class LaneArtifact:
    """What the lane actually produced — measured, not assumed."""

    path: str
    sha256: str
    size_bytes: int
    duration_us: int
    width: int | None
    height: int | None
    has_audio: bool
    lane_ir_hash: str
    ops: tuple[str, ...]
    binary: str
    encoder: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return {
            "output_path": self.path,
            "output_sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "duration_us": self.duration_us,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "lane_ir_hash": self.lane_ir_hash,
            "ops": list(self.ops),
            "encoder": self.encoder,
        }


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def measure_loudness(
    ir: LaneIR,
    *,
    binary: str | None = None,
    timeout: int = MEASURE_TIMEOUT_SECONDS,
    fontfile: str | None = None,
) -> MeasuredLoudness:
    """First pass of EBU R128: decode + filter to null, parse the JSON summary."""
    resolved = resolve_ffmpeg_bin(binary)
    compiled = compile_measure(ir, fontfile=fontfile)
    try:
        result = _run(compiled.measure_argv(binary=resolved), timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise LaneExecutionError(f"loudness measure pass timed out after {timeout}s") from error
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LaneExecutionError(
            "loudness measure pass produced no JSON summary: "
            f"{(result.stderr or '').strip()[-600:]}"
        )
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise LaneExecutionError(f"could not parse loudness JSON: {error}") from error
    try:
        return MeasuredLoudness.model_validate(payload)
    except ValueError as error:
        raise LaneExecutionError(f"loudness JSON missing required fields: {error}") from error


def encode_lane(
    compiled: CompiledLane,
    output_path: Path,
    *,
    binary: str | None = None,
    timeout: int = FFMPEG_TIMEOUT_SECONDS,
    overwrite: bool = False,
) -> LaneArtifact:
    """Render one compiled lane through exactly one FFmpeg process."""
    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise LaneExecutionError(
            f"{destination} already exists; pass overwrite=True (or choose another "
            "path) — a render never silently replaces a file"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.stem}.part{destination.suffix}")
    staging.unlink(missing_ok=True)

    resolved = resolve_ffmpeg_bin(binary)
    try:
        result = _run(compiled.argv(staging, binary=resolved), timeout=timeout)
    except subprocess.TimeoutExpired as error:
        staging.unlink(missing_ok=True)
        raise LaneExecutionError(f"ffmpeg timed out after {timeout}s") from error
    if result.returncode != 0 or not staging.is_file():
        staging.unlink(missing_ok=True)
        raise LaneExecutionError(
            f"ffmpeg exited {result.returncode}: {(result.stderr or '').strip()[-600:]}"
        )
    staging.replace(destination)

    info = probe_video(destination, binary=resolved)
    return LaneArtifact(
        path=str(destination),
        sha256=sha256_file(destination),
        size_bytes=destination.stat().st_size,
        duration_us=info.duration_us,
        width=info.width,
        height=info.height,
        has_audio=info.has_audio,
        lane_ir_hash=compiled.ir_hash,
        ops=(),
        binary=resolved,
        encoder={
            "video_codec": "libx264" if compiled.video_out else None,
            "audio_codec": "aac" if compiled.audio_out else None,
            "produced_by": "nagar.local.apply-lane.v1",
        },
    )


def render_lane(
    ir: LaneIR,
    output_path: Path,
    *,
    binary: str | None = None,
    timeout: int = FFMPEG_TIMEOUT_SECONDS,
    overwrite: bool = False,
    fontfile: str | None = None,
) -> LaneArtifact:
    """Convenience: measure (if a loudnorm op exists) then single-process encode."""
    measured: MeasuredLoudness | None = None
    if any(getattr(op, "op", None) == "loudnorm" for op in ir.ops):
        measured = measure_loudness(ir, binary=binary, fontfile=fontfile)
    compiled = compile_lane(ir, measured=measured, fontfile=fontfile)
    artifact = encode_lane(
        compiled, output_path, binary=binary, timeout=timeout, overwrite=overwrite
    )
    return LaneArtifact(
        path=artifact.path,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        duration_us=artifact.duration_us,
        width=artifact.width,
        height=artifact.height,
        has_audio=artifact.has_audio,
        lane_ir_hash=artifact.lane_ir_hash,
        ops=tuple(getattr(op, "op", "?") for op in ir.ops),
        binary=artifact.binary,
        encoder=artifact.encoder,
    )


__all__ = [
    "FFMPEG_TIMEOUT_SECONDS",
    "FfmpegUnavailableError",
    "LaneArtifact",
    "LaneError",
    "LaneExecutionError",
    "LaneIR",
    "MEASURE_TIMEOUT_SECONDS",
    "encode_lane",
    "measure_loudness",
    "render_lane",
]
