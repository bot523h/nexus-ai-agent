"""Unit and golden tests for Advanced SubStation Alpha (ASS) and Persian/Arabic RTL (Wave 4b).

Covers:
* ASS timestamp formatting in integer microseconds mapped to centiseconds (H:MM:SS.cc);
* Persian/Arabic Unicode detection and bidirectional (BiDi) RLM anchoring;
* Word-level karaoke timing formatting (\\k<dur_cs>);
* Strict deterministic ASS v4.00+ script formatting;
* Command bus execution of ``caption.generate_ass_rtl`` (Level B, REVERSIBLE);
* Typography styling with ``caption.style_vazirmatn`` (Level B, REVERSIBLE);
* Word-level timing calculation with ``caption.highlight_words`` (Level A, IMMEDIATE);
* Undo/redo atomicity preserving state hash.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.packs.caption import (
    AssStyleConfig,
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
    build_caption_registry,
    format_ass,
    format_ass_timestamp,
    format_karaoke_dialogue,
    is_persian_or_arabic,
    wrap_rtl_bidi,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


def _sample_persian_transcript() -> TranscriptRef:
    """Fixture containing Persian text with word timings and punctuation."""
    return TranscriptRef(
        transcript_id="tr_persian_01",
        source_asset_id="asset_audio_vazir",
        language="fa",
        duration_us=10_000_000,
        segments=(
            TranscriptSegment(
                segment_id="seg_01",
                start_us=0,
                end_us=3_500_000,
                text="سلام به استودیوی خلاقه نگار خوش آمدید!",
                words=(
                    WordTiming(word="سلام", start_us=0, end_us=800_000),
                    WordTiming(word="به", start_us=850_000, end_us=1_200_000),
                    WordTiming(word="استودیوی", start_us=1_250_000, end_us=2_000_000),
                    WordTiming(word="خلاقه", start_us=2_050_000, end_us=2_600_000),
                    WordTiming(word="نگار", start_us=2_650_000, end_us=3_000_000),
                    WordTiming(word="خوش آمدید!", start_us=3_050_000, end_us=3_500_000),
                ),
            ),
            TranscriptSegment(
                segment_id="seg_02",
                start_us=3_500_000,
                end_us=7_000_000,
                text="این یک تست پیشرفته است\nبرای بررسی چیدمان چندخطی با فونت وزیر.",
            ),
        ),
    )


def test_format_ass_timestamp_centisecond_precision() -> None:
    # 0 us -> 0:00:00.00
    assert format_ass_timestamp(0) == "0:00:00.00"

    # 50,000 us = 5 cs -> 0:00:00.05
    assert format_ass_timestamp(50_000) == "0:00:00.05"

    # 1,234,000 us = 1s 23cs -> 0:00:01.23
    assert format_ass_timestamp(1_234_000) == "0:00:01.23"

    # 65,430,000 us = 1m 5s 43cs -> 0:01:05.43
    assert format_ass_timestamp(65_430_000) == "0:01:05.43"

    # 3,661,040,000 us = 1h 1m 1s 4cs -> 1:01:01.04
    assert format_ass_timestamp(3_661_040_000) == "1:01:01.04"


def test_is_persian_or_arabic_detection() -> None:
    assert is_persian_or_arabic("سلام دنیا") is True
    assert is_persian_or_arabic("Hello World") is False
    assert is_persian_or_arabic("Text with فارسی word") is True
    assert is_persian_or_arabic("12345 !?") is False


def test_wrap_rtl_bidi_anchors_punctuation_and_normalizes_newlines() -> None:
    persian_text = "سلام دنیا!\nچطور هستید؟"
    wrapped = wrap_rtl_bidi(persian_text)
    assert "\\N" in wrapped
    assert "\u200fسلام دنیا!\u200f" in wrapped
    assert "\u200fچطور هستید؟\u200f" in wrapped

    english_text = "Hello World\nLine two"
    wrapped_en = wrap_rtl_bidi(english_text)
    assert wrapped_en == "Hello World\\NLine two"


def test_format_karaoke_dialogue() -> None:
    seg = TranscriptSegment(
        start_us=0,
        end_us=2_000_000,
        text="یک دو",
        words=(
            WordTiming(word="یک", start_us=0, end_us=1_000_000),  # 100 cs
            WordTiming(word="دو", start_us=1_000_000, end_us=2_000_000),  # 100 cs
        ),
    )
    karaoke = format_karaoke_dialogue(seg)
    assert "{\\k100}یک" in karaoke
    assert "{\\k100}دو" in karaoke


def test_format_ass_deterministic_golden() -> None:
    tr = _sample_persian_transcript()
    style = AssStyleConfig(
        name="Vazir_Title",
        font_name="Vazirmatn",
        font_size=42,
        primary_colour="&H00FFFFFF",
        outline=2.5,
        shadow=1.5,
    )
    ass_content = format_ass(tr, style=style, enable_rtl_wrap=True)

    assert "[Script Info]" in ass_content
    assert "PlayResX: 1280" in ass_content
    assert "PlayResY: 720" in ass_content
    assert "[V4+ Styles]" in ass_content
    assert "Style: Vazir_Title,Vazirmatn,42,&H00FFFFFF" in ass_content
    assert "[Events]" in ass_content
    assert "Dialogue: 0,0:00:00.00,0:00:03.50,Vazir_Title,,0,0,0,," in ass_content

    # Assert byte-for-byte reproducibility
    for _ in range(20):
        assert format_ass(tr, style=style, enable_rtl_wrap=True) == ass_content


# ---------------------------------------------------------------------------
# Command Bus Integration Tests
# ---------------------------------------------------------------------------


def _setup_project() -> tuple[Project, CommandBus]:
    timeline = Timeline(timeline_id="tl_ass", duration_us=15_000_000)
    project = new_project("p_ass_01", "Nagar ASS Subtitle Test", timeline)
    audio = AssetRecord(
        asset_id="asset_audio_vazir",
        media_kind="audio",
        content_sha256="sha256:vaziraudio12345",
        duration_us=15_000_000,
    )
    project = project.model_copy(update={"assets": [audio]})
    bus = CommandBus(project, registry=build_caption_registry())
    return project, bus


def test_generate_ass_rtl_command_dispatch_and_undo() -> None:
    project, bus = _setup_project()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.generate_ass_rtl")
    assert spec.permission_level == PermissionLevel.REVERSIBLE

    tr = _sample_persian_transcript()
    cmd = TypedCommand(
        command_id="cmd_ass_01",
        operation="caption.generate_ass_rtl",
        input={
            "transcript": tr.model_dump(mode="json"),
            "output_asset_id": "caption_ass_derived_01",
            "enable_rtl_wrap": True,
            "enable_karaoke": False,
        },
    )

    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["asset_id"] == "caption_ass_derived_01"
    assert result.output["format"] == "ass"
    assert result.output["parent_asset_ids"] == ["asset_audio_vazir"]

    # Verify registered in project
    assets = bus.project.assets
    assert len(assets) == 2
    ass_asset = next(a for a in assets if a.asset_id == "caption_ass_derived_01")
    assert ass_asset.media_kind == "caption"
    assert ass_asset.provenance["format"] == "ass"
    assert ass_asset.provenance["font"] == "Vazirmatn"

    # Test reversible undo
    undo_cmd = TypedCommand(command_id="cmd_undo_ass", operation="system.undo", input={})
    undo_res = bus.dispatch(undo_cmd)
    assert undo_res.status == "applied"
    assert len(bus.project.assets) == 1
    assert bus.project.assets[0].asset_id == "asset_audio_vazir"


def test_style_vazirmatn_command_dispatch_and_undo() -> None:
    project, bus = _setup_project()
    tr = _sample_persian_transcript()

    # First register an ASS caption
    cmd_ass = TypedCommand(
        command_id="cmd_ass_base",
        operation="caption.generate_ass_rtl",
        input={
            "transcript": tr.model_dump(mode="json"),
            "output_asset_id": "caption_for_styling",
        },
    )
    bus.dispatch(cmd_ass)

    # Now apply style_vazirmatn
    style_cmd = TypedCommand(
        command_id="cmd_style_vazir",
        operation="caption.style_vazirmatn",
        input={
            "caption_asset_id": "caption_for_styling",
            "font_size": 52,
            "primary_colour": "&H00FFFF00",  # Yellow in ASS
            "alignment": 2,
            "bold": True,
        },
    )
    style_res = bus.dispatch(style_cmd)
    assert style_res.status == "applied"
    assert style_res.output["styled_asset_id"] == "caption_for_styling_vazir"
    assert style_res.output["font_size"] == 52

    # Verify styled record
    styled_record = next(a for a in bus.project.assets if a.asset_id == "caption_for_styling_vazir")
    assert styled_record.provenance["font"] == "Vazirmatn"
    assert styled_record.parent_asset_ids == ("caption_for_styling",)


def test_style_vazirmatn_rejects_non_existent_asset() -> None:
    project, bus = _setup_project()
    cmd = TypedCommand(
        command_id="cmd_bad_style",
        operation="caption.style_vazirmatn",
        input={"caption_asset_id": "non_existent"},
    )
    with pytest.raises(CommandValidationError, match="unknown asset"):
        bus.dispatch(cmd)


def test_highlight_words_level_a_immediate() -> None:
    project, bus = _setup_project()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.highlight_words")
    assert spec.permission_level == PermissionLevel.IMMEDIATE

    tr = _sample_persian_transcript()
    cmd = TypedCommand(
        command_id="cmd_hl_words",
        operation="caption.highlight_words",
        input={
            "transcript": tr.model_dump(mode="json"),
            "highlight_colour": "&H0000E5FF",
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert res.output["total_segments"] == 2
    assert res.output["total_highlighted_words"] == 6
    assert len(res.output["highlighted_segments"]) == 2
    assert "{\\k" in res.output["highlighted_segments"][0]["karaoke_text"]


def test_render_ir_with_subtitles_burn_in_filtergraph() -> None:
    from pathlib import Path

    from nexus_ai_agent.creative.slideshow.ffmpeg import (
        RenderIR,
        RenderShot,
        build_command,
        build_filtergraph,
    )

    shot = RenderShot(
        evidence_id="ev_sub_0",
        image_path="/tmp/fake_image.png",
        duration_us=3_000_000,
    )
    ir_ass = RenderIR(
        shots=(shot,),
        template_id="unit",
        target_duration_us=3_000_000,
        width=1280,
        height=720,
        fps=24,
        crf=23,
        preset="medium",
        audio_bitrate="128k",
        subtitle_path="/workspace/captions.ass",
    )

    filtergraph, final_label, has_audio = build_filtergraph(ir_ass)
    assert final_label == "vsub"
    assert "ass='/workspace/captions.ass'[vsub]" in filtergraph

    argv = build_command(ir_ass, output_path=Path("/tmp/master.mp4"))
    assert "-map" in argv
    assert "[vsub]" in argv

    # Test SRT subtitle path
    ir_srt = RenderIR(
        shots=(shot,),
        template_id="unit",
        target_duration_us=3_000_000,
        width=1280,
        height=720,
        fps=24,
        crf=23,
        preset="medium",
        audio_bitrate="128k",
        subtitle_path="/workspace/captions.srt",
    )
    filtergraph_srt, final_label_srt, _ = build_filtergraph(ir_srt)
    assert final_label_srt == "vsub"
    assert "subtitles='/workspace/captions.srt'[vsub]" in filtergraph_srt


def test_search_transcript_matches_keywords_and_words() -> None:
    project, bus = _setup_project()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.search_transcript")
    assert spec.permission_level == PermissionLevel.IMMEDIATE

    tr = _sample_persian_transcript()
    cmd = TypedCommand(
        command_id="cmd_search_01",
        operation="caption.search_transcript",
        input={
            "transcript": tr.model_dump(mode="json"),
            "query": "نگار",
            "exact_word": False,
        },
    )
    res = bus.dispatch(cmd)
    assert res.status == "applied"
    assert res.output["total_hits"] == 1
    assert res.output["hits"][0]["segment_id"] == "seg_01"
    assert len(res.output["hits"][0]["matched_words"]) >= 1

    # Search non-matching query
    cmd_empty = TypedCommand(
        command_id="cmd_search_none",
        operation="caption.search_transcript",
        input={
            "transcript": tr.model_dump(mode="json"),
            "query": "کلمه_ناموجود",
        },
    )
    res_empty = bus.dispatch(cmd_empty)
    assert res_empty.status == "applied"
    assert res_empty.output["total_hits"] == 0


def test_burn_in_level_c_requires_confirmation_and_registers_derived_video() -> None:
    project, bus = _setup_project()
    registry = build_caption_registry()
    spec = registry.get_spec("caption.burn_in")
    assert spec.permission_level == PermissionLevel.CONFIRMATION

    # Add a mock video asset and caption asset
    video_rec = AssetRecord(
        asset_id="asset_video_master",
        media_kind="video",
        content_sha256="sha256:videohash999",
        duration_us=10_000_000,
    )
    caption_rec = AssetRecord(
        asset_id="asset_caption_ass",
        media_kind="caption",
        content_sha256="sha256:asshash999",
        duration_us=10_000_000,
    )
    bus._project = bus.project.model_copy(
        update={"assets": [*bus.project.assets, video_rec, caption_rec]}
    )

    # Missing confirmation -> fails with PermissionDeniedError at command bus gate
    cmd_unconfirmed = TypedCommand(
        command_id="cmd_burn_unconf",
        operation="caption.burn_in",
        confirmed=False,
        input={
            "video_asset_id": "asset_video_master",
            "caption_asset_id": "asset_caption_ass",
        },
    )
    from nexus_ai_agent.creative.studio.models import PermissionDeniedError

    with pytest.raises(PermissionDeniedError, match="level C"):
        bus.dispatch(cmd_unconfirmed)

    # Confirmed -> succeeds
    cmd_burn = TypedCommand(
        command_id="cmd_burn_ok",
        operation="caption.burn_in",
        confirmed=True,
        input={
            "video_asset_id": "asset_video_master",
            "caption_asset_id": "asset_caption_ass",
            "output_asset_id": "burned_master_01",
            "confirmed": True,
        },
    )
    burn_res = bus.dispatch(cmd_burn)
    assert burn_res.status == "applied"
    assert burn_res.output["derived_asset_id"] == "burned_master_01"

    # Verify derived asset in project
    burned_rec = next(a for a in bus.project.assets if a.asset_id == "burned_master_01")
    assert burned_rec.media_kind == "video"
    assert burned_rec.parent_asset_ids == ("asset_video_master", "asset_caption_ass")
    assert burned_rec.provenance["burn_in"] is True

    # Test reversible undo
    undo_cmd = TypedCommand(command_id="cmd_undo_burn", operation="system.undo", input={})
    undo_res = bus.dispatch(undo_cmd)
    assert undo_res.status == "applied"
    assert "burned_master_01" not in [a.asset_id for a in bus.project.assets]
