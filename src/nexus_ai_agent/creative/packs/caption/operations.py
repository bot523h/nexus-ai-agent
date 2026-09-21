"""Pure operations for the ``nexus.language.caption`` pack.

Wave 4a registers two operations:
* ``caption.transcribe`` (Level A, IMMEDIATE): Validates and associates a
  transcript reference with an audio asset (evidence passed above the bus).
* ``caption.generate_srt`` (Level B, REVERSIBLE): Pure, deterministic conversion
  of a transcript into a SubRip (.srt) asset with a WebVTT (.vtt) companion
  rendition attached to the same asset record.
"""

from __future__ import annotations

import hashlib
import uuid

from nexus_ai_agent.creative.packs.caption.formatters import (
    format_ass,
    format_karaoke_dialogue,
    format_srt,
    format_vtt,
)
from nexus_ai_agent.creative.packs.caption.models import (
    CAPTION_PACKAGE_ID,
    OPERATION_GENERATE_ASS,
    OPERATION_GENERATE_SRT,
    OPERATION_HIGHLIGHT_WORDS,
    OPERATION_STYLE_VAZIRMATN,
    OPERATION_TRANSCRIBE,
    AssStyleConfig,
    CaptionAsset,
    GenerateAssInput,
    GenerateSrtInput,
    HighlightWordsInput,
    StyleVazirmatnInput,
    TranscribeInput,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
)

DOMAIN = "caption"


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {a.asset_id: a for a in project.assets}


def _transcribe(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.transcribe."""
    payload = TranscribeInput.model_validate(context.input_data)
    if project.assets:
        known = _asset_index(project)
        if payload.audio_asset_id not in known:
            raise CommandValidationError(
                f"caption.transcribe references unknown audio asset: {payload.audio_asset_id!r}"
            )

    if payload.transcript is None:
        raise CommandValidationError(
            "caption.transcribe requires pinned transcript evidence in Wave 4a substrate; "
            "provide 'transcript' or use a configured caption engine adapter"
        )

    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": payload.transcript.model_dump(mode="json"),
            "transcript_id": payload.transcript.transcript_id,
            "audio_asset_id": payload.audio_asset_id,
            "language": payload.transcript.language,
            "segment_count": len(payload.transcript.segments),
        },
    )


def _generate_srt(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for caption.generate_srt.

    Produces a CaptionAsset containing the pure SRT string as primary content
    and WebVTT as a companion rendition on the same asset.
    """
    payload = GenerateSrtInput.model_validate(context.input_data)
    transcript = payload.transcript

    if project.assets and transcript.source_asset_id:
        known = _asset_index(project)
        if transcript.source_asset_id not in known:
            raise CommandValidationError(
                f"caption.generate_srt transcript references unknown source asset: "
                f"{transcript.source_asset_id!r}"
            )

    srt_text = format_srt(transcript)
    srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()

    companion_renditions: dict[str, str] = {}
    companion_hashes: dict[str, str] = {}

    if payload.include_vtt:
        vtt_text = format_vtt(transcript)
        vtt_sha256 = hashlib.sha256(vtt_text.encode("utf-8")).hexdigest()
        companion_renditions["vtt"] = vtt_text
        companion_hashes["vtt"] = vtt_sha256

    asset_id = payload.output_asset_id or f"caption_{uuid.uuid4().hex[:12]}"

    caption_asset = CaptionAsset(
        asset_id=asset_id,
        source_transcript_id=transcript.transcript_id,
        content=srt_text,
        content_sha256=srt_sha256,
        format="srt",
        companion_renditions=companion_renditions,
        companion_hashes=companion_hashes,
        language=transcript.language,
        line_count=len(transcript.segments),
        duration_us=transcript.duration_us,
    )

    parents = (transcript.source_asset_id,) if transcript.source_asset_id else ()
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="caption",
        content_sha256=srt_sha256,
        duration_us=transcript.duration_us,
        parent_asset_ids=parents,
        provenance={
            "format": "srt",
            "companion_renditions": companion_hashes,
            "source_transcript_id": transcript.transcript_id,
            "language": transcript.language,
            "produced_by": "nagar.local.caption.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": asset_id,
            "caption_asset": caption_asset.model_dump(mode="json"),
            "srt_sha256": srt_sha256,
            "vtt_sha256": companion_hashes.get("vtt"),
            "has_vtt_companion": payload.include_vtt,
            "is_derived": bool(parents),
            "parent_asset_ids": list(parents),
        },
    )


