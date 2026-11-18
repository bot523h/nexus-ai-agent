"""Independent post-render verifier for MP4 artifacts on disk.

FFmpeg's exit code is never evidence of a good output. This verifier re-reads
the file that is actually on disk and returns exactly one of three statuses:

* ``PASS`` — every check below held.
* ``VIOLATED`` — evidence of a defect: not a regular file, a symlink, empty,
  a broken ISO-BMFF box layout, no video stream, or a duration outside the
  tolerance.
* ``UNOBSERVED`` — the verifier could not observe the file: ``ffprobe`` is
  missing, timed out, or printed unparseable output.

Anything other than ``PASS`` must be treated as a failure by the caller.

Checks, in order:

1. ``os.lstat``: regular file, not a symlink, size > 0.
2. Streaming SHA-256 over 64 KiB chunks (``output_sha256``).
3. ISO-BMFF top-level box walk: ``ftyp`` first, ``moov`` and ``mdat`` present,
   box sizes exactly tile the file. Only 8–16 byte headers are read; payloads
   are skipped with ``seek``.
4. ``ffprobe -print_format json``: a video stream exists, and the container
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
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.creative.materializer import stream_sha256

PASS = "PASS"
VIOLATED = "VIOLATED"
UNOBSERVED = "UNOBSERVED"
_REQUIRED_BOXES = ("ftyp", "moov", "mdat")
_MAX_FFPROBE_JSON_BYTES = 1_048_576


@dataclass(frozen=True)
class Mp4Verdict:
    status: str
    output_sha256: str = ""
    size_bytes: int = 0
    probed_duration_s: float = 0.0
    has_video: bool = False
    boxes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status == PASS


def read_top_level_boxes(path: Path) -> tuple[tuple[str, ...], list[str]]:
    """Walk the top-level ISO-BMFF boxes without loading payloads.

    Returns the box types and a list of problems (empty when the layout is
    consistent). A size of 1 means a 64-bit ``largesize`` follows; a size of 0
    means "to end of file" and is only valid for the last box.
    """
    problems: list[str] = []
    types: list[str] = []
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
            types.append(kind)
            offset += size
    # Unreachable by construction: the loop exits either at ``offset == file_size``
    # or through a recorded overrun problem, so tiling is already proven above.
    # Kept as an explicit assertion of the invariant for future edits.
    if offset != file_size and not problems:  # pragma: no cover - structurally unreachable
        problems.append("boxes do not tile the file")
    if types and types[0] != "ftyp":
        problems.append("first box is not ftyp")
    for required in _REQUIRED_BOXES:
        if required not in types:
            problems.append(f"missing required box {required!r}")
    return tuple(types), problems


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
) -> Mp4Verdict:
    """Independently verify the MP4 at ``path``. Never raises for file content."""
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

    probe = _probe(path, ffprobe, timeout_s)
    if probe is None:
        return Mp4Verdict(
            status=UNOBSERVED,
            output_sha256=digest,
            size_bytes=size,
            boxes=boxes,
            reasons=("ffprobe_unavailable_or_unparseable",),
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
        )
    return Mp4Verdict(
        status=PASS,
        output_sha256=digest,
        size_bytes=size,
        probed_duration_s=probed,
        has_video=True,
        boxes=boxes,
    )


__all__ = [
    "PASS",
    "UNOBSERVED",
    "VIOLATED",
    "Mp4Verdict",
    "read_top_level_boxes",
    "verify_mp4",
]
