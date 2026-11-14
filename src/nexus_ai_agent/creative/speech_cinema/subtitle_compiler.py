"""Subtitle compilation from aligned words with deterministic text codecs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class AlignedWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str
    index: int = 0
    #: True when the cue cannot meet the reading-speed or shot-cut guards (e.g. one
    #: word alone exceeds the CPS limit, or a single word straddles a shot cut).
    flagged: bool = False


_GUARD_FRAMES = 3  # a cue starting < 3 frames before a cut is moved to the cut
_SNAP_FRAMES = 2  # a cue ending < 3 frames before a cut is pulled back 2 frames


def _stamp(t: float, vtt: bool = False) -> str:
    ms = max(0, round(float(t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{'.' if vtt else ','}{ms:03d}"


def partition_cues(
    words: list[AlignedWord] | tuple[AlignedWord, ...],
    *,
    max_chars: int = 42,
    max_duration: float = 4.0,
    shot_cuts: list[float] | tuple[float, ...] = (),
    max_gap: float = 0.8,
    max_cps: float = 20.0,
    fps: float = 25.0,
) -> tuple[SubtitleCue, ...]:
    """Group aligned words into cues.

    A new cue starts before a word when the line would exceed ``max_chars``, the
    cue would exceed ``max_duration`` or ``max_cps`` (characters per second), the
    silence gap exceeds ``max_gap``, or a shot cut falls between the previous word's
    end and this word's end.  Cue times are then guarded against shot cuts at
    ``fps`` (start clamp within 3 frames before a cut; end snap 2 frames before it).
    """
    if not fps > 0:
        raise ValueError("fps must be positive")
    ws = list(words)
    cuts = sorted(float(x) for x in shot_cuts)
    out: list[SubtitleCue] = []
    group: list[AlignedWord] = []

    def flush() -> None:
        if not group:
            return
        text = " ".join(w.text for w in group)
        start, end = group[0].start, group[-1].end
        new_start, new_end, ok = _guard_cut(start, end, cuts, fps)
        if not ok:
            new_start, new_end = start, end
        dur = new_end - new_start
        over_cps = dur <= 0 or len(text) / dur > max_cps
        out.append(
            SubtitleCue(new_start, new_end, text, len(out) + 1, flagged=(not ok) or over_cps)
        )
        group.clear()

    for w in ws:
        if group:
            proposed = " ".join(x.text for x in group + [w])
            span = w.end - group[0].start
            cut = any(group[-1].end <= c < w.end for c in cuts)
            cps = len(proposed) / span if span > 0 else float("inf")
            if (
                len(proposed) > max_chars
                or span > max_duration
                or cut
                or w.start - group[-1].end > max_gap
                or cps > max_cps
            ):
                flush()
        group.append(w)
    flush()
    return tuple(out)


def _guard_cut(
    start: float, end: float, cuts: list[float], fps: float
) -> tuple[float, float, bool]:
    """Return guarded (start, end, ok); ``ok`` is False when no valid guarded span exists."""
    frame = 1.0 / fps
    guard = _GUARD_FRAMES * frame
    snap = _SNAP_FRAMES * frame
    new_start, new_end = start, end
    for c in cuts:
        if c - guard < new_start < c:
            new_start = c
    for c in cuts:
        if new_start < c and new_end >= c - guard and new_end > c - snap:
            new_end = c - snap
    if new_end > new_start:
        return new_start, new_end, True
    return start, end, False


compile_subtitles = partition_cues


def serialize_srt(cues: list[SubtitleCue] | tuple[SubtitleCue, ...]) -> str:
    return "\n\n".join(
        f"{i}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{c.text}" for i, c in enumerate(cues, 1)
    ) + ("\n" if cues else "")


def serialize_vtt(cues: list[SubtitleCue] | tuple[SubtitleCue, ...]) -> str:
    body = "\n\n".join(f"{_stamp(c.start, True)} --> {_stamp(c.end, True)}\n{c.text}" for c in cues)
    return "WEBVTT\n\n" + body + ("\n" if body else "")


def serialize_ass(cues: list[SubtitleCue] | tuple[SubtitleCue, ...]) -> str:
    head = (
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    def a(t: float) -> str:
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

    lines = (
        f"Dialogue: 0,{a(c.start)},{a(c.end)},Default,,0,0,0,,"
        f"{c.text.replace(chr(10), chr(92) + 'N')}"
        for c in cues
    )
    return head + "\n".join(lines) + ("\n" if cues else "")


_STAMP = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _seconds(stamp: str) -> float:
    m = _STAMP.fullmatch(stamp.strip())
    if m is None:
        raise ValueError(f"invalid subtitle timestamp: {stamp!r}")
    hours, minutes, seconds, fraction = m.groups()
    whole = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return float(whole) + int(fraction) / float(10 ** len(fraction))


def _parse_cues(text: str) -> tuple[SubtitleCue, ...]:
    """Parse SRT or WebVTT.  Cue identifiers are optional; header/NOTE blocks are skipped."""
    out: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        arrow = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if arrow is None:
            continue
        head, _, tail = lines[arrow].partition("-->")
        fields = tail.split()
        if not fields:
            raise ValueError("cue timing line has no end timestamp")
        start, end = _seconds(head), _seconds(fields[0])
        if end <= start:
            raise ValueError(f"cue ends before it starts: {start} -> {end}")
        out.append(SubtitleCue(start, end, " ".join(lines[arrow + 1 :]), len(out) + 1))
    return tuple(out)


def parse_srt(text: str) -> tuple[SubtitleCue, ...]:
    return _parse_cues(text)


def parse_vtt(text: str) -> tuple[SubtitleCue, ...]:
    return _parse_cues(text)


def subtitle_sha256(data: str | bytes) -> str:
    return sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


sha256_subtitles = subtitle_sha256
