"""Nagar Creative Studio -- Persian RTL Subtitles and Typography Engine.

Owned by Agent 2 (Media Pipeline Engineer + Interaction Designer).

Enforces Law 5 (RTL Persian First-Class):
- Full Bidirectional (bidi) isolate wrapping for mixed Persian/English text
- Persian numerals (۰-۹) and LTR-isolated timecodes (00:01:23.456)
- Punctuation stabilization (anchors Persian '؟', '«»', '،', '.')
- Word-boundary aware wrapping preserving Persian compound words and ZWNJ (\\u200c)
- Vazirmatn font typography styling
- Bidi transcript search with Unicode Persian normalization
- SubtitleCue and SubtitleTrack models with .ass, .srt, and .vtt formatters
"""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import MICROSECONDS_PER_SECOND, TimeRangeUS

# Unicode Bidi directional format characters
RLI = "\u2067"  # Right-to-Left Isolate
LRI = "\u2066"  # Left-to-Right Isolate
PDI = "\u2069"  # Pop Directional Isolate
RLM = "\u200f"  # Right-to-Left Mark
LRM = "\u200e"  # Left-to-Right Mark
ZWNJ = "\u200c"  # Zero-Width Non-Joiner (نیم‌فاصله)

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_OR_NUM_RE = re.compile(r"([a-zA-Z0-9_\-.:/]+)")


