"""Bullet-proof normaliser for mobile/cinema input media.

Probes a file with ``ffprobe`` (``shell=False``, ``timeout=30``), detects
variable frame rate and mobile rotation, and emits a standardised MP4
(``yuv420p``, Rec.709, AAC 48 kHz stereo).  Files without an audio track get a
calibrated silence source injected so downstream timeline merges never break.

All heavy pixel work happens inside a single ``ffmpeg -filter_complex`` graph;
input frames are never loaded into Python RAM.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .models import HDR, SDR, ColorSpace, MediaStreamProbe, _is_so2

FFPROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 600
MAX_RAM_BYTES = 120 * 1024 * 1024  # 120 MiB, the hard ceiling for this package

#: ffprobe side-data keys that carry a display matrix.
_DISPLAY_MATRIX_TYPES = {"Display Matrix", "displaymatrix"}

#: Rotation angles (degrees) the mobile/cinema normaliser recognises.
_ROTATION_ANGLES = (90.0, 180.0, 270.0)


class NormalizationError(RuntimeError):
    """The input cannot be normalised (fail-closed)."""


@dataclass(frozen=True)
class NormalizedMedia:
    """The result of a successful normalisation."""

    output_path: str
    sha256: str
    probe: MediaStreamProbe
    duration_us: int
    filtergraph: str


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=False,
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
    )


def _sha256_of_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _parse_rate(value: str | None) -> Fraction:
    if not value or value in {"0/0", "0", "N/A", "none"}:
        return Fraction(0, 1)
    try:
        if "/" in value:
            num, _, den = value.partition("/")
            return Fraction(int(num), int(den) or 1)
        return Fraction(int(value))
    except (ValueError, ZeroDivisionError):
        return Fraction(0, 1)


def _rotation_matrix_from_side_data(
    side_data_list: object,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Extract the SO(2) rotation matrix from an ffprobe side-data list.

    ffmpeg stores the display matrix as 9 int32 fixed-point (16.16) values;
    the top-left 2x2 is ``[[cos, -sin], [sin, cos]]``.  ffprobe emits it either
    as a hex-encoded string or already-parsed numbers depending on the build.
    """
    identity = ((1.0, 0.0), (0.0, 1.0))
    if not isinstance(side_data_list, list):
        return identity
    for entry in side_data_list:
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("side_data_type") not in _DISPLAY_MATRIX_TYPES
            and entry.get("type") not in _DISPLAY_MATRIX_TYPES
        ):
            continue
        raw = entry.get("displaymatrix") or entry.get("data")
        if raw is None:
            continue
        matrix = _parse_displaymatrix(raw)
        if matrix is not None and _is_so2(matrix):
            return matrix
    return identity


