"""Unit and golden tests for the ``nexus.language.caption`` pack (Wave 4a).

Covers:
* typed models (:class:`WordTiming`, :class:`TranscriptSegment`,
  :class:`SpeakerTurn`, :class:`TranscriptRef`, :class:`CaptionAsset`);
* byte-identical SRT and WebVTT golden formatters;
* Unicode and Persian (Farsi) text preservation and RTL characters;
* multiline segment handling and WebVTT escaping (&, <, >);
* microsecond timestamp rollover across minutes, hours, and days;
* pure command bus operations: ``caption.transcribe`` (A) and
  ``caption.generate_srt`` (B, reversible, atomic asset registration, undo);
* fail-closed unavailable caption engine adapter (no silent fallback).
"""

from __future__ import annotations

import hashlib

import pytest
from nagar_helpers import authorized_bus, command_for

from nexus_ai_agent.application.ports.caption_engine import (
    CaptionProfileUnavailableError,
)
from nexus_ai_agent.creative.caption import (
    UnavailableCaptionAdapter,
    UnavailableCaptionEngine,
)
from nexus_ai_agent.creative.packs.caption import (
    CaptionAsset,
    SpeakerTurn,
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
    build_caption_registry,
    escape_vtt,
    format_srt,
    format_timestamp,
    format_vtt,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
    Timeline,
    new_project,
)


def _golden_transcript() -> TranscriptRef:
    """Deterministic golden fixture exercising Unicode, Persian, multiline and rollover."""
    return TranscriptRef(
        transcript_id="tr_golden_fa_01",
        source_asset_id="asset_audio_master",
        language="fa-IR",
        duration_us=3665_500_000,
        segments=(
            TranscriptSegment(
                segment_id="seg_01",
                start_us=0,
                end_us=2_500_000,
                text="سلام دنیا",
                words=(
                    WordTiming(word="سلام", start_us=0, end_us=1_000_000, score=0.98),
                    WordTiming(word="دنیا", start_us=1_100_000, end_us=2_500_000, score=0.95),
                ),
                speaker="spk_0",
                score=0.965,
            ),
            TranscriptSegment(
                segment_id="seg_02",
                start_us=2_500_000,
                end_us=5_000_000,
                text="این یک خط آزمایشی است\nکه به صورت چندخطی نوشته شده است.",
                speaker="spk_0",
            ),
            TranscriptSegment(
                segment_id="seg_03",
                start_us=5_000_000,
                end_us=7_123_000,
                text="آزمایش کاراکترهای خاص: متن فارسی با فونت وزیر & <نمونه>",
                speaker="spk_1",
            ),
            TranscriptSegment(
                segment_id="seg_04",
                start_us=3661_040_000,  # 1h 1m 1s 40ms
                end_us=3665_500_000,  # 1h 1m 5s 500ms
                text="پایان گفتگو بیش از یک ساعت بعد.",
                speaker="spk_0",
            ),
        ),
        speaker_turns=(
            SpeakerTurn(speaker="spk_0", start_us=0, end_us=5_000_000),
            SpeakerTurn(speaker="spk_1", start_us=5_000_000, end_us=7_123_000),
            SpeakerTurn(speaker="spk_0", start_us=3661_040_000, end_us=3665_500_000),
        ),
    )


# ---------------------------------------------------------------------------
# Typed models unit tests
# ---------------------------------------------------------------------------


def test_word_timing_validates_bounds_and_exposes_properties() -> None:
    timing = WordTiming(word="سلام", start_us=1_000_000, end_us=2_500_000, score=0.99)
    assert timing.duration_us == 1_500_000
    assert timing.start_s == 1.0
    assert timing.end_s == 2.5

    with pytest.raises(ValueError, match="end_us"):
        WordTiming(word="خطا", start_us=5_000_000, end_us=4_000_000)


def test_transcript_segment_bounds_and_properties() -> None:
    seg = TranscriptSegment(start_us=500_000, end_us=1_500_000, text="آزمایش")
    assert seg.duration_us == 1_000_000
    assert seg.start_s == 0.5
    assert seg.end_s == 1.5

    with pytest.raises(ValueError, match="end_us"):
        TranscriptSegment(start_us=2_000_000, end_us=1_000_000, text="برعکس")


def test_speaker_turn_bounds() -> None:
    turn = SpeakerTurn(speaker="مجری", start_us=0, end_us=10_000_000, text="شروع برنامه")
    assert turn.duration_us == 10_000_000
    with pytest.raises(ValueError, match="end_us"):
        SpeakerTurn(speaker="مجری", start_us=10_000_000, end_us=0)


def test_transcript_ref_text_and_is_empty() -> None:
    empty = TranscriptRef(transcript_id="tr_empty", source_asset_id="a1")
    assert empty.is_empty is True
    assert empty.text == ""

    filled = _golden_transcript()
    assert filled.is_empty is False
    assert "سلام دنیا" in filled.text


