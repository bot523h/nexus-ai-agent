"""Pure deterministic formatters for SubRip (.srt) and WebVTT (.vtt).

Both formatters are pure mathematical functions:
* integer microseconds are mapped to strict ``HH:MM:SS,mmm`` (SRT) and
  ``HH:MM:SS.mmm`` (VTT) without floating point drift or timezone jitter;
* multiline segments and Unicode (Persian/RTL) are preserved byte-for-byte;
* WebVTT escapes HTML-like metacharacters (``&``, ``<``, ``>``) in plain text;
* output is 100% deterministic and byte-identical across runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from nexus_ai_agent.creative.packs.caption.models import (
    TranscriptRef,
    TranscriptSegment,
)


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
