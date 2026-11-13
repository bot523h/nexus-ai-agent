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
) -> tuple[SubtitleCue, ...]:
    ws = list(words)
    cuts = sorted(float(x) for x in shot_cuts)
    out: list[SubtitleCue] = []
    group: list[AlignedWord] = []

    def flush() -> None:
        if group:
            out.append(
                SubtitleCue(
                    group[0].start, group[-1].end, " ".join(w.text for w in group), len(out) + 1
                )
            )
            group.clear()

    for w in ws:
        proposed = " ".join(x.text for x in group + [w])
        cut = any(group and group[-1].end <= c <= w.start for c in cuts)
        if group and (
            len(proposed) > max_chars
            or w.end - group[0].start > max_duration
            or cut
            or w.start - group[-1].end > max_gap
        ):
            flush()
        group.append(w)
    flush()
    return tuple(out)


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


def parse_srt(text: str) -> tuple[SubtitleCue, ...]:
    out: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        m = re.search(r"([\d:,]+)\s*-->\s*([\d:,]+)", lines[1])
        if not m:
            continue

        def p(x: str) -> float:
            h, mi, sec = x.replace(",", ".").split(":")
            return int(h) * 3600 + int(mi) * 60 + float(sec)

        out.append(SubtitleCue(p(m.group(1)), p(m.group(2)), " ".join(lines[2:]), len(out) + 1))
    return tuple(out)


def parse_vtt(text: str) -> tuple[SubtitleCue, ...]:
    return parse_srt(text.replace(".", ","))


def subtitle_sha256(data: str | bytes) -> str:
    return sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


sha256_subtitles = subtitle_sha256