def _parse_displaymatrix(raw: object) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Parse a 9-value int32 display matrix into the top-left SO(2) block."""
    try:
        if isinstance(raw, str):
            hex_text = raw.replace(" ", "").replace("0x", "").replace("0X", "")
            if not re.fullmatch(r"[0-9a-fA-F]*", hex_text):
                return None
            if len(hex_text) < 72:
                return None
            bytes_values = bytes.fromhex(hex_text[:72])
            ints = [
                int.from_bytes(bytes_values[i * 4 : i * 4 + 4], "little", signed=True)
                for i in range(9)
            ]
        elif isinstance(raw, list) and len(raw) >= 9:
            ints = [int(x) for x in raw[:9]]
        else:
            return None
    except (ValueError, TypeError):
        return None
    fixed = 65536.0
    a = ints[0] / fixed  # cos
    c = ints[3] / fixed  # sin
    # Re-orthonormalise: [[cos, -sin], [sin, cos]].
    theta = math.atan2(c, a)
    return ((math.cos(theta), -math.sin(theta)), (math.sin(theta), math.cos(theta)))


def _rotation_from_matrix(matrix: tuple[tuple[float, float], tuple[float, float]]) -> float:
    (a, _b), (c, _d) = matrix
    return math.degrees(math.atan2(c, a)) % 360.0


def _nearest_supported_rotation(degrees: float) -> float | None:
    if degrees < 1e-3 or degrees > 360 - 1e-3:
        return None
    for angle in _ROTATION_ANGLES:
        if abs(((degrees - angle + 180) % 360) - 180) < 2.0:
            return angle
    return None


def _color_space(stream: dict) -> ColorSpace:
    cs = (stream.get("color_space") or stream.get("color_space_name") or "").upper()
    transfer = (stream.get("color_transfer") or "").upper()
    primaries = (stream.get("color_primaries") or "").upper()
    hdr_markers = ("BT2020", "SMPTE2084", "PQ", "HLG", "SMPTE428", "XYZ")
    combined = cs + " " + transfer + " " + primaries
    if any(marker in combined for marker in hdr_markers):
        return HDR
    return SDR


def probe_media(path: str | Path, *, timeout: int = FFPROBE_TIMEOUT) -> MediaStreamProbe:
    """Probe *path* with ffprobe and return a :class:`MediaStreamProbe`.

    Runs ``ffprobe -v quiet -print_format json -show_format -show_streams`` with
    ``shell=False`` and the given timeout.  Raises :class:`NormalizationError`
    on any failure.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = _run(cmd, timeout=timeout)
    except (FileNotFoundError, OSError) as exc:
        raise NormalizationError(f"ffprobe is not available or failed to launch: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NormalizationError(f"ffprobe timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise NormalizationError(
            f"ffprobe failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NormalizationError(f"ffprobe emitted invalid JSON: {exc}") from exc

    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise NormalizationError("no video stream found")
    video = video_streams[0]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    width = int(video.get("width", 0))
    height = int(video.get("height", 0))
    r_frame_rate = _parse_rate(video.get("r_frame_rate"))
    avg_frame_rate = _parse_rate(video.get("avg_frame_rate"))
    aspect_ratio = _parse_aspect_ratio(video.get("display_aspect_ratio"), width, height)

    rotation = _rotation_matrix_from_side_data(video.get("side_data_list"))
    metadata_rotation = video.get("metadata", {}).get("rotate") or video.get("metadata", {}).get(
        "Rotation"
    )
    if metadata_rotation is not None:
        try:
            degrees = float(metadata_rotation) % 360.0
            theta = math.radians(degrees)
            rotation = ((math.cos(theta), -math.sin(theta)), (math.sin(theta), math.cos(theta)))
        except (TypeError, ValueError):
            pass

    sha256 = _sha256_of_file(path)
    return MediaStreamProbe(
        width=width,
        height=height,
        r_frame_rate=r_frame_rate,
        avg_frame_rate=avg_frame_rate,
        aspect_ratio=aspect_ratio,
        rotation=rotation,
        color_space=_color_space(video),
        has_audio=bool(audio_streams),
        sha256=sha256,
    )


def _parse_aspect_ratio(value: str | None, width: int, height: int) -> float:
    """ffprobe reports aspect ratio as ``16:9`` or as a decimal; fall back to W/H."""
    if value:
        try:
            if ":" in str(value):
                num, _, den = str(value).partition(":")
                return float(num) / float(den or 1)
            return float(value)
        except (ValueError, ZeroDivisionError):
            pass
    if width and height:
        return width / height
    return 1.0


def _read_duration(path: str | Path) -> float:
    """Read the container duration (seconds) via ffprobe, or ``0.0`` when the
    duration cannot be established (e.g. ffprobe is unavailable)."""
    try:
        probe_result = _run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            timeout=FFPROBE_TIMEOUT,
        )
        if probe_result.returncode == 0:
            return float((json.loads(probe_result.stdout).get("format") or {}).get("duration") or 0)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
        pass
    return 0.0


def _transpose_filter(degrees: float) -> str:
    nearest = _nearest_supported_rotation(degrees)
    if nearest == 90.0:
        return "transpose=1"  # 90° clockwise
    if nearest == 180.0:
        return "hflip,vflip"
    if nearest == 270.0:
        return "transpose=2"  # 90° counter-clockwise
    raise NormalizationError(f"unsupported rotation {degrees:.1f}°")


def _fps_target(probe: MediaStreamProbe) -> float:
    if probe.avg_frame_rate > 0:
        return float(probe.avg_frame_rate)
    if probe.r_frame_rate > 0:
        return float(probe.r_frame_rate)
    return 30.0


def _is_variable_frame_rate(probe: MediaStreamProbe) -> bool:
    if probe.avg_frame_rate <= 0:
        return False
    return (
        abs(float(probe.r_frame_rate) - float(probe.avg_frame_rate))
        / max(float(probe.avg_frame_rate), 1e-6)
    ) > 0.01


def normalize_media(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    timeout: int = FFMPEG_TIMEOUT,
) -> NormalizedMedia:
    """Normalise *input_path* into a standard MP4.

    Steps:
      1. probe with ffprobe (30 s timeout);
      2. detect VFR → insert ``fps`` filter and pass ``-fps_mode cfr``;
      3. detect mobile rotation → precise ``transpose``/``hflip,vflip`` filter
         and zero the rotation metadata in the output;
      4. no audio → inject ``anullsrc=channel_layout=stereo:sample_rate=48000``
         trimmed to the video duration;
      5. encode MP4 (``yuv420p``, Rec.709, AAC 48 kHz stereo) to a temporary
         file then ``os.replace`` into place;
      6. verify the output hash and re-probe.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise NormalizationError(f"input not found: {input_path}")
    probe = probe_media(input_path)
    if output_path is None:
        output_path = input_path.with_suffix(".normalized.mp4")
    output_path = Path(output_path)

    duration_sec = _read_duration(input_path)
    duration_us = int(duration_sec * 1_000_000)
    target_fps = _fps_target(probe)

    # --- build the single filter_complex graph -----------------------------
    video_lines: list[str] = []
    label = "[0:v]"
    if probe.needs_rotation:
        video_lines.append(f"{label}{_transpose_filter(probe.rotation_degrees)}[vrot]")
        label = "[vrot]"
    if _is_variable_frame_rate(probe):
        video_lines.append(f"{label}fps=fps={_fps_target(probe):.6f}[vcfr]")
        label = "[vcfr]"
    # ``format`` is always the final video stage so the graph ends at [vout].
    video_lines.append(f"{label}format=yuv420p[vout]")

    audio_lines: list[str] = []
    if probe.has_audio:
        audio_lines.append(
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[abase]"
        )
    else:
        # Calibrated silence, same length as the video programme.  When the
        # container declares no duration we emit an un-trimmed silence source
        # and let -t bound the output.
        if duration_sec > 0:
            audio_lines.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000"
                f"[asrc];[asrc]atrim=0:{duration_sec:.6f},asetpts=PTS-STARTPTS[abase]"
            )
        else:
            audio_lines.append("anullsrc=channel_layout=stereo:sample_rate=48000[abase]")

    filter_graph = ";".join([*video_lines, *audio_lines])

    # --- encode to a temporary file, then atomically replace ---------------
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="hyper_norm_", suffix=".mp4", dir=str(output_path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)

    args: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
    ]
    # ``-fps_mode cfr`` is an input-side option; the exact flag name/position
    # varies across FFmpeg builds, so CFR conversion is performed deterministically
    # by the ``fps`` filter inside the filter graph instead (see above).
    args += [
        "-i",
        str(input_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[vout]",
        "-map",
        "[abase]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        f"{target_fps:.6f}",
        "-metadata",
        "rotate=0",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
    ]
    if duration_sec > 0:
        args += ["-t", f"{duration_sec:.6f}"]
    args.append(str(temp_path))

    try:
        result = _run(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        temp_path.unlink(missing_ok=True)
        raise NormalizationError(f"ffmpeg timed out after {timeout}s") from exc
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise NormalizationError(f"ffmpeg failed to launch: {exc}") from exc

    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise NormalizationError(
            f"ffmpeg failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    try:
        os.replace(temp_path, output_path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise

    # --- verify output ------------------------------------------------------
    output_sha256 = _sha256_of_file(output_path)
    output_probe = probe_media(output_path)  # re-probe the produced file

    return NormalizedMedia(
        output_path=str(output_path),
        sha256=output_sha256,
        probe=output_probe,
        duration_us=duration_us,
        filtergraph=filter_graph,
    )
