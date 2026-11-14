"""Hyperion Phase 2 speech-cinema primitives."""

from .color_ops import HANDLERS as COLOR_HANDLERS
from .color_ops import bind_operations as bind_color_operations
from .forced_aligner import (
    AcousticFeatures,
    AlignmentResult,
    PhonemeAlignment,
    WordAlignment,
    align_tokens,
    banded_dtw,
    forced_align,
)
from .isochronous_dubbing import (
    TimeScaleResult,
    scale_audio,
    time_scale,
    time_stretch_wsola_pcm,
)
from .subtitle_compiler import (
    AlignedWord,
    SubtitleCue,
    compile_subtitles,
    parse_srt,
    parse_vtt,
    partition_cues,
    serialize_ass,
    serialize_srt,
    serialize_vtt,
    subtitle_sha256,
)
from .vad_detector import SpeechSegment, VADConfig, VADResult, detect_voice, read_wav

__all__ = [
    "VADConfig",
    "VADResult",
    "SpeechSegment",
    "detect_voice",
    "read_wav",
    "AcousticFeatures",
    "AlignmentResult",
    "WordAlignment",
    "PhonemeAlignment",
    "align_tokens",
    "forced_align",
    "banded_dtw",
    "TimeScaleResult",
    "time_scale",
    "scale_audio",
    "time_stretch_wsola_pcm",
    "AlignedWord",
    "SubtitleCue",
    "partition_cues",
    "compile_subtitles",
    "serialize_srt",
    "serialize_vtt",
    "serialize_ass",
    "parse_srt",
    "parse_vtt",
    "subtitle_sha256",
    "COLOR_HANDLERS",
    "bind_color_operations",
]
