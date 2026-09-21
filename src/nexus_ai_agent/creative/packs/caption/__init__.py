"""``nexus.language.caption`` — Local Persian and multilingual captions (Wave 4a substrate).

This package provides the pure substrate for Nagar speech captioning:
* typed data models (:class:`TranscriptSegment`, :class:`WordTiming`,
  :class:`SpeakerTurn`, :class:`TranscriptRef`, :class:`CaptionAsset`);
* pure formatters for SubRip (.srt) and WebVTT (.vtt companion rendition);
* pure capability operations: ``caption.transcribe`` (Level A) and
  ``caption.generate_srt`` (Level B).

In accordance with Wave 4a requirements, this package has zero heavy dependencies
(no torch, whisperx, or pyannote imports).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.caption.formatters import (
    escape_vtt,
    format_ass,
    format_ass_timestamp,
    format_karaoke_dialogue,
    format_srt,
    format_timestamp,
    format_vtt,
    is_persian_or_arabic,
    wrap_rtl_bidi,
)
from nexus_ai_agent.creative.packs.caption.models import (
    CAPTION_PACKAGE_ID,
    OPERATION_BURN_IN,
    OPERATION_GENERATE_ASS,
    OPERATION_GENERATE_SRT,
    OPERATION_HIGHLIGHT_WORDS,
    OPERATION_SEARCH_TRANSCRIPT,
    OPERATION_STYLE_VAZIRMATN,
    OPERATION_TRANSCRIBE,
    AssStyleConfig,
    BurnInInput,
    CaptionAsset,
    GenerateAssInput,
    GenerateSrtInput,
    HighlightWordsInput,
    SearchTranscriptInput,
    SpeakerTurn,
    StyleVazirmatnInput,
    TranscribeInput,
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
)
from nexus_ai_agent.creative.packs.caption.operations import (
    DOMAIN,
    build_caption_registry,
    register_caption_operations,
)

__all__ = [
    "BurnInInput",
    "CAPTION_PACKAGE_ID",
    "DOMAIN",
    "OPERATION_BURN_IN",
    "OPERATION_GENERATE_ASS",
    "OPERATION_GENERATE_SRT",
    "OPERATION_HIGHLIGHT_WORDS",
    "OPERATION_SEARCH_TRANSCRIPT",
    "OPERATION_STYLE_VAZIRMATN",
    "OPERATION_TRANSCRIBE",
    "SearchTranscriptInput",
    "AssStyleConfig",
    "CaptionAsset",
    "GenerateAssInput",
    "GenerateSrtInput",
    "HighlightWordsInput",
    "SpeakerTurn",
    "StyleVazirmatnInput",
    "TranscribeInput",
    "TranscriptRef",
    "TranscriptSegment",
    "WordTiming",
    "build_caption_registry",
    "escape_vtt",
    "format_ass",
    "format_ass_timestamp",
    "format_karaoke_dialogue",
    "format_srt",
    "format_timestamp",
    "format_vtt",
    "is_persian_or_arabic",
    "register_caption_operations",
    "wrap_rtl_bidi",
]
