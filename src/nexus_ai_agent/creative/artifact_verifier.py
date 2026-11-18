"""Independent post-render verifier for MP4 artifacts on disk.

FFmpeg's exit code is never evidence of a good output. This verifier re-reads
the file that is actually on disk and returns exactly one of three statuses:

* ``PASS`` — every check below held.
* ``VIOLATED`` — evidence of a defect: not a regular file, a symlink, empty,
  a broken ISO-BMFF box layout, no video stream, no audio stream when one is
  required, or a duration outside the tolerance.
* ``UNOBSERVED`` — the verifier could not observe the file: ``ffprobe`` is
  missing, timed out, or printed unparseable output.

Anything other than ``PASS`` must be treated as a failure by the caller.

Checks, in order:

1. ``os.lstat``: regular file, not a symlink, size > 0.
2. Streaming SHA-256 over 64 KiB chunks (``output_sha256``).
3. ISO-BMFF top-level box walk: ``ftyp`` first, ``moov`` and ``mdat`` present,
   box sizes exactly tile the file. Only 8–16 byte headers are read; payloads
   are skipped with ``seek``.
4. ``moov`` stream table walk: inside the ``moov`` box every ``trak`` is parsed
   down to ``mdia`` so the container itself declares its streams - the handler
   type from ``hdlr``, the first sample-entry format from ``stbl/stsd`` and the
   ``mdhd`` timescale. A video track is ``vide``; an audio track is ``soun``
   with the AAC format ``mp4a`` and a positive timescale. The studio pipeline
   always muxes AAC audio next to H.264 video, so ``require_audio=True`` (the
   default) reports ``missing_audio_stream`` for a silent MP4 even when
   ``ffprobe`` would happily call it valid.
5. ``ffprobe -print_format json``: a video stream exists, and the container
   duration is finite and positive and within ``tolerance_s`` of the expected
   total duration.
"""

from __future__ import annotations

import json
import math
import os
import stat
import struct
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.creative.materializer import stream_sha256

PASS = "PASS"
VIOLATED = "VIOLATED"
UNOBSERVED = "UNOBSERVED"
_REQUIRED_BOXES = ("ftyp", "moov", "mdat")
_MAX_FFPROBE_JSON_BYTES = 1_048_576
#: A ``moov`` box is metadata; anything near this size is a malformed file.
_MAX_MOOV_BYTES = 4 * 1024 * 1024
#: Handler type of a video track.
VIDEO_HANDLER = "vide"
#: Handler type of an audio track.
AUDIO_HANDLER = "soun"
#: Sample-entry format of AAC audio in an MP4 sample description.
AUDIO_CODEC = "mp4a"


@dataclass(frozen=True)
class Mp4Verdict:
    status: str
    output_sha256: str = ""
    size_bytes: int = 0
    probed_duration_s: float = 0.0
    has_video: bool = False
    boxes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)
    #: Sample-entry format of the container's first video track (``avc1``, ...).
    video_codec: str | None = None
    #: True when the container declares an AAC audio track with a positive timescale.
    has_audio_stream: bool = False
    #: Sample-entry format of the container's first audio track (``mp4a``).
    audio_codec: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == PASS