def test_caption_asset_renditions_and_hashes() -> None:
    srt_text = "1\n00:00:00,000 --> 00:00:01,000\nسلام\n"
    vtt_text = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nسلام\n"
    srt_hash = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    vtt_hash = hashlib.sha256(vtt_text.encode("utf-8")).hexdigest()

    asset = CaptionAsset(
        asset_id="cap_01",
        content=srt_text,
        content_sha256=srt_hash,
        format="srt",
        companion_renditions={"vtt": vtt_text},
        companion_hashes={"vtt": vtt_hash},
        language="fa",
    )

    assert asset.has_rendition("srt") is True
    assert asset.has_rendition("vtt") is True
    assert asset.has_rendition("ass") is False

    assert asset.rendition("srt") == srt_text
    assert asset.rendition("vtt") == vtt_text
    assert asset.rendition_hash("srt") == srt_hash
    assert asset.rendition_hash("vtt") == vtt_hash

    with pytest.raises(KeyError, match="ass"):
        asset.rendition("ass")
    with pytest.raises(KeyError, match="ass"):
        asset.rendition_hash("ass")


# ---------------------------------------------------------------------------
# Golden byte-identical tests: SRT and WebVTT
# ---------------------------------------------------------------------------


EXPECTED_GOLDEN_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,500\n"
    "سلام دنیا\n"
    "\n"
    "2\n"
    "00:00:02,500 --> 00:00:05,000\n"
    "این یک خط آزمایشی است\n"
    "که به صورت چندخطی نوشته شده است.\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:07,123\n"
    "آزمایش کاراکترهای خاص: متن فارسی با فونت وزیر & <نمونه>\n"
    "\n"
    "4\n"
    "01:01:01,040 --> 01:01:05,500\n"
    "پایان گفتگو بیش از یک ساعت بعد.\n"
)

EXPECTED_GOLDEN_VTT = (
    "WEBVTT\n"
    "\n"
    "1\n"
    "00:00:00.000 --> 00:00:02.500\n"
    "سلام دنیا\n"
    "\n"
    "2\n"
    "00:00:02.500 --> 00:00:05.000\n"
    "این یک خط آزمایشی است\n"
    "که به صورت چندخطی نوشته شده است.\n"
    "\n"
    "3\n"
    "00:00:05.000 --> 00:00:07.123\n"
    "آزمایش کاراکترهای خاص: متن فارسی با فونت وزیر &amp; &lt;نمونه&gt;\n"
    "\n"
    "4\n"
    "01:01:01.040 --> 01:01:05.500\n"
    "پایان گفتگو بیش از یک ساعت بعد.\n"
)


def test_golden_srt_formatting_is_byte_identical() -> None:
    transcript = _golden_transcript()
    formatted = format_srt(transcript)
    assert formatted == EXPECTED_GOLDEN_SRT

    # Guarantee repeat runs yield byte-identical results
    for _ in range(50):
        assert format_srt(transcript).encode("utf-8") == EXPECTED_GOLDEN_SRT.encode("utf-8")


def test_golden_vtt_formatting_is_byte_identical() -> None:
    transcript = _golden_transcript()
    formatted = format_vtt(transcript)
    assert formatted == EXPECTED_GOLDEN_VTT

    # Guarantee repeat runs yield byte-identical results
    for _ in range(50):
        assert format_vtt(transcript).encode("utf-8") == EXPECTED_GOLDEN_VTT.encode("utf-8")


def test_empty_transcript_formatting() -> None:
    empty = TranscriptRef(transcript_id="tr_empty", source_asset_id="a1")
    assert format_srt(empty) == ""
    assert format_vtt(empty) == "WEBVTT\n"


def test_timestamp_rollover_edge_cases() -> None:
    # 0 us
    assert format_timestamp(0, decimal_sep=",") == "00:00:00,000"
    assert format_timestamp(0, decimal_sep=".") == "00:00:00.000"

    # sub-second (999 ms)
    assert format_timestamp(999_000, decimal_sep=",") == "00:00:00,999"

    # exactly 1 second
    assert format_timestamp(1_000_000, decimal_sep=",") == "00:00:01,000"

    # rollover to 1 minute
    assert format_timestamp(59_999_000, decimal_sep=",") == "00:00:59,999"
    assert format_timestamp(60_000_000, decimal_sep=",") == "00:01:00,000"

    # rollover to 1 hour
    assert format_timestamp(3_599_999_000, decimal_sep=",") == "00:59:59,999"
    assert format_timestamp(3_600_000_000, decimal_sep=",") == "01:00:00,000"

    # 24 hours rollover
    assert format_timestamp(86_400_000_000, decimal_sep=",") == "24:00:00,000"

    # 100 hours rollover
    assert format_timestamp(360_000_000_000, decimal_sep=",") == "100:00:00,000"


def test_persian_zwnj_and_multiline_preservation() -> None:
    text_with_zwnj = "می\u200cخواهم یک زیرنویس با نیم‌فاصله ایجاد کنم.\nسطر دوم با جزئیات بیشتر."
    seg = TranscriptSegment(start_us=0, end_us=3_000_000, text=text_with_zwnj)
    srt = format_srt([seg])
    assert text_with_zwnj in srt
    assert "\u200c" in srt


