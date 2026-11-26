"""Pure parsers for FFmpeg's diagnostic output (no I/O, no subprocess).

FFmpeg reports every measurement on *stderr* as human-oriented text.  These
functions turn that text into integers/floats and are the only place the format
is known, so a format drift is a one-file fix with a failing unit test, not a
silent mis-measurement.  Everything here is total over arbitrary text: unknown
lines are ignored, impossible values raise :class:`ParseError` instead of
producing evidence.

Formats were captured from FFmpeg 6.1 and 7.0 (the apt build and the bundled
``imageio-ffmpeg`` wheel respectively)::

    [silencedetect @ 0x...] silence_start: 1.99998
    [silencedetect @ 0x...] silence_end: 3.50002 | silence_duration: 1.50005
    [Parsed_showinfo_1 @ 0x...] n:   0 pts:  10240 pts_time:1 duration: ...
    Duration: 00:00:05.00, start: 0.000000, bitrate: 705 kb/s
    [Parsed_ebur128_0 @ 0x...] Summary:
      Integrated loudness:
        I:         -22.0 LUFS
      Loudness range:
        LRA:         1.0 LU
      True peak:
        Peak:      -18.1 dBFS
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

MICROSECONDS = 1_000_000

#: ITU-R BS.1770 absolute gate: program loudness at or below it is not measurable.
ABSOLUTE_GATE_LUFS = -70.0

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_PTS_TIME = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video:.*?(?<!\w)(\d{2,5})x(\d{2,5})(?!\w)")
_AUDIO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Audio:.*?(\d{4,6}) Hz,\s*([^,]+),")
_LUFS = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS", re.MULTILINE)
_LRA = re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?|-inf)\s*LU\b", re.MULTILINE)
_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", re.MULTILINE)
_FFMPEG_VERSION = re.compile(r"ffmpeg version\s+(\S+)")

#: Relevance filters so the analyzer can keep only the lines a parser needs
#: while streaming stderr (bounded memory regardless of media length).
SILENCE_LINE = re.compile(r"silence_(?:start|end)")
SCENE_LINE = re.compile(r"showinfo.*pts_time:")
LOUDNESS_SUMMARY_LINE = re.compile(
    r"Summary:|^\s*(?:I|LRA|Peak|Threshold|LRA low|LRA high):|loudness:|True peak:"
)
PROBE_LINE = re.compile(r"^\s*(?:Duration:|Stream #)")
VERSION_LINE = re.compile(r"^ffmpeg version")


class ParseError(ValueError):
    """The text did not contain a coherent measurement."""


def to_us(seconds: float) -> int:
    """Round seconds to integer microseconds, rejecting non-finite values."""
    if not math.isfinite(seconds):
        raise ParseError(f"non-finite time: {seconds!r}")
    return int(round(seconds * MICROSECONDS))


def parse_silences(lines: Iterable[str], duration_us: int) -> list[tuple[int, int]]:
    """``silencedetect`` output -> ascending, non-overlapping ``(start_us, end_us)`` spans.

    A trailing ``silence_start`` without a matching ``silence_end`` means the
    silence runs to the end of the media: it is closed at ``duration_us``.
    Spans are clamped to ``[0, duration_us]`` (FFmpeg reports tiny negative
    starts and ends a frame past the duration) and empty spans are dropped.
    """
    spans: list[tuple[int, int]] = []
    open_start: int | None = None
    for line in lines:
        start_match = _SILENCE_START.search(line)
        if start_match:
            open_start = to_us(float(start_match.group(1)))
            continue
        end_match = _SILENCE_END.search(line)
        if end_match and open_start is not None:
            spans.append((open_start, to_us(float(end_match.group(1)))))
            open_start = None
    if open_start is not None:
        spans.append((open_start, duration_us))

    clamped: list[tuple[int, int]] = []
    previous_end = 0
    for start, end in spans:
        start = max(0, min(start, duration_us))
        end = max(0, min(end, duration_us))
        start = max(start, previous_end)  # never overlap the previous span
        if end > start:
            clamped.append((start, end))
            previous_end = end
    return clamped


def parse_scene_cuts(lines: Iterable[str], duration_us: int) -> list[int]:
    """``showinfo`` output of ``select='gt(scene,T)'`` -> ascending unique cut instants."""
    cuts: set[int] = set()
    for line in lines:
        if "showinfo" not in line:
            continue
        match = _PTS_TIME.search(line)
        if match is None:
            continue
        instant = to_us(float(match.group(1)))
        if 0 <= instant <= duration_us:
            cuts.add(instant)
    return sorted(cuts)


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float | None
    loudness_range_lu: float | None
    true_peak_dbfs: float | None


def _number(token: str) -> float | None:
    """``-inf`` means "not measurable" -> ``None``; everything else must be finite."""
    if token == "-inf":
        return None
    value = float(token)
    if not math.isfinite(value):
        raise ParseError(f"non-finite loudness value: {token!r}")
    return value


def parse_loudness(text: str) -> Loudness:
    """The ``ebur128`` *Summary* block -> :class:`Loudness`.

    Only the text after the final ``Summary:`` marker is read: the per-frame
    lines before it carry running values that are not the result.
    """
    marker = text.rfind("Summary:")
    if marker < 0:
        raise ParseError("no ebur128 summary found (is the media audio-less?)")
    summary = text[marker:]
    integrated = _LUFS.search(summary)
    if integrated is None:
        raise ParseError("ebur128 summary has no integrated loudness")
    loudness_range = _LRA.search(summary)
    peak = _PEAK.search(summary)
    measured = _number(integrated.group(1))
    if measured is not None and measured <= ABSOLUTE_GATE_LUFS:
        # FFmpeg 6 prints "-inf" for a signal below the BS.1770 absolute gate, FFmpeg 7
        # prints the gate itself ("-70.0"). Both mean "nothing measurable".
        measured = None
    return Loudness(
        integrated_lufs=measured,
        loudness_range_lu=_number(loudness_range.group(1)) if loudness_range else None,
        true_peak_dbfs=_number(peak.group(1)) if peak else None,
    )


@dataclass(frozen=True)
class MediaProbe:
    duration_us: int
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    sample_rate: int | None


def parse_probe(lines: Iterable[str]) -> MediaProbe:
    """``ffmpeg -i`` banner -> duration and stream presence."""
    duration_us: int | None = None
    width = height = sample_rate = None
    has_video = has_audio = False
    for line in lines:
        if duration_us is None:
            duration = _DURATION.search(line)
            if duration:
                hours, minutes, seconds = duration.groups()
                duration_us = to_us(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
                continue
        if "Video:" in line and not has_video:
            video = _VIDEO_STREAM.search(line)
            if video:
                has_video = True
                width, height = int(video.group(1)), int(video.group(2))
        elif "Audio:" in line and not has_audio:
            audio = _AUDIO_STREAM.search(line)
            if audio:
                has_audio = True
                sample_rate = int(audio.group(1))
    if duration_us is None:
        raise ParseError("no Duration found (not a seekable media file?)")
    return MediaProbe(duration_us, has_video, has_audio, width, height, sample_rate)


def parse_version(lines: Iterable[str]) -> str:
    for line in lines:
        match = _FFMPEG_VERSION.match(line)
        if match:
            return match.group(1)
    return "unknown"


__all__ = [
    "LOUDNESS_SUMMARY_LINE",
    "PROBE_LINE",
    "SCENE_LINE",
    "SILENCE_LINE",
    "VERSION_LINE",
    "Loudness",
    "MediaProbe",
    "ParseError",
    "parse_loudness",
    "parse_probe",
    "parse_scene_cuts",
    "parse_silences",
    "parse_version",
    "to_us",
]