class PersianRTLStyler:
    """Core typography and bidirectional isolation engine for Persian and multilingual text."""

    @classmethod
    def is_persian_or_rtl(cls, text: str) -> bool:
        """Return True if text contains any Persian/Arabic script characters."""
        return bool(_PERSIAN_CHAR_RE.search(text))

    @classmethod
    def format_timecode_rtl(cls, timecode_us: int, use_persian_digits: bool = False) -> str:
        """Format microseconds into an LTR-isolated timecode string.

        Wrapping in LRI...PDI ensures colons and milliseconds do not invert in RTL layouts.
        """
        total_seconds = timecode_us / MICROSECONDS_PER_SECOND
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        millis = int((timecode_us % MICROSECONDS_PER_SECOND) // 1000)

        tc_raw = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        if use_persian_digits:
            tc_raw = tc_raw.translate(PERSIAN_DIGITS)

        # LRI (\\u2066) ... PDI (\\u2069) guarantees timecodes stay LTR even inside RTL paragraphs
        return f"{LRI}{tc_raw}{PDI}"

    @classmethod
    def stabilize_bidi_text(cls, text: str) -> str:
        """Stabilize mixed Persian/English text with Unicode directional isolates.

        Isolates embedded Latin words/numbers so they don't corrupt RTL reading order
        or flip punctuation like periods, quotes («»), or Persian question marks (؟).
        """
        if not text:
            return ""

        # If purely LTR, return as is
        if not cls.is_persian_or_rtl(text):
            return text

        # Persian punctuation standardizations
        normalized = text.replace("?", "؟").replace(";", "؛")

        # Isolate embedded Latin words, numbers, and URLs with LRI ... PDI
        def _isolate_ltr(match: re.Match[str]) -> str:
            token = match.group(1)
            return f"{LRI}{token}{PDI}"

        isolated = _LATIN_OR_NUM_RE.sub(_isolate_ltr, normalized)

        # Wrap overall paragraph in RLI ... PDI with trailing RLM to pin final punctuation
        return f"{RLI}{isolated}{PDI}{RLM}"

    @classmethod
    def wrap_persian_lines(cls, text: str, max_chars_per_line: int = 42) -> str:
        """Wrap Persian text along word boundaries without breaking ZWNJ compound words."""
        words = text.split(" ")
        lines: list[str] = []
        current_line: list[str] = []
        current_len = 0

        for w in words:
            # Visible length excludes bidi marks
            vis_len = len(w.replace(RLI, "").replace(LRI, "").replace(PDI, "").replace(RLM, ""))
            if current_len + vis_len + 1 > max_chars_per_line and current_line:
                lines.append(" ".join(current_line))
                current_line = [w]
                current_len = vis_len
            else:
                current_line.append(w)
                current_len += vis_len + 1

        if current_line:
            lines.append(" ".join(current_line))

        return "\n".join(lines)

    @classmethod
    def normalize_for_search(cls, text: str) -> str:
        """Normalize Persian text for search matching (Yeh/Kaf unification, ZWNJ stripping)."""
        t = text.translate(ASCII_DIGITS)
        # Unify Arabic/Persian Yeh and Kaf
        t = (
            t.replace("ي", "ی")
            .replace("ك", "ک")
            .replace("آ", "ا")
            .replace("أ", "ا")
            .replace("إ", "ا")
        )
        # Strip diacritics (fatha, damma, kasra, tanween, tashdeed)
        t = re.sub(r"[\u064B-\u065F\u0670]", "", t)
        # Strip ZWNJ and bidi control characters
        t = t.replace(ZWNJ, "").replace(RLI, "").replace(LRI, "").replace(PDI, "").replace(RLM, "")
        return re.sub(r"\s+", " ", t).strip().lower()


# ---------------------------------------------------------------------------
# Subtitle Cue & Track Models
# ---------------------------------------------------------------------------


class SubtitleCue(BaseModel):
    """A single timed subtitle cue."""

    model_config = ConfigDict(extra="forbid")

    cue_id: str = Field(default_factory=lambda: f"cue_{uuid.uuid4().hex[:8]}")
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    text: str
    styled_text: str = ""
    speaker_id: str | None = None

    @model_validator(mode="after")
    def _validate_and_style(self) -> SubtitleCue:
        if self.start_us >= self.end_us:
            raise ValueError(f"start_us ({self.start_us}) must be < end_us ({self.end_us})")
        if not self.styled_text:
            self.styled_text = PersianRTLStyler.stabilize_bidi_text(self.text)
        return self

    @property
    def range(self) -> TimeRangeUS:
        return TimeRangeUS(start_us=self.start_us, end_us=self.end_us)


class SubtitleTrack(BaseModel):
    """A track containing timed subtitle cues with Persian Vazirmatn styling."""

    model_config = ConfigDict(extra="forbid")

    track_id: str = Field(default_factory=lambda: f"sub_{uuid.uuid4().hex[:8]}")
    name: str = "Persian Subtitles"
    language: str = "fa"
    font_family: str = "Vazirmatn"
    font_size: int = 24
    cues: list[SubtitleCue] = Field(default_factory=list)

    def add_cue(
        self, start_us: int, end_us: int, text: str, speaker_id: str | None = None
    ) -> SubtitleCue:
        cue = SubtitleCue(start_us=start_us, end_us=end_us, text=text, speaker_id=speaker_id)
        self.cues.append(cue)
        self.cues.sort(key=lambda c: c.start_us)
        return cue

    def find_cue_at(self, timecode_us: int) -> SubtitleCue | None:
        """Find the active subtitle cue at a given microsecond timecode."""
        for cue in self.cues:
            if cue.start_us <= timecode_us < cue.end_us:
                return cue
        return None

    def search_cues(self, query: str) -> list[SubtitleCue]:
        """Search cues using normalized Persian matching."""
        norm_q = PersianRTLStyler.normalize_for_search(query)
        matches: list[SubtitleCue] = []
        for cue in self.cues:
            if norm_q in PersianRTLStyler.normalize_for_search(cue.text):
                matches.append(cue)
        return matches

    def export_srt(self) -> str:
        """Export track as SubRip (.srt) format with RTL preservation."""
        blocks: list[str] = []
        for idx, cue in enumerate(self.cues, 1):
            s_tc = self._format_srt_tc(cue.start_us)
            e_tc = self._format_srt_tc(cue.end_us)
            blocks.append(f"{idx}\n{s_tc} --> {e_tc}\n{cue.styled_text}\n")
        return "\n".join(blocks)

    def export_vtt(self) -> str:
        """Export track as WebVTT (.vtt) format."""
        lines = ["WEBVTT", ""]
        for idx, cue in enumerate(self.cues, 1):
            s_tc = self._format_vtt_tc(cue.start_us)
            e_tc = self._format_vtt_tc(cue.end_us)
            lines.append(f"{idx}\n{s_tc} --> {e_tc}\n{cue.styled_text}\n")
        return "\n".join(lines)

    def export_ass(self, video_width: int = 1920, video_height: int = 1080) -> str:
        """Export track as Advanced SubStation Alpha (.ass) format with Vazirmatn font."""
        header = f"""[Script Info]
Title: Nagar Studio Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {video_width}
PlayResY: {video_height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, \
BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, \
Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{self.font_family},{self.font_size},&H00FFFFFF,&H000000FF,&H00000000,\
&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,20,20,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events: list[str] = []
        for cue in self.cues:
            s_tc = self._format_ass_tc(cue.start_us)
            e_tc = self._format_ass_tc(cue.end_us)
            speaker = cue.speaker_id or ""
            # In ASS, newlines are represented as \\N
            text = cue.styled_text.replace("\n", "\\N")
            events.append(f"Dialogue: 0,{s_tc},{e_tc},Default,{speaker},0,0,0,,{text}")

        return header + "\n".join(events) + "\n"

    @staticmethod
    def _format_srt_tc(us: int) -> str:
        total_s = us // MICROSECONDS_PER_SECOND
        ms = (us % MICROSECONDS_PER_SECOND) // 1000
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @staticmethod
    def _format_vtt_tc(us: int) -> str:
        total_s = us // MICROSECONDS_PER_SECOND
        ms = (us % MICROSECONDS_PER_SECOND) // 1000
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    @staticmethod
    def _format_ass_tc(us: int) -> str:
        total_s = us // MICROSECONDS_PER_SECOND
        cs = (us % MICROSECONDS_PER_SECOND) // 10_000  # centiseconds
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"