def _generate_ass_rtl(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for caption.generate_ass_rtl.

    Generates Advanced SubStation Alpha (.ass) format with bidirectional
    Persian/Arabic typography, Vazirmatn layout, and optional karaoke word tags.
    """
    payload = GenerateAssInput.model_validate(context.input_data)
    transcript = payload.transcript

    if project.assets and transcript.source_asset_id:
        known = _asset_index(project)
        if transcript.source_asset_id not in known:
            raise CommandValidationError(
                f"caption.generate_ass_rtl transcript references unknown source asset: "
                f"{transcript.source_asset_id!r}"
            )

    ass_text = format_ass(
        transcript,
        style=payload.style,
        enable_karaoke=payload.enable_karaoke,
        enable_rtl_wrap=payload.enable_rtl_wrap,
        play_res_x=payload.play_res_x,
        play_res_y=payload.play_res_y,
    )
    ass_sha256 = hashlib.sha256(ass_text.encode("utf-8")).hexdigest()

    asset_id = payload.output_asset_id or f"caption_ass_{uuid.uuid4().hex[:12]}"

    caption_asset = CaptionAsset(
        asset_id=asset_id,
        source_transcript_id=transcript.transcript_id,
        content=ass_text,
        content_sha256=ass_sha256,
        format="ass",
        companion_renditions={},
        companion_hashes={},
        language=transcript.language,
        line_count=len(transcript.segments),
        duration_us=transcript.duration_us,
    )

    parents = (transcript.source_asset_id,) if transcript.source_asset_id else ()
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="caption",
        content_sha256=ass_sha256,
        duration_us=transcript.duration_us,
        parent_asset_ids=parents,
        provenance={
            "format": "ass",
            "source_transcript_id": transcript.transcript_id,
            "language": transcript.language,
            "font": payload.style.font_name if payload.style else "Vazirmatn",
            "karaoke": payload.enable_karaoke,
            "produced_by": "nagar.local.caption.ass.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": asset_id,
            "caption_asset": caption_asset.model_dump(mode="json"),
            "ass_sha256": ass_sha256,
            "format": "ass",
            "is_derived": bool(parents),
            "parent_asset_ids": list(parents),
            "line_count": len(transcript.segments),
        },
    )


def _style_vazirmatn(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for caption.style_vazirmatn.

    Applies Vazirmatn font styling and layout parameters to caption assets.
    """
    payload = StyleVazirmatnInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.caption_asset_id not in known:
        raise CommandValidationError(
            f"caption.style_vazirmatn references unknown asset: {payload.caption_asset_id!r}"
        )

    target_asset = known[payload.caption_asset_id]
    if target_asset.media_kind != "caption":
        raise CommandValidationError(
            f"caption.style_vazirmatn requires a caption asset, got: {target_asset.media_kind!r}"
        )

    style_cfg = AssStyleConfig(
        name="Vazirmatn_Custom",
        font_name="Vazirmatn",
        font_size=payload.font_size,
        primary_colour=payload.primary_colour,
        outline_colour=payload.outline_colour,
        back_colour=payload.shadow_colour,
        alignment=payload.alignment,
        bold=payload.bold,
    )

    styled_asset_id = f"{payload.caption_asset_id}_vazir"
    styled_record = AssetRecord(
        asset_id=styled_asset_id,
        media_kind="caption",
        content_sha256=target_asset.content_sha256,
        duration_us=target_asset.duration_us,
        parent_asset_ids=(target_asset.asset_id,),
        provenance={
            **target_asset.provenance,
            "font": "Vazirmatn",
            "style_config": style_cfg.model_dump(mode="json"),
            "styled_by": "caption.style_vazirmatn",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, styled_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "styled_asset_id": styled_asset_id,
            "parent_asset_id": target_asset.asset_id,
            "font_name": "Vazirmatn",
            "font_size": payload.font_size,
            "style": style_cfg.model_dump(mode="json"),
        },
    )


def _highlight_words(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.highlight_words.

    Computes word-level highlight tags and karaoke cue spans for real-time preview.
    """
    payload = HighlightWordsInput.model_validate(context.input_data)
    transcript = payload.transcript

    highlighted_segments: list[dict[str, object]] = []
    total_highlighted_words = 0

    for seg in transcript.segments:
        karaoke_text = format_karaoke_dialogue(seg)
        word_count = len(seg.words)
        total_highlighted_words += word_count
        highlighted_segments.append(
            {
                "segment_id": seg.segment_id,
                "start_us": seg.start_us,
                "end_us": seg.end_us,
                "karaoke_text": karaoke_text,
                "word_count": word_count,
            }
        )

    return OperationOutcome(
        project,
        context.history,
        {
            "transcript_id": transcript.transcript_id,
            "total_segments": len(transcript.segments),
            "total_highlighted_words": total_highlighted_words,
            "highlight_colour": payload.highlight_colour,
            "highlighted_segments": highlighted_segments,
        },
    )


# ---------------------------------------------------------------------------
# Registration and runtime building
# ---------------------------------------------------------------------------


def register_caption_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register Wave 4 caption operations on an existing capability registry."""
    registry.register_domain(
        DOMAIN, "Local Persian and multilingual captions (pack nexus.language.caption)"
    )
    registry.register_operation(
        DOMAIN,
        "transcribe",
        OperationSpec(
            operation_id=OPERATION_TRANSCRIBE,
            description="Transcribe audio to a structured transcript reference (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=TranscribeInput,
            handler=_transcribe,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "generate_srt",
        OperationSpec(
            operation_id=OPERATION_GENERATE_SRT,
            description="Generate deterministic SRT with VTT companion rendition (level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=GenerateSrtInput,
            handler=_generate_srt,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "generate_ass_rtl",
        OperationSpec(
            operation_id=OPERATION_GENERATE_ASS,
            description="Generate Advanced SubStation Alpha (.ass) with Persian/RTL formatting (level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=GenerateAssInput,
            handler=_generate_ass_rtl,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "style_vazirmatn",
        OperationSpec(
            operation_id=OPERATION_STYLE_VAZIRMATN,
            description="Apply Vazirmatn typography styling to caption assets (level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=StyleVazirmatnInput,
            handler=_style_vazirmatn,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "highlight_words",
        OperationSpec(
            operation_id=OPERATION_HIGHLIGHT_WORDS,
            description="Compute word-level karaoke and highlight timings (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=HighlightWordsInput,
            handler=_highlight_words,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    return registry


def build_caption_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry with Wave 1, Slideshow and Caption operations."""
    from nexus_ai_agent.creative.packs.slideshow.operations import build_slideshow_registry

    return register_caption_operations(build_slideshow_registry())


__all__ = [
    "DOMAIN",
    "OPERATION_GENERATE_ASS",
    "OPERATION_GENERATE_SRT",
    "OPERATION_HIGHLIGHT_WORDS",
    "OPERATION_STYLE_VAZIRMATN",
    "OPERATION_TRANSCRIBE",
    "build_caption_registry",
    "register_caption_operations",
]