def _walk_top_level(path: Path) -> tuple[list[tuple[str, int, int]], list[str]]:
    """Walk the top-level boxes, returning ``(type, payload offset, payload length)``.

    Only 8-16 byte headers are read; every payload is skipped with ``seek``.
    A size of 1 means a 64-bit ``largesize`` follows; a size of 0 means "to end
    of file" and is only valid for the last box.
    """
    problems: list[str] = []
    boxes: list[tuple[str, int, int]] = []
    file_size = os.lstat(path).st_size
    offset = 0
    with open(path, "rb") as handle:
        while offset < file_size:
            handle.seek(offset)
            header = handle.read(8)
            if len(header) < 8:
                problems.append(f"truncated box header at {offset}")
                break
            size, kind_raw = struct.unpack(">I4s", header)
            kind = kind_raw.decode("latin-1")
            header_len = 8
            if size == 1:
                large = handle.read(8)
                if len(large) < 8:
                    problems.append(f"truncated largesize at {offset}")
                    break
                (size,) = struct.unpack(">Q", large)
                header_len = 16
            elif size == 0:
                size = file_size - offset
            if size < header_len or offset + size > file_size:
                problems.append(f"box {kind!r} at {offset} overruns the file")
                break
            boxes.append((kind, offset + header_len, size - header_len))
            offset += size
    # Unreachable by construction: the loop exits either at ``offset == file_size``
    # or through a recorded overrun problem, so tiling is already proven above.
    # Kept as an explicit assertion of the invariant for future edits.
    if offset != file_size and not problems:  # pragma: no cover - structurally unreachable
        problems.append("boxes do not tile the file")
    return boxes, problems


def read_top_level_boxes(path: Path) -> tuple[tuple[str, ...], list[str]]:
    """Walk the top-level ISO-BMFF boxes without loading payloads.

    Returns the box types and a list of problems (empty when the layout is
    consistent). A size of 1 means a 64-bit ``largesize`` follows; a size of 0
    means "to end of file" and is only valid for the last box.
    """
    boxes, problems = _walk_top_level(path)
    types = [kind for kind, _, _ in boxes]
    if types and types[0] != "ftyp":
        problems.append("first box is not ftyp")
    for required in _REQUIRED_BOXES:
        if required not in types:
            problems.append(f"missing required box {required!r}")
    return tuple(types), problems


@dataclass(frozen=True)
class MoovTrack:
    """One ``trak`` of an MP4 ``moov`` box, as far as a verifier needs to know it."""

    handler: str
    codec: str | None
    timescale: int

    @property
    def is_video(self) -> bool:
        return self.handler == VIDEO_HANDLER

    @property
    def is_aac_audio(self) -> bool:
        """A real audio track: ``soun`` handler, ``mp4a`` entry, non-zero timescale."""
        return self.handler == AUDIO_HANDLER and self.codec == AUDIO_CODEC and self.timescale > 0


def _iter_boxes(buffer: bytes, start: int, end: int) -> Iterator[tuple[str, int, int]]:
    """Yield ``(type, payload start, payload end)`` for the boxes in ``buffer[start:end]``.

    A nested box that does not fit is not yielded, so a truncated inner box can
    never be mistaken for a stream declaration (the caller reports the gap).
    """
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(buffer[offset : offset + 4], "big")
        kind = buffer[offset + 4 : offset + 8].decode("latin-1")
        header_len = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = int.from_bytes(buffer[offset + 8 : offset + 16], "big")
            header_len = 16
        elif size == 0:
            size = end - offset
        if size < header_len or offset + size > end:
            return
        yield kind, offset + header_len, offset + size
        offset += size


def _sample_format(buffer: bytes, start: int, end: int) -> str | None:
    """Return the first sample-entry format inside a ``minf`` box (``avc1``/``mp4a``)."""
    for kind, inner_start, inner_end in _iter_boxes(buffer, start, end):
        if kind != "stbl":
            continue
        for entry, entry_start, entry_end in _iter_boxes(buffer, inner_start, inner_end):
            if entry != "stsd":
                continue
            # version/flags (4) + entry_count (4) + entry_size (4) + format (4)
            if entry_end - entry_start >= 16:
                return buffer[entry_start + 12 : entry_start + 16].decode("latin-1")
            return None
    return None


