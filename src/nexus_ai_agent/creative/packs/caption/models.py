"""Typed models for the ``nexus.language.caption`` pack (Wave 4a substrate).

This module defines the typed contracts for spoken-audio transcription and
caption generation in Nagar.  In accordance with Nagar architecture, this layer
is purely declarative: stdlib + pydantic only, no I/O, no heavy ML imports.

Surface:
* :class:`WordTiming` — word-level timestamp and confidence boundary
* :class:`TranscriptSegment` — timed phrase or sentence segment
* :class:`SpeakerTurn` — diarization turn associating a speaker with time
* :class:`TranscriptRef` — canonical transcript container
* :class:`CaptionAsset` — derived caption carrying primary SRT and companion renditions (VTT)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPTION_PACKAGE_ID = "nexus.language.caption"
OPERATION_TRANSCRIBE = "caption.transcribe"
OPERATION_GENERATE_SRT = "caption.generate_srt"
OPERATION_GENERATE_ASS = "caption.generate_ass_rtl"
OPERATION_STYLE_VAZIRMATN = "caption.style_vazirmatn"
OPERATION_HIGHLIGHT_WORDS = "caption.highlight_words"
OPERATION_SEARCH_TRANSCRIPT = "caption.search_transcript"
OPERATION_BURN_IN = "caption.burn_in"


class WordTiming(BaseModel):
    """Word-level alignment timing in integer microseconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    word: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    speaker: str | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> WordTiming:
        if self.end_us < self.start_us:
            raise ValueError(f"end_us ({self.end_us}) must be >= start_us ({self.start_us})")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def start_s(self) -> float:
        return self.start_us / 1_000_000

    @property
    def end_s(self) -> float:
        return self.end_us / 1_000_000


class TranscriptSegment(BaseModel):
    """Timed transcript segment (utterance/sentence/phrase)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    text: str
    words: tuple[WordTiming, ...] = ()
    speaker: str | None = None
    segment_id: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> TranscriptSegment:
        if self.end_us < self.start_us:
            raise ValueError(f"end_us ({self.end_us}) must be >= start_us ({self.start_us})")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def start_s(self) -> float:
        return self.start_us / 1_000_000

    @property
    def end_s(self) -> float:
        return self.end_us / 1_000_000


class SpeakerTurn(BaseModel):
    """Diarized speaker turn interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    speaker: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    text: str | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> SpeakerTurn:
        if self.end_us < self.start_us:
            raise ValueError(f"end_us ({self.end_us}) must be >= start_us ({self.start_us})")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


class TranscriptRef(BaseModel):
    """Structured container for transcript segments, word alignments, and speaker turns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript_id: str = Field(min_length=1)
    source_asset_id: str = Field(default="")
    language: str = Field(default="fa", min_length=1)
    segments: tuple[TranscriptSegment, ...] = ()
    duration_us: int = Field(default=0, ge=0)
    speaker_turns: tuple[SpeakerTurn, ...] = ()

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def is_empty(self) -> bool:
        return len(self.segments) == 0


class CaptionAsset(BaseModel):
    """Derived caption asset carrying the primary SRT text and companion renditions (e.g. VTT)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1)
    source_transcript_id: str = Field(default="")
    content: str
    content_sha256: str = Field(min_length=1)
    format: Literal["srt", "vtt", "ass"] = "srt"
    companion_renditions: dict[str, str] = Field(default_factory=dict)
    companion_hashes: dict[str, str] = Field(default_factory=dict)
    language: str = Field(default="fa")
    line_count: int = Field(default=0, ge=0)
    duration_us: int = Field(default=0, ge=0)

    def rendition(self, fmt: str) -> str:
        """Return content for requested format (primary or companion)."""
        if fmt == self.format:
            return self.content
        if fmt in self.companion_renditions:
            return self.companion_renditions[fmt]
        raise KeyError(f"rendition {fmt!r} not available on caption asset {self.asset_id!r}")

    def has_rendition(self, fmt: str) -> bool:
        return fmt == self.format or fmt in self.companion_renditions

    def rendition_hash(self, fmt: str) -> str:
        """Return content hash for requested format."""
        if fmt == self.format:
            return self.content_sha256
        if fmt in self.companion_hashes:
            return self.companion_hashes[fmt]
        raise KeyError(f"rendition {fmt!r} not available on caption asset {self.asset_id!r}")


# ---------------------------------------------------------------------------
# Command input schemas
# ---------------------------------------------------------------------------


class TranscribeInput(BaseModel):
    """Input payload for ``caption.transcribe`` (Level A)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_asset_id: str = Field(min_length=1)
    language: str | None = None
    language_policy: str = "auto"
    transcript: TranscriptRef | None = None
    model_name: str | None = None


class GenerateSrtInput(BaseModel):
    """Input payload for ``caption.generate_srt`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript: TranscriptRef
    output_asset_id: str | None = None
    include_vtt: bool = True
    line_policy: dict[str, Any] = Field(default_factory=dict)


class AssStyleConfig(BaseModel):
    """Typography and layout style specification for Advanced SubStation Alpha (ASS)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "Default"
    font_name: str = "Vazirmatn"
    font_size: int = Field(default=48, ge=8, le=144)
    primary_colour: str = "&H00FFFFFF"  # White (&HAABBGGRR in ASS)
    secondary_colour: str = "&H000000FF"
    outline_colour: str = "&H00000000"  # Black outline
    back_colour: str = "&H80000000"  # Semi-transparent shadow
    bold: bool = True
    italic: bool = False
    underline: bool = False
    strike_out: bool = False
    scale_x: int = 100
    scale_y: int = 100
    spacing: int = 0
    angle: int = 0
    border_style: int = 1
    outline: float = 3.0
    shadow: float = 2.0
    alignment: int = Field(default=2, ge=1, le=9)  # 2 = Bottom-Center in ASS v4+
    margin_l: int = 40
    margin_r: int = 40
    margin_v: int = 40
    encoding: int = 1


class GenerateAssInput(BaseModel):
    """Input payload for ``caption.generate_ass_rtl`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript: TranscriptRef
    output_asset_id: str | None = None
    style: AssStyleConfig | None = None
    enable_rtl_wrap: bool = True
    enable_karaoke: bool = False
    play_res_x: int = Field(default=1280, ge=320)
    play_res_y: int = Field(default=720, ge=240)


class StyleVazirmatnInput(BaseModel):
    """Input payload for ``caption.style_vazirmatn`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caption_asset_id: str = Field(min_length=1)
    font_size: int = Field(default=48, ge=8, le=144)
    primary_colour: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"
    shadow_colour: str = "&H80000000"
    alignment: int = Field(default=2, ge=1, le=9)
    bold: bool = True
    play_res_x: int = Field(default=1280, ge=320)
    play_res_y: int = Field(default=720, ge=240)


class HighlightWordsInput(BaseModel):
    """Input payload for ``caption.highlight_words`` (Level A)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript: TranscriptRef
    highlight_colour: str = "&H0000E5FF"  # Golden/Cyan highlight
    mode: Literal["karaoke_tag", "span_tag"] = "karaoke_tag"


class SearchTranscriptInput(BaseModel):
    """Input payload for ``caption.search_transcript`` (Level A)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript: TranscriptRef
    query: str = Field(min_length=1)
    case_sensitive: bool = False
    exact_word: bool = False


class BurnInInput(BaseModel):
    """Input payload for ``caption.burn_in`` (Level C)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_asset_id: str = Field(min_length=1)
    caption_asset_id: str = Field(min_length=1)
    output_asset_id: str | None = None
    confirmed: bool = False