def test_vtt_escaping() -> None:
    assert escape_vtt("A & B < C > D") == "A &amp; B &lt; C &gt; D"


# ---------------------------------------------------------------------------
# Command Bus operations: caption.transcribe & caption.generate_srt
# ---------------------------------------------------------------------------


def _setup_project_with_audio() -> tuple[Project, CommandBus]:
    timeline = Timeline(timeline_id="tl_01", duration_us=30_000_000)
    project = new_project("project_01", "Caption Studio Test", timeline)
    audio_record = AssetRecord(
        asset_id="asset_audio_01",
        media_kind="audio",
        content_sha256="sha256:fakeaudiohash123",
        duration_us=30_000_000,
    )
    project = project.model_copy(update={"assets": [audio_record]})
    registry = build_caption_registry()
    bus = authorized_bus(project, registry=registry)
    return project, bus


def test_caption_transcribe_permission_level_a_and_execution() -> None:
    project, bus = _setup_project_with_audio()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.transcribe")
    assert spec.permission_level == PermissionLevel.IMMEDIATE

    transcript = _golden_transcript()
    cmd = command_for(
        bus,
        command_id="cmd_transcribe_01",
        operation="caption.transcribe",
        input={
            "audio_asset_id": "asset_audio_01",
            "language": "fa-IR",
            "transcript": transcript.model_dump(mode="json"),
        },
    )

    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["transcript_id"] == "tr_golden_fa_01"
    assert result.output["language"] == "fa-IR"
    assert result.output["segment_count"] == 4


def test_caption_transcribe_rejects_missing_evidence_or_unknown_audio() -> None:
    project, bus = _setup_project_with_audio()

    # Missing pinned transcript
    cmd_no_evidence = command_for(
        bus,
        command_id="cmd_no_ev",
        operation="caption.transcribe",
        input={"audio_asset_id": "asset_audio_01"},
    )
    with pytest.raises(CommandValidationError, match="requires pinned transcript evidence"):
        bus.dispatch(cmd_no_evidence)

    # Unknown audio asset
    transcript = _golden_transcript()
    cmd_bad_audio = command_for(
        bus,
        command_id="cmd_bad_audio",
        operation="caption.transcribe",
        input={
            "audio_asset_id": "non_existent_audio",
            "transcript": transcript.model_dump(mode="json"),
        },
    )
    with pytest.raises(CommandValidationError, match="unknown audio asset"):
        bus.dispatch(cmd_bad_audio)


def test_caption_generate_srt_produces_companion_vtt_and_derived_asset() -> None:
    project, bus = _setup_project_with_audio()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.generate_srt")
    assert spec.permission_level == PermissionLevel.REVERSIBLE

    transcript = _golden_transcript()
    # Align source_asset_id with registered audio
    transcript = transcript.model_copy(update={"source_asset_id": "asset_audio_01"})

    cmd = command_for(
        bus,
        command_id="cmd_gen_srt_01",
        operation="caption.generate_srt",
        input={
            "transcript": transcript.model_dump(mode="json"),
            "output_asset_id": "caption_derived_01",
            "include_vtt": True,
        },
    )

    result = bus.dispatch(cmd)
    assert result.status == "applied"

    output = result.output
    assert output["asset_id"] == "caption_derived_01"
    assert output["has_vtt_companion"] is True
    assert output["srt_sha256"] is not None
    assert output["vtt_sha256"] is not None
    assert output["parent_asset_ids"] == ["asset_audio_01"]

    # Verify asset registered on project
    new_project = bus.project
    caption_records = [a for a in new_project.assets if a.asset_id == "caption_derived_01"]
    assert len(caption_records) == 1
    record = caption_records[0]
    assert record.media_kind == "caption"
    assert record.is_derived is True
    assert record.parent_asset_ids == ("asset_audio_01",)
    assert record.provenance["format"] == "srt"
    assert "vtt" in record.provenance["companion_renditions"]

    # Test reversible undo: system.undo must restore original project state
    undo_cmd = command_for(
        bus,
        command_id="cmd_undo_srt",
        operation="system.undo",
        input={},
    )
    undo_result = bus.dispatch(undo_cmd)
    assert undo_result.status == "applied"
    assert len(bus.project.assets) == 1
    assert bus.project.assets[0].asset_id == "asset_audio_01"


# ---------------------------------------------------------------------------
# Unavailable Caption Adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_caption_adapter_fails_closed() -> None:
    adapter = UnavailableCaptionAdapter(reason="Speech profile not installed")
    assert adapter.is_available() is False

    with pytest.raises(CaptionProfileUnavailableError) as exc_info:
        await adapter.transcribe("/path/to/speech.wav", language="fa")

    assert exc_info.value.code == "caption_profile_unavailable"
    assert "Speech profile not installed" in str(exc_info.value)
    assert "speech.wav" in str(exc_info.value)

    # Alias check
    assert UnavailableCaptionEngine is UnavailableCaptionAdapter