def _parse_trak(buffer: bytes, start: int, end: int) -> MoovTrack:
    """Parse one ``trak``: handler from ``hdlr``, codec from ``stsd``, ``mdhd`` timescale."""
    handler = ""
    codec: str | None = None
    timescale = 0
    for kind, media_start, media_end in _iter_boxes(buffer, start, end):
        if kind != "mdia":
            continue
        for entry, inner_start, inner_end in _iter_boxes(buffer, media_start, media_end):
            if entry == "hdlr" and inner_end - inner_start >= 12:
                handler = buffer[inner_start + 8 : inner_start + 12].decode("latin-1")
            elif entry == "mdhd":
                # version/flags (4) + creation (4|8) + modification (4|8) + timescale (4)
                offset = inner_start + (20 if buffer[inner_start] == 1 else 12)
                if inner_end - offset >= 4:
                    timescale = int.from_bytes(buffer[offset : offset + 4], "big")
            elif entry == "minf":
                found = _sample_format(buffer, inner_start, inner_end)
                if found is not None:
                    codec = found
    return MoovTrack(handler=handler, codec=codec, timescale=timescale)


def read_moov_tracks(path: Path) -> tuple[tuple[MoovTrack, ...], list[str]]:
    """Read the stream table out of the ``moov`` box; report layout problems.

    Returns the parsed tracks (empty when the file declares none) and a list of
    problems. This is the container's own statement about its streams, checked
    without trusting ``ffprobe`` and without decoding a single frame: it is what
    lets the verifier refuse a silent MP4 instead of accepting it.
    """
    boxes, _ = _walk_top_level(path)
    moov = next((box for box in boxes if box[0] == "moov"), None)
    if moov is None:
        return (), []
    _, payload_start, payload_len = moov
    if payload_len <= 0 or payload_len > _MAX_MOOV_BYTES:
        return (), [f"moov payload size {payload_len} out of range"]
    with open(path, "rb") as handle:
        handle.seek(payload_start)
        buffer = handle.read(payload_len)
    tracks = [
        _parse_trak(buffer, start, end)
        for kind, start, end in _iter_boxes(buffer, 0, len(buffer))
        if kind == "trak"
    ]
    if not tracks:
        return (), ["moov declares no trak box"]
    return tuple(tracks), []


