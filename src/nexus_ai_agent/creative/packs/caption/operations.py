"""Pure operations for the ``nexus.language.caption`` pack.

Wave 4a registers the core operations (``caption.transcribe``,
``caption.generate_srt``, ASS/RTL, styling, highlight, search, burn-in); Wave 9
completes the manifest with three more pure operations:
* ``caption.align_words`` (Level A, IMMEDIATE): Project word timings onto
  segment spans (length-proportional, exact microsecond cover).
* ``caption.diarize`` (Level A, IMMEDIATE): Gap-heuristic speaker turns —
  merge segments into turns first, *then* stamp speakers (single source of
  truth, also consumed by the local speech adapter).
* ``caption.translate_local`` (Level A, IMMEDIATE): Offline glossary/dictionary
  translation projection with honest coverage stats (the neural engine behind
  the adapter keeps timings; this is the deterministic substrate).
"""

from __future__ import annotations

import hashlib
import re
import uuid

from nexus_ai_agent.creative.packs.caption.formatters import (
    format_ass,
    format_karaoke_dialogue,
    format_srt,
    format_vtt,
)
from nexus_ai_agent.creative.packs.caption.models import (
    CAPTION_PACKAGE_ID,
    OPERATION_ALIGN_WORDS,
    OPERATION_BURN_IN,
    OPERATION_DIARIZE,
    OPERATION_GENERATE_ASS,
    OPERATION_GENERATE_SRT,
    OPERATION_HIGHLIGHT_WORDS,
    OPERATION_SEARCH_TRANSCRIPT,
    OPERATION_STYLE_VAZIRMATN,
    OPERATION_TRANSCRIBE,
    OPERATION_TRANSLATE_LOCAL,
    AlignWordsInput,
    AssStyleConfig,
    BurnInInput,
    CaptionAsset,
    DiarizeInput,
    GenerateAssInput,
    GenerateSrtInput,
    HighlightWordsInput,
    SearchTranscriptInput,
    SpeakerTurn,
    StyleVazirmatnInput,
    TranscribeInput,
    TranscriptRef,
    TranscriptSegment,
    TranslateLocalInput,
    WordTiming,
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


def _search_transcript(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.search_transcript.

    Searches across transcript segments and returns matching spans with
    microsecond boundary hits and word timing occurrences.
    """
    payload = SearchTranscriptInput.model_validate(context.input_data)
    transcript = payload.transcript
    query = payload.query if payload.case_sensitive else payload.query.lower()

    hits: list[dict[str, object]] = []
    for seg in transcript.segments:
        text = seg.text if payload.case_sensitive else seg.text.lower()
        if payload.exact_word:
            pattern = r"\b" + re.escape(query) + r"\b"
            matched = bool(re.search(pattern, text))
        else:
            matched = query in text

        if matched:
            matching_words = [
                w.model_dump(mode="json")
                for w in seg.words
                if (w.word if payload.case_sensitive else w.word.lower()) == query
                or (
                    not payload.exact_word
                    and query in (w.word if payload.case_sensitive else w.word.lower())
                )
            ]
            hits.append(
                {
                    "segment_id": seg.segment_id,
                    "start_us": seg.start_us,
                    "end_us": seg.end_us,
                    "text": seg.text,
                    "speaker": seg.speaker,
                    "matched_words": matching_words,
                }
            )

    return OperationOutcome(
        project,
        context.history,
        {
            "query": payload.query,
            "total_hits": len(hits),
            "hits": hits,
        },
    )


def _burn_in(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMED) handler for caption.burn_in.

    Associates a caption track with a video asset and derives a new burned-in
    video asset record in project state.
    """
    payload = BurnInInput.model_validate(context.input_data)
    if not payload.confirmed:
        raise CommandValidationError(
            "caption.burn_in requires explicit user confirmation (confirmed=true) in Level C"
        )

    known = _asset_index(project)
    if payload.video_asset_id not in known:
        raise CommandValidationError(
            f"caption.burn_in references unknown video asset: {payload.video_asset_id!r}"
        )
    if payload.caption_asset_id not in known:
        raise CommandValidationError(
            f"caption.burn_in references unknown caption asset: {payload.caption_asset_id!r}"
        )

    video_record = known[payload.video_asset_id]
    caption_record = known[payload.caption_asset_id]

    derived_asset_id = payload.output_asset_id or f"burnin_{uuid.uuid4().hex[:12]}"
    content_composite = f"{video_record.content_sha256}:{caption_record.content_sha256}"
    derived_sha256 = hashlib.sha256(content_composite.encode("utf-8")).hexdigest()

    burned_record = AssetRecord(
        asset_id=derived_asset_id,
        media_kind="video",
        content_sha256=derived_sha256,
        duration_us=video_record.duration_us,
        parent_asset_ids=(video_record.asset_id, caption_record.asset_id),
        provenance={
            "source_video_id": video_record.asset_id,
            "source_caption_id": caption_record.asset_id,
            "burn_in": True,
            "produced_by": "nagar.local.caption.burnin.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, burned_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "derived_asset_id": derived_asset_id,
            "video_asset_id": video_record.asset_id,
            "caption_asset_id": caption_record.asset_id,
            "derived_sha256": derived_sha256,
        },
    )


# ---------------------------------------------------------------------------
# Pure Wave 9 helpers (single source of truth — also consumed by adapters)
# ---------------------------------------------------------------------------


def project_word_timings(text: str, start_us: int, end_us: int) -> tuple[WordTiming, ...]:
    """Project word timings onto ``[start_us, end_us]`` (pure, deterministic).

    Words share the span proportionally to their character length (minimum
    weight 1); the largest-remainder method guarantees the words cover the
    span *exactly* (no drift, no gaps) with deterministic tie-breaking by
    word index.  A zero-length span stamps every word at ``start_us``.
    """
    words = text.split()
    if not words:
        return ()
    if end_us <= start_us:
        return tuple(WordTiming(word=w, start_us=start_us, end_us=start_us) for w in words)
    weights = [max(1, len(w)) for w in words]
    total = sum(weights)
    span = end_us - start_us
    floors = [(span * w) // total for w in weights]
    remainders = [(span * w) % total for w in weights]
    leftover = span - sum(floors)
    order = sorted(range(len(words)), key=lambda i: (-remainders[i], i))
    extra = [0] * len(words)
    for index in order[:leftover]:
        extra[index] = 1
    out: list[WordTiming] = []
    cursor = start_us
    for word, base, bump in zip(words, floors, extra, strict=True):
        nxt = cursor + base + bump
        out.append(WordTiming(word=word, start_us=cursor, end_us=nxt))
        cursor = nxt
    return tuple(out)


def merge_segments_by_gap(
    segments: tuple[TranscriptSegment, ...], gap_threshold_us: int
) -> tuple[tuple[TranscriptSegment, ...], ...]:
    """Group consecutive segments into turns (pure, deterministic).

    A segment joins the current turn while the silence gap between the
    previous segment's end and its start is ``<= gap_threshold_us``; a larger
    gap opens a new turn.  Overlapping segments (negative gap) never split.
    """
    if not segments:
        return ()
    turns: list[list[TranscriptSegment]] = [[segments[0]]]
    for seg in segments[1:]:
        gap = seg.start_us - turns[-1][-1].end_us
        if gap > gap_threshold_us:
            turns.append([seg])
        else:
            turns[-1].append(seg)
    return tuple(tuple(turn) for turn in turns)


def assign_speakers(turn_count: int, max_speakers: int) -> tuple[str, ...]:
    """Assign deterministic speaker labels round-robin (``SPEAKER_00`` …)."""
    return tuple(f"SPEAKER_{i % max_speakers:02d}" for i in range(turn_count))


_GLOSSARY_STRIP = ".,!?;:\"'()[]«»؟،"


def apply_glossary(text: str, glossary: dict[str, str]) -> tuple[str, int, int]:
    """Translate ``text`` word-by-word through ``glossary`` (pure, offline).

    Lookup is case-insensitive on the punctuation-stripped core; surrounding
    punctuation is preserved and unknown words pass through untouched.
    Returns ``(translated_text, hits, total_tokens)`` so callers can report
    honest coverage.  Whitespace is normalized to single spaces.
    """
    tokens = text.split()
    lowered: dict[str, str] = {}
    for key, value in glossary.items():
        lowered.setdefault(key.lower(), value)
    out: list[str] = []
    hits = 0
    for token in tokens:
        core = token.strip(_GLOSSARY_STRIP)
        if not core:
            out.append(token)
            continue
        start = token.index(core)
        hit = lowered.get(core.lower())
        if hit is None:
            out.append(token)
            continue
        hits += 1
        out.append(token[:start] + hit + token[start + len(core) :])
    return " ".join(out), hits, len(tokens)


def _align_words(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.align_words."""
    payload = AlignWordsInput.model_validate(context.input_data)
    transcript = payload.transcript
    aligned: list[TranscriptSegment] = []
    projected_segments = 0
    for seg in transcript.segments:
        if seg.words:
            clamped = tuple(
                w.model_copy(
                    update={
                        "start_us": min(max(w.start_us, seg.start_us), seg.end_us),
                        "end_us": min(max(w.end_us, seg.start_us), seg.end_us),
                    }
                )
                for w in seg.words
            )
            aligned.append(seg.model_copy(update={"words": clamped}))
        else:
            projected_segments += 1
            aligned.append(
                seg.model_copy(
                    update={"words": project_word_timings(seg.text, seg.start_us, seg.end_us)}
                )
            )
    new_ref = TranscriptRef(
        transcript_id=payload.output_transcript_id or f"{transcript.transcript_id}_aligned",
        source_asset_id=transcript.source_asset_id,
        language=transcript.language,
        segments=tuple(aligned),
        duration_us=transcript.duration_us,
        speaker_turns=transcript.speaker_turns,
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": new_ref.model_dump(mode="json"),
            "transcript_id": new_ref.transcript_id,
            "aligned_word_count": sum(len(s.words) for s in aligned),
            "projected_segment_count": projected_segments,
        },
    )


def _diarize(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.diarize.

    Merge first, stamp second: segments are grouped into turns by the gap
    heuristic, speakers are assigned per turn, and only then are the labels
    stamped onto segments *and* their words — so a merged turn can never
    carry stale per-segment labels.
    """
    payload = DiarizeInput.model_validate(context.input_data)
    transcript = payload.transcript
    turns = merge_segments_by_gap(transcript.segments, payload.gap_threshold_us)
    labels = assign_speakers(len(turns), payload.max_speakers)
    stamped: list[TranscriptSegment] = []
    speaker_turns: list[SpeakerTurn] = []
    for turn, label in zip(turns, labels, strict=True):
        for seg in turn:
            stamped.append(
                seg.model_copy(
                    update={
                        "speaker": label,
                        "words": tuple(w.model_copy(update={"speaker": label}) for w in seg.words),
                    }
                )
            )
        speaker_turns.append(
            SpeakerTurn(
                speaker=label,
                start_us=turn[0].start_us,
                end_us=turn[-1].end_us,
                text=" ".join(s.text.strip() for s in turn if s.text.strip()) or None,
            )
        )
    new_ref = TranscriptRef(
        transcript_id=payload.output_transcript_id or f"{transcript.transcript_id}_diarized",
        source_asset_id=transcript.source_asset_id,
        language=transcript.language,
        segments=tuple(stamped),
        duration_us=transcript.duration_us,
        speaker_turns=tuple(speaker_turns),
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": new_ref.model_dump(mode="json"),
            "transcript_id": new_ref.transcript_id,
            "turn_count": len(speaker_turns),
            "speaker_turns": [t.model_dump(mode="json") for t in speaker_turns],
        },
    )


def _translate_local(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.translate_local."""
    payload = TranslateLocalInput.model_validate(context.input_data)
    transcript = payload.transcript
    translated: list[TranscriptSegment] = []
    total_hits = 0
    total_tokens = 0
    for seg in transcript.segments:
        text, hits, total = apply_glossary(seg.text, payload.glossary)
        total_hits += hits
        total_tokens += total
        words: list[WordTiming] = []
        for word in seg.words:
            w_text, _, _ = apply_glossary(word.word, payload.glossary)
            words.append(word.model_copy(update={"word": w_text}))
        translated.append(seg.model_copy(update={"text": text, "words": tuple(words)}))
    new_ref = TranscriptRef(
        transcript_id=payload.output_transcript_id or f"{transcript.transcript_id}_t",
        source_asset_id=transcript.source_asset_id,
        language=payload.target_language,
        segments=tuple(translated),
        duration_us=transcript.duration_us,
        speaker_turns=transcript.speaker_turns,
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": new_ref.model_dump(mode="json"),
            "transcript_id": new_ref.transcript_id,
            "target_language": payload.target_language,
            "glossary_hits": total_hits,
            "glossary_total": total_tokens,
            "coverage": (total_hits / total_tokens) if total_tokens else 1.0,
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
        "align_words",
        OperationSpec(
            operation_id=OPERATION_ALIGN_WORDS,
            description="Project word timings onto segment spans (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=AlignWordsInput,
            handler=_align_words,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "diarize",
        OperationSpec(
            operation_id=OPERATION_DIARIZE,
            description="Gap-heuristic speaker turns, merge-then-stamp (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=DiarizeInput,
            handler=_diarize,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "translate_local",
        OperationSpec(
            operation_id=OPERATION_TRANSLATE_LOCAL,
            description="Offline glossary translation projection with coverage (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=TranslateLocalInput,
            handler=_translate_local,
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
            description="Generate Advanced SubStation Alpha (.ass) with "
            "Persian/RTL formatting (level B).",
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
    registry.register_operation(
        DOMAIN,
        "search_transcript",
        OperationSpec(
            operation_id=OPERATION_SEARCH_TRANSCRIPT,
            description="Search transcript segments for keywords and occurrences (level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=SearchTranscriptInput,
            handler=_search_transcript,
            required_packs=(CAPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "burn_in",
        OperationSpec(
            operation_id=OPERATION_BURN_IN,
            description="Burn in captions onto video asset with explicit confirmation (level C).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=BurnInInput,
            handler=_burn_in,
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
    "OPERATION_ALIGN_WORDS",
    "OPERATION_BURN_IN",
    "OPERATION_DIARIZE",
    "OPERATION_GENERATE_ASS",
    "OPERATION_GENERATE_SRT",
    "OPERATION_HIGHLIGHT_WORDS",
    "OPERATION_SEARCH_TRANSCRIPT",
    "OPERATION_STYLE_VAZIRMATN",
    "OPERATION_TRANSLATE_LOCAL",
    "OPERATION_TRANSCRIBE",
    "apply_glossary",
    "assign_speakers",
    "build_caption_registry",
    "merge_segments_by_gap",
    "project_word_timings",
    "register_caption_operations",
]
