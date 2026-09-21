"""Pure deterministic formatters for SubRip (.srt) and WebVTT (.vtt).

Both formatters are pure mathematical functions:
* integer microseconds are mapped to strict ``HH:MM:SS,mmm`` (SRT) and
  ``HH:MM:SS.mmm`` (VTT) without floating point drift or timezone jitter;
* multiline segments and Unicode (Persian/RTL) are preserved byte-for-byte;
* WebVTT escapes HTML-like metacharacters (``&``, ``<``, ``>``) in plain text;
* output is 100% deterministic and byte-identical across runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from nexus_ai_agent.creative.packs.caption.models import (
    AssStyleConfig,
    TranscriptRef,
    TranscriptSegment,
)

_PERSIAN_ARABIC_RE = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_RLM = "\u200f"  # Right-to-Left Mark


def format_timestamp(us: int, *, decimal_sep: str = ",") -> str:
    """Format microseconds as HH:MM:SS,mmm (SRT) or HH:MM:SS.mmm (VTT).

    Handles timestamp rollover across minutes, hours, and days deterministically.
    """
    safe_us = max(0, us)
    total_ms = safe_us // 1000
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}{decimal_sep}{ms:03d}"


def escape_vtt(text: str) -> str:
    """Escape special characters in WebVTT cue text (&, <, >)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_cue_text(text: str) -> str:
    """Normalize line endings to UNIX format and strip outer whitespace."""
    clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return clean


def _extract_segments(
    source: Sequence[TranscriptSegment] | TranscriptRef,
) -> Sequence[TranscriptSegment]:
    if isinstance(source, TranscriptRef):
        return source.segments
    return source


def format_srt(source: Sequence[TranscriptSegment] | TranscriptRef) -> str:
    """Format a transcript as a strict SubRip (.srt) string."""
    segments = _extract_segments(source)
    if not segments:
        return ""

    blocks: list[str] = []
    for index, seg in enumerate(segments, start=1):
        text = _normalize_cue_text(seg.text)
        start_ts = format_timestamp(seg.start_us, decimal_sep=",")
        end_ts = format_timestamp(seg.end_us, decimal_sep=",")
        blocks.append(f"{index}\n{start_ts} --> {end_ts}\n{text}")

    return "\n\n".join(blocks) + "\n"


def format_vtt(
    source: Sequence[TranscriptSegment] | TranscriptRef,
    *,
    escape: bool = True,
) -> str:
    """Format a transcript as a strict WebVTT (.vtt) string."""
    segments = _extract_segments(source)
    if not segments:
        return "WEBVTT\n"

    blocks: list[str] = ["WEBVTT"]
    for index, seg in enumerate(segments, start=1):
        text = _normalize_cue_text(seg.text)
        if escape:
            text = escape_vtt(text)
        start_ts = format_timestamp(seg.start_us, decimal_sep=".")
        end_ts = format_timestamp(seg.end_us, decimal_sep=".")
        blocks.append(f"{index}\n{start_ts} --> {end_ts}\n{text}")

    return "\n\n".join(blocks) + "\n"


def format_ass_timestamp(us: int) -> str:
    """Format microseconds as H:MM:SS.cc for Advanced SubStation Alpha (.ass).

    Centiseconds (two digits) are standard in ASS v4.00+.
    """
    safe_us = max(0, us)
    total_cs = safe_us // 10_000
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def is_persian_or_arabic(text: str) -> bool:
    """Return True if text contains Persian or Arabic characters."""
    return bool(_PERSIAN_ARABIC_RE.search(text))


def wrap_rtl_bidi(text: str) -> str:
    """Ensure proper bidirectional display and newline normalization for ASS subtitles.

    Normalizes newlines to ASS ``\\N`` and wraps Persian/Arabic text with RLM
    markers to preserve punctuation anchoring across heterogeneous renderers.
    """
    clean = _normalize_cue_text(text)
    lines = clean.split("\n")
    processed_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if is_persian_or_arabic(stripped):
            # Anchor punctuation at boundaries using RLM
            processed = f"{_RLM}{stripped}{_RLM}"
        else:
            processed = stripped
        processed_lines.append(processed)
    return "\\N".join(processed_lines)


def format_karaoke_dialogue(segment: TranscriptSegment) -> str:
    """Format word-level karaoke timing tags (\\k<centiseconds>) for ASS."""
    if not segment.words:
        return wrap_rtl_bidi(segment.text)

    tokens: list[str] = []
    for w in segment.words:
        dur_cs = max(1, (w.end_us - w.start_us) // 10_000)
        clean_word = w.word.strip()
        tokens.append(f"{{\\k{dur_cs}}}{clean_word}")
    return " ".join(tokens)


def format_ass(
    source: Sequence[TranscriptSegment] | TranscriptRef,
    *,
    style: AssStyleConfig | None = None,
    enable_karaoke: bool = False,
    enable_rtl_wrap: bool = True,
    play_res_x: int = 1280,
    play_res_y: int = 720,
) -> str:
    """Format a transcript into strict, deterministic Advanced SubStation Alpha (ASS v4.00+)."""
    cfg = style or AssStyleConfig()
    segments = _extract_segments(source)

    bold_val = -1 if cfg.bold else 0
    italic_val = -1 if cfg.italic else 0
    underline_val = -1 if cfg.underline else 0
    strikeout_val = -1 if cfg.strike_out else 0

    header = (
        "[Script Info]\n"
        "; Script generated by NEXUS Nagar Caption Engine (Wave 4b)\n"
        "Title: Nagar Persian Captions\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: None\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {cfg.name},{cfg.font_name},{cfg.font_size},{cfg.primary_colour},{cfg.secondary_colour},"
        f"{cfg.outline_colour},{cfg.back_colour},{bold_val},{italic_val},{underline_val},{strikeout_val},"
        f"{cfg.scale_x},{cfg.scale_y},{cfg.spacing},{cfg.angle},{cfg.border_style},{cfg.outline:.1f},"
        f"{cfg.shadow:.1f},{cfg.alignment},{cfg.margin_l},{cfg.margin_r},{cfg.margin_v},{cfg.encoding}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    if not segments:
        return header

    lines: list[str] = [header.rstrip("\n")]
    for seg in segments:
        start_ts = format_ass_timestamp(seg.start_us)
        end_ts = format_ass_timestamp(seg.end_us)

        if enable_karaoke and seg.words:
            dialogue_text = format_karaoke_dialogue(seg)
        elif enable_rtl_wrap:
            dialogue_text = wrap_rtl_bidi(seg.text)
        else:
            dialogue_text = _normalize_cue_text(seg.text).replace("\n", "\\N")

        lines.append(
            f"Dialogue: 0,{start_ts},{end_ts},{cfg.name},,0,0,0,,{dialogue_text}"
        )

    return "\n".join(lines) + "\n"