def _probe(path: Path, ffprobe: str, timeout_s: float) -> dict[str, object] | None:
    """Run ffprobe without a shell. ``None`` means it could not observe the file."""
    argv = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > _MAX_FFPROBE_JSON_BYTES:
        return None
    try:
        parsed = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def verify_mp4(
    path: Path,
    *,
    expected_duration_s: float,
    tolerance_s: float,
    ffprobe: str = "ffprobe",
    timeout_s: float = 60.0,
    require_audio: bool = True,
) -> Mp4Verdict:
    """Independently verify the MP4 at ``path``. Never raises for file content.

    ``require_audio`` is ``True`` by default because every studio render muxes an
    AAC track beside the video: a file without one is a truncated pipeline output,
    not a valid artifact, and is reported as ``missing_audio_stream``.
    """
    if not math.isfinite(expected_duration_s) or expected_duration_s <= 0:
        return Mp4Verdict(status=VIOLATED, reasons=("expected_duration_not_positive_finite",))
    if not math.isfinite(tolerance_s) or tolerance_s <= 0:
        return Mp4Verdict(status=VIOLATED, reasons=("tolerance_not_positive_finite",))

    try:
        st = os.lstat(path)
    except OSError:
        return Mp4Verdict(status=UNOBSERVED, reasons=("file_not_observable",))
    if stat.S_ISLNK(st.st_mode):
        return Mp4Verdict(status=VIOLATED, reasons=("symlink_refused",))
    if not stat.S_ISREG(st.st_mode):
        return Mp4Verdict(status=VIOLATED, reasons=("not_regular_file",))
    if st.st_size <= 0:
        return Mp4Verdict(status=VIOLATED, reasons=("zero_bytes",), size_bytes=0)

    digest, size, _ = stream_sha256(path)
    boxes, box_problems = read_top_level_boxes(path)
    if box_problems:
        return Mp4Verdict(
            status=VIOLATED,
            output_sha256=digest,
            size_bytes=size,
            boxes=boxes,
            reasons=tuple(f"box_layout: {p}" for p in box_problems),
        )

    tracks, moov_problems = read_moov_tracks(path)
    if moov_problems:
        return Mp4Verdict(
            status=VIOLATED,
            output_sha256=digest,
            size_bytes=size,
            boxes=boxes,
            reasons=tuple(f"moov_layout: {problem}" for problem in moov_problems),
        )
    video_tracks = tuple(track for track in tracks if track.is_video)
    audio_tracks = tuple(track for track in tracks if track.is_aac_audio)
    container_video = bool(video_tracks)
    container_audio = bool(audio_tracks)
    video_codec = video_tracks[0].codec if video_tracks else None
    audio_codec = audio_tracks[0].codec if audio_tracks else None
    # The container's own stream table is decided before the demuxer is asked:
    # a missing video or audio track is a definite defect, not an observation
    # failure, so it must not be masked by an unavailable ``ffprobe``.
    container_reasons: list[str] = []
    if not container_video:
        container_reasons.append("no_video_stream")
    if require_audio and not container_audio:
        container_reasons.append("missing_audio_stream")
    if container_reasons:
        return Mp4Verdict(
            status=VIOLATED,
            output_sha256=digest,
            size_bytes=size,
            boxes=boxes,
            reasons=tuple(container_reasons),
            has_video=container_video,
            video_codec=video_codec,
            has_audio_stream=container_audio,
            audio_codec=audio_codec,
        )

    probe = _probe(path, ffprobe, timeout_s)
    if probe is None:
        return Mp4Verdict(
            status=UNOBSERVED,
            output_sha256=digest,
            size_bytes=size,
            boxes=boxes,
            reasons=("ffprobe_unavailable_or_unparseable",),
            has_video=container_video,
            video_codec=video_codec,
            has_audio_stream=container_audio,
            audio_codec=audio_codec,
        )

    streams = probe.get("streams")
    fmt = probe.get("format")
    video = [
        s
        for s in (streams if isinstance(streams, list) else [])
        if isinstance(s, dict) and s.get("codec_type") == "video"
    ]
    duration_raw = fmt.get("duration") if isinstance(fmt, dict) else None
    try:
        probed = float(duration_raw) if duration_raw is not None else float("nan")
    except (TypeError, ValueError):
        probed = float("nan")

    reasons: list[str] = []
    # The container already declared a video track; the demuxer must agree.
    if not video:
        reasons.append("no_video_stream")
    if not math.isfinite(probed) or probed <= 0:
        reasons.append("probed_duration_not_positive_finite")
    elif abs(probed - expected_duration_s) > tolerance_s:
        reasons.append(
            f"duration_mismatch: probed={probed:.6f}s expected={expected_duration_s:.6f}s"
        )
    if reasons:
        return Mp4Verdict(
            status=VIOLATED,
            output_sha256=digest,
            size_bytes=size,
            probed_duration_s=probed if math.isfinite(probed) else 0.0,
            has_video=bool(video),
            boxes=boxes,
            reasons=tuple(reasons),
            video_codec=video_codec,
            has_audio_stream=container_audio,
            audio_codec=audio_codec,
        )
    return Mp4Verdict(
        status=PASS,
        output_sha256=digest,
        size_bytes=size,
        probed_duration_s=probed,
        has_video=True,
        boxes=boxes,
        video_codec=video_codec,
        has_audio_stream=container_audio,
        audio_codec=audio_codec,
    )


__all__ = [
    "AUDIO_CODEC",
    "AUDIO_HANDLER",
    "PASS",
    "UNOBSERVED",
    "VIDEO_HANDLER",
    "VIOLATED",
    "MoovTrack",
    "Mp4Verdict",
    "read_moov_tracks",
    "read_top_level_boxes",
    "verify_mp4",
]
