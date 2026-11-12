"""Exact timeline, content-addressed shot DAG, and fail-closed concat checks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .contracts import Continuity, KernelError


@dataclass(frozen=True, slots=True)
class StreamSignature:
    codec: str
    profile: str | None
    pixel_or_sample_format: str | None
    time_base: Fraction
    sample_rate: int | None
    channel_layout: str | None
    extradata_hash: str

    def compatible(self, other: StreamSignature) -> bool:
        return self == other


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ShotDAG:
    """Small ordered shot cache; changed payload invalidates its local edges."""

    def __init__(self, shots: Mapping[str, bytes]) -> None:
        self._shots = dict(shots)
        self._hashes = {key: content_hash(value) for key, value in shots.items()}
        self._edges: dict[tuple[str, str], Continuity] = {}

    def mutate(self, key: str, payload: bytes) -> tuple[str, ...]:
        if key not in self._shots:
            raise KeyError(key)
        self._shots[key] = payload
        self._hashes[key] = content_hash(payload)
        keys = list(self._shots)
        index = keys.index(key)
        affected = keys[max(0, index - 1) : min(len(keys), index + 2)]
        for edge in tuple(self._edges):
            if key in edge:
                del self._edges[edge]
        return tuple(affected)

    def set_continuity(self, left: str, right: str, result: Continuity) -> None:
        self._edges[(left, right)] = result

    def verify(self, left: str, right: str) -> bool:
        return self._edges.get((left, right), Continuity.UNKNOWN) is Continuity.OK

    def digest(self, key: str) -> str:
        return self._hashes[key]


def ffprobe_signature(path: str | Path, *, executable: str = "ffprobe") -> StreamSignature:
    """Probe one stream and reject missing fields rather than guessing."""
    command = [executable, "-v", "error", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1:
        raise KernelError("expected exactly one media stream")
    stream = streams[0]
    codec_type = stream.get("codec_type")
    fmt = stream.get("pix_fmt") if codec_type == "video" else stream.get("sample_fmt")
    required = (stream.get("codec_name"), stream.get("time_base"), stream.get("extradata_hash"))
    if any(value is None for value in required):
        raise KernelError("ffprobe omitted required compatibility metadata")
    tb = Fraction(str(stream["time_base"]))
    return StreamSignature(
        str(stream["codec_name"]),
        stream.get("profile"),
        fmt,
        tb,
        stream.get("sample_rate"),
        stream.get("channel_layout"),
        str(stream["extradata_hash"]),
    )


def require_compatible(signatures: Sequence[StreamSignature]) -> StreamSignature:
    if not signatures or any(not signatures[0].compatible(sig) for sig in signatures[1:]):
        raise KernelError("incompatible streams: refusing stream-copy assembly")
    return signatures[0]


@dataclass(frozen=True, slots=True)
class AssemblyReceipt:
    quality_claim: str
    sha256: str
    duration_seconds: Fraction
    packet_counts: Mapping[int, int]
    verified_splice_count: int


def _probe(path: Path, executable: str, *, packets: bool) -> dict[str, object]:
    command = [
        executable,
        "-v",
        "error",
        "-show_data_hash",
        "sha256",
        "-show_format",
        "-show_streams",
    ]
    if packets:
        command.append("-show_packets")
    command.extend(["-of", "json", str(path)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KernelError(f"ffprobe failed: {exc}") from exc
    if result.returncode != 0 or result.stderr.strip():
        raise KernelError("ffprobe returned an error")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KernelError("ffprobe returned invalid JSON") from exc
    if not isinstance(data, dict) or not data.get("streams"):
        raise KernelError("ffprobe returned no streams")
    return data


def _streams(data: Mapping[str, object]) -> list[dict[str, object]]:
    raw = data.get("streams")
    if not isinstance(raw, list) or not raw or not all(isinstance(item, dict) for item in raw):
        raise KernelError("invalid ffprobe stream list")
    return raw


def _fraction_duration(data: Mapping[str, object]) -> Fraction:
    format_data = data.get("format")
    if not isinstance(format_data, dict) or format_data.get("duration") is None:
        raise KernelError("ffprobe omitted format duration")
    try:
        duration = Fraction(str(format_data["duration"]))
    except (ValueError, ZeroDivisionError) as exc:
        raise KernelError("invalid format duration") from exc
    if duration <= 0:
        raise KernelError("media duration must be positive")
    return duration


def _stream_key(stream: Mapping[str, object]) -> tuple[object, ...]:
    codec_type = stream.get("codec_type")
    fmt = stream.get("pix_fmt") if codec_type == "video" else stream.get("sample_fmt")
    time_base = stream.get("time_base")
    if not time_base or not stream.get("codec_name") or stream.get("extradata_hash") is None:
        raise KernelError("ffprobe omitted required stream metadata")
    return (
        codec_type,
        stream.get("codec_name"),
        stream.get("profile"),
        fmt,
        stream.get("width"),
        stream.get("height"),
        stream.get("r_frame_rate"),
        Fraction(str(time_base)),
        stream.get("sample_rate"),
        stream.get("channels"),
        stream.get("channel_layout"),
        stream.get("extradata_hash"),
    )


def _check_packets(data: Mapping[str, object]) -> tuple[dict[int, int], dict[int, int]]:
    raw_packets = data.get("packets")
    if not isinstance(raw_packets, list):
        raise KernelError("ffprobe omitted packets")
    counts: dict[int, int] = {}
    last_pts: dict[int, int] = {}
    last_dts: dict[int, int] = {}
    for packet in raw_packets:
        if not isinstance(packet, dict) or packet.get("stream_index") is None:
            raise KernelError("invalid packet metadata")
        index = int(packet["stream_index"])
        counts[index] = counts.get(index, 0) + 1
        for key, previous in (("pts", last_pts), ("dts", last_dts)):
            value = packet.get(key)
            if value is None and key == "pts":
                value = packet.get("dts")
            if value is None:
                raise KernelError(f"packet missing {key}/dts timestamp")
            timestamp = int(value)
            if index in previous and timestamp < previous[index]:
                raise KernelError(f"non-monotonic {key} in stream {index}")
            previous[index] = timestamp
    return counts, last_pts


def concat_copy(
    inputs: Sequence[Path], output: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"
) -> AssemblyReceipt:
    """Fail-closed stream-copy assembly; publish only after decode and packet checks."""
    if (
        len(inputs) < 2
        or not output.is_absolute()
        or output.exists()
        or any(not path.is_absolute() for path in inputs)
    ):
        raise KernelError("absolute input paths and a new absolute destination are required")
    source_paths = [path.resolve(strict=True) for path in inputs]
    if any("'" in str(path) or "\n" in str(path) or "\r" in str(path) for path in source_paths):
        raise KernelError("unsafe path for concat manifest")
    input_data = [_probe(path, ffprobe, packets=True) for path in source_paths]
    signatures = [_streams(item) for item in input_data]
    canonical = signatures[0]
    if any(len(group) != len(canonical) for group in signatures[1:]):
        raise KernelError("stream count mismatch")
    for group in signatures:
        for expected, actual in zip(canonical, group, strict=True):
            if _stream_key(expected) != _stream_key(actual):
                raise KernelError("incompatible stream signature")
    expected_duration = sum((_fraction_duration(item) for item in input_data), Fraction(0))
    source_counts = [_check_packets(item)[0] for item in input_data]
    summed_counts: dict[int, int] = {}
    for counts in source_counts:
        for index, count in counts.items():
            summed_counts[index] = summed_counts.get(index, 0) + count
    output.parent.mkdir(parents=True, exist_ok=True)
    destination = output.resolve()
    with tempfile.TemporaryDirectory(prefix="chronos-", dir=str(destination.parent)) as temporary:
        stage_dir = Path(temporary)
        manifest = stage_dir / "inputs.ffconcat"
        staged = stage_dir / ("assembled" + destination.suffix)
        lines = ["ffconcat version 1.0", *(f"file '{path}'" for path in source_paths)]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [
            ffmpeg,
            "-y",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-map",
            "0",
            "-c",
            "copy",
            str(staged),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or result.stderr.strip() or not staged.is_file():
                raise KernelError("FFmpeg concat failed")
            decode = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(staged),
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if decode.returncode != 0 or decode.stderr.strip():
                raise KernelError("full output decode failed")
            splice_times: list[Fraction] = []
            running = Fraction(0)
            for item in input_data[:-1]:
                running += _fraction_duration(item)
                splice_times.append(running)
            for cut in splice_times:
                start = max(Fraction(0), cut - Fraction(3, 20))
                boundary = subprocess.run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-ss",
                        str(float(start)),
                        "-t",
                        "0.30",
                        "-i",
                        str(staged),
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if boundary.returncode != 0 or boundary.stderr.strip():
                    raise KernelError("splice boundary decode failed")
            assembled = _probe(staged, ffprobe, packets=True)
            output_streams = _streams(assembled)
            if len(output_streams) != len(canonical):
                raise KernelError("assembled stream count mismatch")
            for expected, actual in zip(canonical, output_streams, strict=True):
                if _stream_key(expected) != _stream_key(actual):
                    raise KernelError("assembled stream signature mismatch")
            output_counts, _ = _check_packets(assembled)
            if output_counts != summed_counts:
                raise KernelError("packet conservation failed")
            if abs(_fraction_duration(assembled) - expected_duration) > Fraction(1, 1000):
                raise KernelError("duration conservation failed")
            staged_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
            os.replace(staged, destination)
            published_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            if staged_hash != published_hash:
                destination.unlink(missing_ok=True)
                raise KernelError("published hash mismatch")
            return AssemblyReceipt(
                "NO_ADDITIONAL_REENCODE_LOSS_AT_ASSEMBLY",
                published_hash,
                expected_duration,
                output_counts,
                len(splice_times),
            )
        except Exception:
            staged.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
