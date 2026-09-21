"""The render lane: plan -> render IR -> filtergraph -> exactly one encoder call.

Wave 2c turns a composed plan into a real master file.  The lane is deliberately
staged so that everything except the last step is a *pure function* of its input:

1. :func:`render_ir_from_plan` maps the pack's plan (canonical, hashable
   parameters) into a small render IR — no FFmpeg syntax anywhere yet;
2. :func:`build_filtergraph` and :func:`build_command` derive the argv from the
   IR — deterministic, assertable in a test without executing anything;
3. :func:`encode` resolves one allow-listed binary, runs it **without a shell**,
   writes a staging file and atomically renames it onto the destination.

Design rules this module is the only place allowed to break:

* the agent never authors a filtergraph — it only picks parameters, and the
  filtergraph is derived from them here (TDD §۲.۴, master lane);
* sources are never overwritten: the encoder always writes
  ``.<name>.part`` next to the destination and ``Path.replace`` publishes it, so
  a failed render cannot leave a half-written master and an existing file is
  only replaced when the caller asked for it;
* one process per render, one timeout, no shell, no ``-y`` against a user file.

The binary is resolved in an explicit order: the caller's override, then
``NEXUS_FFMPEG_BIN``, then ``PATH``, then the ``imageio-ffmpeg`` wheel (a
dev/test convenience — the production image installs FFmpeg properly).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MICROSECONDS_PER_SECOND = 1_000_000
FFMPEG_TIMEOUT_SECONDS = 900
DEFAULT_PRESET = "veryfast"
PRODUCTION_NAME = "nagar.local.slideshow.v1"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_VIDEO_RE = re.compile(r"Video:\s*\w+.*?,\s*(\d+)x(\d+)")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*: Audio:")


class RenderError(RuntimeError):
    """The render lane could not produce a verifiable master."""


class FfmpegUnavailableError(RenderError):
    """No FFmpeg binary could be resolved."""


# ---------------------------------------------------------------------------
# IR
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MotionParams:
    zoom_from: float = 1.0
    zoom_to: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0


@dataclass(frozen=True)
class GradeParams:
    contrast: float = 1.0
    saturation: float = 1.0
    brightness: float = 0.0
    gamma: float = 1.0
    hue_degrees: float = 0.0
    temperature: float = 0.0
    vignette: float = 0.0
    grain: int = 0


@dataclass(frozen=True)
class RenderShot:
    evidence_id: str
    image_path: str
    duration_us: int
    motion: MotionParams = MotionParams()
    grade: GradeParams = GradeParams()
    transition_kind: str = "fade"
    transition_us: int = 0


@dataclass(frozen=True)
class RenderIR:
    shots: tuple[RenderShot, ...]
    template_id: str
    target_duration_us: int
    width: int
    height: int
    fps: int
    crf: int
    preset: str
    audio_bitrate: str
    audio_path: str | None = None
    audio_duration_us: int | None = None
    fade_in_us: int = 0
    fade_out_us: int = 0
    loudness_lufs: float | None = None

    @property
    def input_durations_us(self) -> tuple[int, ...]:
        """Per-input clip lengths, extended so the transitions keep the target.

        ``xfade`` consumes each transition from the tail of one clip and the head
        of the next, so both neighbours are authored half a transition longer
        than their slot.  Two properties follow, and both are asserted in the
        tests: the extensions sum to exactly the transitions consumed (the
        master lands on the duration the pack promised), and every crossfade is
        *centred* on the cut it replaces — the shot boundaries stay where the
        plan put them, which is what makes the result reproducible.
        """
        extensions = [0] * len(self.shots)
        for index, shot in enumerate(self.shots[:-1]):
            head = shot.transition_us // 2
            extensions[index] += head
            extensions[index + 1] += shot.transition_us - head
        return tuple(
            shot.duration_us + extension
            for shot, extension in zip(self.shots, extensions, strict=True)
        )

    @property
    def final_duration_us(self) -> int:
        return sum(shot.duration_us for shot in self.shots)


def _motion_from(parameters: Mapping[str, Any]) -> MotionParams:
    zoom = parameters.get("zoom") or {}
    return MotionParams(
        zoom_from=float(zoom.get("from", parameters.get("zoom_from", 1.0))),
        zoom_to=float(zoom.get("to", parameters.get("zoom_to", 1.0))),
        pan_x=float(parameters.get("pan_x", 0.0)),
        pan_y=float(parameters.get("pan_y", 0.0)),
    )


def _grade_from(parameters: Mapping[str, Any]) -> GradeParams:
    return GradeParams(
        contrast=float(parameters.get("contrast", 1.0)),
        saturation=float(parameters.get("saturation", 1.0)),
        brightness=float(parameters.get("brightness", 0.0)),
        gamma=float(parameters.get("gamma", 1.0)),
        hue_degrees=float(parameters.get("hue_degrees", 0.0)),
        temperature=float(parameters.get("temperature", 0.0)),
        vignette=float(parameters.get("vignette", 0.0)),
        grain=int(parameters.get("grain", 0) or 0),
    )


def _resolution_from(profile: Mapping[str, Any]) -> tuple[int, int]:
    raw = str(profile.get("resolution") or "1920x1080")
    width, _, height = raw.partition("x")
    if not width.isdigit() or not height.isdigit():
        raise RenderError(f"unusable resolution in the render profile: {raw!r}")
    return int(width), int(height)


def render_ir_from_plan(
    plan: Mapping[str, Any],
    evidence_paths: Mapping[str, str],
) -> RenderIR:
    """Map a composed plan into the render IR (pure, no FFmpeg syntax).

    ``evidence_paths`` maps evidence id -> file path; a shot whose media is
    missing is a hard error, because rendering a master that silently drops a
    shot would be worse than refusing to render.
    """
    profile = dict(plan.get("render_profile") or {})
    audio = dict(plan.get("audio") or {})
    shots: list[RenderShot] = []
    for shot in plan.get("shots") or ():
        evidence_id = str(shot["evidence_id"])
        path = evidence_paths.get(evidence_id)
        if not path:
            raise RenderError(f"no media path for evidence id {evidence_id!r}")
        slot = shot["slot"]
        transition = shot.get("transition_in") or {}
        shots.append(
            RenderShot(
                evidence_id=evidence_id,
                image_path=path,
                duration_us=int(slot["end_us"]) - int(slot["start_us"]),
                motion=_motion_from(shot.get("motion") or {}),
                grade=_grade_from(shot.get("color") or {}),
                transition_kind=str(transition.get("kind") or "fade"),
                transition_us=int(transition.get("duration_us") or 0),
            )
        )
    if not shots:
        raise RenderError("the plan contains no shots to render")
    width, height = _resolution_from(profile)
    return RenderIR(
        shots=tuple(shots),
        template_id=str(plan.get("template_id") or ""),
        target_duration_us=int(plan["target_duration_us"]),
        width=width,
        height=height,
        fps=int(profile.get("fps") or 30),
        crf=int(profile.get("crf") or 20),
        preset=str(profile.get("preset") or DEFAULT_PRESET),
        audio_bitrate=str(profile.get("audio_bitrate") or "192k"),
        audio_path=audio.get("path"),
        audio_duration_us=audio.get("source_duration_us"),
        fade_in_us=int(float(audio.get("fade_in_seconds") or 0.0) * MICROSECONDS_PER_SECOND),
        fade_out_us=int(float(audio.get("fade_out_seconds") or 0.0) * MICROSECONDS_PER_SECOND),
        loudness_lufs=profile.get("loudness_lufs"),
    )


def render_ir_dict(ir: RenderIR) -> dict[str, Any]:
    """The canonical, hashable description of what will be rendered."""
    return {
        "template_id": ir.template_id,
        "target_duration_us": ir.target_duration_us,
        "width": ir.width,
        "height": ir.height,
        "fps": ir.fps,
        "crf": ir.crf,
        "preset": ir.preset,
        "audio_bitrate": ir.audio_bitrate,
        "audio": None
        if ir.audio_path is None
        else {
            "path": ir.audio_path,
            "duration_us": ir.audio_duration_us,
            "fade_in_us": ir.fade_in_us,
            "fade_out_us": ir.fade_out_us,
            "loudness_lufs": ir.loudness_lufs,
        },
        "shots": [
            {
                "evidence_id": shot.evidence_id,
                "image_path": shot.image_path,
                "duration_us": shot.duration_us,
                "transition_kind": shot.transition_kind,
                "transition_us": shot.transition_us,
                "motion": [shot.motion.zoom_from, shot.motion.zoom_to, shot.motion.pan_x],
                "grade": [
                    shot.grade.contrast,
                    shot.grade.saturation,
                    shot.grade.brightness,
                    shot.grade.gamma,
                    shot.grade.hue_degrees,
                    shot.grade.temperature,
                    shot.grade.vignette,
                    shot.grade.grain,
                ],
            }
            for shot in ir.shots
        ],
    }


def render_ir_hash(ir: RenderIR) -> str:
    """Pin *what* was rendered: the IR decides the filtergraph, so hash the IR."""
    payload = json.dumps(render_ir_dict(ir), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# filtergraph (pure)
# ---------------------------------------------------------------------------


def _seconds(microseconds: int) -> str:
    return f"{microseconds / MICROSECONDS_PER_SECOND:.6f}"


def _clip_filter(ir: RenderIR, shot: RenderShot, frames: int) -> str:
    width, height = ir.width, ir.height
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={ir.fps}",
    ]
    zoom_from = max(1.0, shot.motion.zoom_from)
    zoom_to = max(zoom_from, shot.motion.zoom_to)
    if abs(zoom_to - zoom_from) < 1e-6:
        zoom_expression = f"{zoom_from:.4f}"
    else:
        step = (zoom_to - zoom_from) / max(1, frames)
        zoom_expression = f"min({zoom_from:.4f}+{step:.8f}*on,{zoom_to:.4f})"
    pan_x = shot.motion.pan_x
    pan_y = shot.motion.pan_y
    x_expression = "iw/2-(iw/zoom/2)"
    y_expression = "ih/2-(ih/zoom/2)"
    if abs(pan_x) > 1e-6:
        x_expression += f"+{pan_x:.4f}*iw*on/{max(1, frames)}"
    if abs(pan_y) > 1e-6:
        y_expression += f"+{pan_y:.4f}*ih*on/{max(1, frames)}"
    filters.append(
        f"zoompan=z='{zoom_expression}':x='{x_expression}':y='{y_expression}'"
        f":d=1:s={width}x{height}"
    )
    grade = shot.grade
    filters.append(
        f"eq=contrast={grade.contrast:.3f}:saturation={grade.saturation:.3f}"
        f":brightness={grade.brightness:.3f}:gamma={grade.gamma:.3f}"
    )
    if abs(grade.hue_degrees) > 1e-6:
        filters.append(f"hue=h={grade.hue_degrees:.2f}")
    if abs(grade.temperature) > 1e-6:
        warm = max(-1.0, min(1.0, grade.temperature)) * 0.12
        filters.append(f"colorbalance=rs={warm:.3f}:bs={-warm:.3f}")
    if grade.vignette > 1e-6:
        strength = max(0.0, min(1.0, grade.vignette))
        filters.append(f"vignette=angle=PI/{max(2.0, 8.0 - 6.0 * strength):.2f}")
    if grade.grain > 0:
        filters.append(f"noise=alls={grade.grain}:allf=t+u")
    filters.append("format=yuv420p")
    return ",".join(filters)


def build_filtergraph(ir: RenderIR) -> tuple[str, str, bool]:
    """Return ``(filtergraph, final_video_label, has_audio)`` for the IR."""
    lines: list[str] = []
    for index, (shot, duration_us) in enumerate(zip(ir.shots, ir.input_durations_us, strict=True)):
        frames = max(1, round(duration_us / MICROSECONDS_PER_SECOND * ir.fps))
        lines.append(f"[{index}:v]{_clip_filter(ir, shot, frames)}[v{index}]")

    if len(ir.shots) == 1:
        final_label = "v0"
    else:
        accumulated = ir.input_durations_us[0]
        previous_label = "v0"
        for index in range(1, len(ir.shots)):
            transition_us = ir.shots[index - 1].transition_us
            kind = ir.shots[index - 1].transition_kind or "fade"
            offset = accumulated - transition_us
            label = f"vx{index}"
            lines.append(
                f"[{previous_label}][v{index}]xfade=transition={kind}"
                f":duration={_seconds(transition_us)}:offset={_seconds(offset)}[{label}]"
            )
            accumulated = accumulated + ir.input_durations_us[index] - transition_us
            previous_label = label
        final_label = previous_label

    has_audio = ir.audio_path is not None
    if has_audio:
        total = _seconds(ir.final_duration_us)
        audio_filters = [
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
            "apad",
            f"atrim=0:{total}",
            "asetpts=PTS-STARTPTS",
        ]
        if ir.loudness_lufs is not None:
            audio_filters.append(f"loudnorm=I={float(ir.loudness_lufs):.1f}:TP=-1.5:LRA=11")
        if ir.fade_in_us > 0:
            audio_filters.append(f"afade=t=in:st=0:d={_seconds(ir.fade_in_us)}")
        if ir.fade_out_us > 0:
            start = max(0, ir.final_duration_us - ir.fade_out_us)
            audio_filters.append(f"afade=t=out:st={_seconds(start)}:d={_seconds(ir.fade_out_us)}")
        lines.append(f"[{len(ir.shots)}:a]{','.join(audio_filters)}[aout]")

    return ";".join(lines), final_label, has_audio


def build_upscale_command(
    input_path: Path,
    *,
    width: int,
    height: int,
    output_path: Path,
    binary: str = "ffmpeg",
) -> list[str]:
    """Build the allow-listed lossless still-image upscale argv."""
    if width < 2 or height < 2 or width > 8192 or height > 8192:
        raise RenderError(f"upscale dimensions outside 2..8192: {width}x{height}")
    if width * height > 33_177_600:  # bounded at roughly 8K UHD pixels
        raise RenderError(f"upscale exceeds the 8K pixel budget: {width}x{height}")
    return [
        binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-vf",
        f"scale={width}:{height}:flags=lanczos",
        "-frames:v",
        "1",
        "-c:v",
        "png",
        "-compression_level",
        "6",
        "-y",
        str(output_path),
    ]


def build_command(ir: RenderIR, *, output_path: Path, binary: str = "ffmpeg") -> list[str]:
    """The exact argv the encoder will run — assertable without running it."""
    filtergraph, final_label, has_audio = build_filtergraph(ir)
    args: list[str] = [binary, "-hide_banner", "-nostdin", "-loglevel", "error"]
    for shot, duration_us in zip(ir.shots, ir.input_durations_us, strict=True):
        args += [
            "-loop",
            "1",
            "-framerate",
            str(ir.fps),
            "-t",
            _seconds(duration_us),
            "-i",
            shot.image_path,
        ]
    if has_audio:
        args += ["-i", str(ir.audio_path)]
    args += ["-filter_complex", filtergraph, "-map", f"[{final_label}]"]
    if has_audio:
        args += ["-map", "[aout]"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        ir.preset,
        "-crf",
        str(ir.crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(ir.fps),
        "-movflags",
        "+faststart",
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", ir.audio_bitrate]
    args += ["-t", _seconds(ir.final_duration_us), "-y", str(output_path)]
    return args


# ---------------------------------------------------------------------------
# the one place that runs a process
# ---------------------------------------------------------------------------


def resolve_ffmpeg_bin(explicit: str | None = None) -> str:
    """Resolve the allow-listed encoder: override, env, PATH, then the wheel."""
    candidates = [explicit, os.environ.get("NEXUS_FFMPEG_BIN")]
    for candidate in candidates:
        if candidate:
            if Path(candidate).is_file():
                return candidate
            raise FfmpegUnavailableError(f"NEXUS_FFMPEG_BIN is not a file: {candidate!r}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:  # pragma: no cover - depends on the environment
        raise FfmpegUnavailableError(
            "no FFmpeg binary found: install ffmpeg, set NEXUS_FFMPEG_BIN, or "
            "install the 'imageio-ffmpeg' package for a bundled static build "
            f"({error})"
        ) from error


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass(frozen=True)
class VideoInfo:
    duration_us: int
    width: int | None
    height: int | None
    has_audio: bool


@dataclass(frozen=True)
class UpscaleArtifact:
    """A lossless PNG produced by the local Lanczos stage."""

    path: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    binary: str


@dataclass(frozen=True)
class RenderArtifact:
    """What the encoder actually produced — measured, not assumed."""

    path: str
    sha256: str
    size_bytes: int
    duration_us: int
    width: int | None
    height: int | None
    has_audio: bool
    render_ir_hash: str
    binary: str
    encoder: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return {
            "output_path": self.path,
            "output_sha256": self.sha256,
            "duration_us": self.duration_us,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "render_ir_hash": self.render_ir_hash,
            "encoder": self.encoder,
        }


def probe_video(path: Path, *, binary: str) -> VideoInfo:
    """Read duration/size/streams back out of a produced file with FFmpeg itself.

    ``ffprobe`` is a separate binary that a minimal install may not ship, so the
    evidence about a master comes from the same allow-listed binary that wrote
    it — ``ffmpeg -i`` prints the stream table on stderr and exits non-zero
    because no output was requested.
    """
    result = _run([binary, "-hide_banner", "-nostdin", "-i", str(path)], timeout=120)
    text = result.stderr or ""
    match = _DURATION_RE.search(text)
    if match is None:
        raise RenderError(f"could not read a duration back from {path}: {text[-400:]}")
    hours, minutes, seconds = match.groups()
    duration_us = int(
        round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * MICROSECONDS_PER_SECOND)
    )
    video = _VIDEO_RE.search(text)
    size = None
    if video is not None:
        size = (int(video.group(1)), int(video.group(2)))
    return VideoInfo(
        duration_us=duration_us,
        width=size[0] if size else None,
        height=size[1] if size else None,
        has_audio=bool(_AUDIO_STREAM_RE.search(text)),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def upscale_image(
    input_path: Path,
    output_path: Path,
    *,
    width: int,
    height: int,
    binary: str | None = None,
    timeout: int = 120,
    overwrite: bool = False,
) -> UpscaleArtifact:
    """Upscale one still with Lanczos into an atomically published PNG."""
    source = Path(input_path)
    if not source.is_file():
        raise RenderError(f"upscale input not found: {source}")
    destination = Path(output_path)
    if destination.suffix.lower() != ".png":
        raise RenderError("upscale output must use .png to avoid lossy re-encoding")
    if source.resolve() == destination.resolve():
        raise RenderError("upscale never overwrites its source image")
    if destination.exists() and not overwrite:
        raise RenderError(f"{destination} already exists; pass overwrite=True")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.stem}.part.png")
    staging.unlink(missing_ok=True)
    resolved = resolve_ffmpeg_bin(binary)
    args = build_upscale_command(
        source, width=width, height=height, output_path=staging, binary=resolved
    )
    try:
        result = _run(args, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        staging.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg upscale timed out after {timeout}s") from error
    if result.returncode != 0 or not staging.is_file():
        staging.unlink(missing_ok=True)
        raise RenderError(
            f"ffmpeg upscale exited {result.returncode}: {(result.stderr or '').strip()[-600:]}"
        )
    staging.replace(destination)
    return UpscaleArtifact(
        path=str(destination),
        sha256=sha256_file(destination),
        size_bytes=destination.stat().st_size,
        width=width,
        height=height,
        binary=resolved,
    )


def encode(
    ir: RenderIR,
    output_path: Path,
    *,
    binary: str | None = None,
    timeout: int = FFMPEG_TIMEOUT_SECONDS,
    overwrite: bool = False,
) -> RenderArtifact:
    """Render the IR to ``output_path`` through exactly one FFmpeg process.

    The encoder writes ``.<name>.part.<ext>`` and the published file appears by
    an atomic rename, so a crash or a timeout never leaves a half-written master
    and an existing file is protected unless ``overwrite`` says otherwise.
    """
    resolved = resolve_ffmpeg_bin(binary)
    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise RenderError(
            f"{destination} already exists; pass overwrite=True (or choose another "
            "path) — a render never silently replaces a file"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the container extension: FFmpeg picks the muxer by extension.
    staging = destination.with_name(f".{destination.stem}.part{destination.suffix}")
    staging.unlink(missing_ok=True)

    args = build_command(ir, output_path=staging, binary=resolved)
    try:
        result = _run(args, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        staging.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg timed out after {timeout}s") from error
    if result.returncode != 0 or not staging.is_file():
        staging.unlink(missing_ok=True)
        raise RenderError(
            f"ffmpeg exited {result.returncode}: {(result.stderr or '').strip()[-600:]}"
        )
    staging.replace(destination)

    info = probe_video(destination, binary=resolved)
    return RenderArtifact(
        path=str(destination),
        sha256=sha256_file(destination),
        size_bytes=destination.stat().st_size,
        duration_us=info.duration_us,
        width=info.width,
        height=info.height,
        has_audio=info.has_audio,
        render_ir_hash=render_ir_hash(ir),
        binary=resolved,
        encoder={
            "video_codec": "libx264",
            "audio_codec": "aac" if info.has_audio else None,
            "crf": ir.crf,
            "preset": ir.preset,
            "fps": ir.fps,
            "resolution": f"{ir.width}x{ir.height}",
            "produced_by": PRODUCTION_NAME,
        },
    )


__all__ = [
    "FFMPEG_TIMEOUT_SECONDS",
    "FfmpegUnavailableError",
    "GradeParams",
    "MotionParams",
    "PRODUCTION_NAME",
    "RenderArtifact",
    "RenderError",
    "RenderIR",
    "RenderShot",
    "UpscaleArtifact",
    "VideoInfo",
    "build_command",
    "build_filtergraph",
    "build_upscale_command",
    "encode",
    "probe_video",
    "render_ir_dict",
    "render_ir_from_plan",
    "render_ir_hash",
    "resolve_ffmpeg_bin",
    "sha256_file",
    "upscale_image",
]
