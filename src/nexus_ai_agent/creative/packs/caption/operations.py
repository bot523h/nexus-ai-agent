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
import json
import re

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


def _require_fresh_asset_id(project: Project, asset_id: str) -> None:
    if asset_id in _asset_index(project):
        raise CommandValidationError(f"caption output asset id already exists: {asset_id!r}")


def _transcript_sha256(transcript: TranscriptRef) -> str:
    """Content address the complete typed transcript, not its caller-chosen ID."""
    canonical = json.dumps(
        transcript.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_transcript_source(
    project: Project,
    transcript: TranscriptRef,
    *,
    expected_asset_id: str | None = None,
    require_digest_for_engine: bool = False,
) -> None:
    """Bind typed transcript provenance to an authorized project media record."""
    source_id = transcript.source_asset_id or expected_asset_id
    if expected_asset_id is not None and source_id != expected_asset_id:
        raise CommandValidationError("transcript source_asset_id does not match audio_asset_id")
    if not source_id:
        if require_digest_for_engine and transcript.engine and not transcript.source_sha256:
            raise CommandValidationError("engine transcript is missing source SHA-256 provenance")
        return
    source = _asset_index(project).get(source_id)
    if source is None:
        raise CommandValidationError(f"transcript references unknown source asset: {source_id!r}")
    if source.media_kind not in {"audio", "video"}:
        raise CommandValidationError("transcript source must be an audio or video asset")
    if transcript.source_sha256 is None:
        if require_digest_for_engine and transcript.engine:
            raise CommandValidationError("engine transcript is missing source SHA-256 provenance")
        return
    digest = source.content_sha256.removeprefix("sha256:").lower()
    if digest != transcript.source_sha256:
        raise CommandValidationError("transcript source SHA-256 does not match project media asset")


def _transcribe(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (IMMEDIATE) handler for caption.transcribe."""
    payload = TranscribeInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"caption.transcribe references unknown audio asset: {payload.audio_asset_id!r}"
        )
    if known[payload.audio_asset_id].media_kind not in {"audio", "video"}:
        raise CommandValidationError("caption.transcribe requires an audio or video source asset")

    if payload.transcript is None:
        raise CommandValidationError(
            "caption.transcribe requires executed CaptionEnginePort evidence; "
            "no transcript was supplied and no local engine ran in the pure pack"
        )
    transcript = payload.transcript
    if not transcript.engine or not transcript.source_sha256:
        raise CommandValidationError(
            "caption.transcribe requires engine identity and source SHA-256 evidence"
        )
    _validate_transcript_source(
        project,
        transcript,
        expected_asset_id=payload.audio_asset_id,
        require_digest_for_engine=True,
    )
    if not transcript.source_asset_id:
        transcript = transcript.model_copy(update={"source_asset_id": payload.audio_asset_id})

    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": transcript.model_dump(mode="json"),
            "transcript_id": transcript.transcript_id,
            "audio_asset_id": payload.audio_asset_id,
            "language": transcript.language,
            "segment_count": len(transcript.segments),
            "engine": transcript.engine,
            "engine_version": transcript.engine_version,
            "model_name": transcript.model_name,
            "model_digest": transcript.model_digest,
            "source_sha256": transcript.source_sha256,
        },
    )


def _generate_srt(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for caption.generate_srt.

    Produces a CaptionAsset containing the pure SRT string as primary content
    and WebVTT as a companion rendition on the same asset.
    """
    payload = GenerateSrtInput.model_validate(context.input_data)
    transcript = payload.transcript

    if transcript.source_asset_id:
        _validate_transcript_source(project, transcript)

    srt_text = format_srt(transcript)
    srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()

    companion_renditions: dict[str, str] = {}
    companion_hashes: dict[str, str] = {}

    if payload.include_vtt:
        vtt_text = format_vtt(transcript)
        vtt_sha256 = hashlib.sha256(vtt_text.encode("utf-8")).hexdigest()
        companion_renditions["vtt"] = vtt_text
        companion_hashes["vtt"] = vtt_sha256

    asset_id = payload.output_asset_id or f"caption_{srt_sha256[:16]}"
    _require_fresh_asset_id(project, asset_id)

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
            "source_transcript_sha256": _transcript_sha256(transcript),
            "language": transcript.language,
            "engine": transcript.engine,
            "engine_version": transcript.engine_version,
            "model_name": transcript.model_name,
            "model_digest": transcript.model_digest,
            "source_audio_sha256": transcript.source_sha256,
            "parameters": transcript.parameters,
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

    if transcript.source_asset_id:
        _validate_transcript_source(project, transcript)

    ass_text = format_ass(
        transcript,
        style=payload.style,
        enable_karaoke=payload.enable_karaoke,
        enable_rtl_wrap=payload.enable_rtl_wrap,
        play_res_x=payload.play_res_x,
        play_res_y=payload.play_res_y,
    )
    ass_sha256 = hashlib.sha256(ass_text.encode("utf-8")).hexdigest()
    font_path = None
    font_sha256 = None
    if (payload.style.font_name if payload.style else "Vazirmatn") == "Vazirmatn":
        from nexus_ai_agent.creative.packs.caption.font_asset import vazirmatn_asset

        font_path, font_sha256 = vazirmatn_asset()

    asset_id = payload.output_asset_id or f"caption_ass_{ass_sha256[:16]}"
    _require_fresh_asset_id(project, asset_id)

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
            "source_transcript_sha256": _transcript_sha256(transcript),
            "language": transcript.language,
            "engine": transcript.engine,
            "engine_version": transcript.engine_version,
            "model_name": transcript.model_name,
            "model_digest": transcript.model_digest,
            "source_audio_sha256": transcript.source_sha256,
            "parameters": transcript.parameters,
            "font": payload.style.font_name if payload.style else "Vazirmatn",
            "font_asset_path": "fonts/Vazirmatn.ttf" if font_path else None,
            "font_sha256": font_sha256,
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
    """Create a real ASS rendition from transcript evidence bound to its parent asset."""
    payload = StyleVazirmatnInput.model_validate(context.input_data)
    known = _asset_index(project)
    target_asset = known.get(payload.caption_asset_id)
    if target_asset is None:
        raise CommandValidationError(
            f"caption.style_vazirmatn references unknown asset: {payload.caption_asset_id!r}"
        )
    if target_asset.media_kind != "caption":
        raise CommandValidationError(
            f"caption.style_vazirmatn requires a caption asset, got: {target_asset.media_kind!r}"
        )

    transcript = payload.transcript
    provenance = target_asset.provenance
    if transcript.transcript_id != provenance.get("source_transcript_id"):
        raise CommandValidationError("caption style transcript does not match parent provenance")
    expected_digest = provenance.get("source_transcript_sha256")
    if not isinstance(expected_digest, str) or _transcript_sha256(transcript) != expected_digest:
        raise CommandValidationError("caption style transcript digest does not match parent asset")
    if transcript.source_sha256 != provenance.get("source_audio_sha256"):
        raise CommandValidationError(
            "caption style source audio digest does not match parent asset"
        )
    if (
        transcript.source_asset_id
        and transcript.source_asset_id not in target_asset.parent_asset_ids
    ):
        raise CommandValidationError("caption style source asset does not match parent lineage")

    style_cfg = AssStyleConfig(
        name="Vazirmatn",
        font_name="Vazirmatn",
        font_size=payload.font_size,
        primary_colour=payload.primary_colour,
        outline_colour=payload.outline_colour,
        back_colour=payload.shadow_colour,
        alignment=payload.alignment,
        bold=payload.bold,
    )
    ass_text = format_ass(
        transcript,
        style=style_cfg,
        enable_rtl_wrap=True,
        play_res_x=payload.play_res_x,
        play_res_y=payload.play_res_y,
    )
    content_sha256 = hashlib.sha256(ass_text.encode("utf-8")).hexdigest()
    from nexus_ai_agent.creative.packs.caption.font_asset import vazirmatn_asset

    font_path, font_sha256 = vazirmatn_asset()
    asset_id = payload.output_asset_id or f"{payload.caption_asset_id}_vazir_{content_sha256[:12]}"
    _require_fresh_asset_id(project, asset_id)
    caption_asset = CaptionAsset(
        asset_id=asset_id,
        source_transcript_id=transcript.transcript_id,
        content=ass_text,
        content_sha256=content_sha256,
        format="ass",
        language=transcript.language,
        line_count=len(transcript.segments),
        duration_us=transcript.duration_us,
    )
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="caption",
        content_sha256=content_sha256,
        duration_us=transcript.duration_us,
        parent_asset_ids=(target_asset.asset_id,),
        provenance={
            "format": "ass",
            "source_transcript_id": transcript.transcript_id,
            "source_transcript_sha256": expected_digest,
            "source_audio_sha256": transcript.source_sha256,
            "engine": transcript.engine,
            "engine_version": transcript.engine_version,
            "model_name": transcript.model_name,
            "model_digest": transcript.model_digest,
            "parameters": transcript.parameters,
            "parent_caption_sha256": target_asset.content_sha256,
            "font": "Vazirmatn",
            "font_asset_path": str(font_path.name),
            "font_sha256": font_sha256,
            "style_config": style_cfg.model_dump(mode="json"),
            "styled_by": OPERATION_STYLE_VAZIRMATN,
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "styled_asset_id": asset_id,
            "parent_asset_id": target_asset.asset_id,
            "caption_asset": caption_asset.model_dump(mode="json"),
            "content_sha256": content_sha256,
            "font_name": "Vazirmatn",
            "font_sha256": font_sha256,
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
    if video_record.media_kind != "video" or caption_record.media_kind != "caption":
        raise CommandValidationError("caption.burn_in requires a video and a caption asset")

    # This pure pack handler cannot claim a rendered output exists. The trusted
    # render lane must execute FFmpeg and publish measured bytes before a derived
    # AssetRecord may be registered; hashing a pair of parent hashes is not an
    # artifact hash and would be fabricated success.
    raise CommandValidationError(
        "caption_burn_in_renderer_unavailable: no trusted subtitle render result was supplied; "
        "no derived artifact was created"
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
    if start_us < 0 or end_us < start_us:
        raise ValueError("word projection requires 0 <= start_us <= end_us")
    weights = [max(1, len(w)) for w in words]
    return _project_weighted(words, weights, start_us, end_us)


def _project_weighted(
    words: list[str], weights: list[int], start_us: int, end_us: int
) -> tuple[WordTiming, ...]:
    """Largest-remainder partition: monotone intervals exactly cover the span."""
    if not words:
        return ()
    span = end_us - start_us
    total = sum(max(1, weight) for weight in weights)
    floors = [(span * max(1, weight)) // total for weight in weights]
    remainders = [(span * max(1, weight)) % total for weight in weights]
    leftover = span - sum(floors)
    order = sorted(range(len(words)), key=lambda i: (-remainders[i], i))
    for index in order[:leftover]:
        floors[index] += 1
    out: list[WordTiming] = []
    cursor = start_us
    for word, duration in zip(words, floors, strict=True):
        out.append(WordTiming(word=word, start_us=cursor, end_us=cursor + duration))
        cursor += duration
    assert cursor == end_us
    return tuple(out)


def _normalize_word_cover(segment: TranscriptSegment) -> tuple[WordTiming, ...]:
    """Clamp supplied words, retain duration proportions, then force exact cover."""
    if not segment.words:
        return project_word_timings(segment.text, segment.start_us, segment.end_us)
    texts = [word.word for word in segment.words]
    weights = [
        max(0, min(word.end_us, segment.end_us) - max(word.start_us, segment.start_us))
        for word in segment.words
    ]
    if not any(weights):
        weights = [max(1, len(text)) for text in texts]
    normalized = _project_weighted(texts, weights, segment.start_us, segment.end_us)
    return tuple(
        projected.model_copy(update={"score": original.score, "speaker": original.speaker})
        for projected, original in zip(normalized, segment.words, strict=True)
    )


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
        if not seg.words:
            projected_segments += 1
        aligned.append(seg.model_copy(update={"words": _normalize_word_cover(seg)}))
    new_ref = TranscriptRef(
        transcript_id=payload.output_transcript_id or f"{transcript.transcript_id}_aligned",
        source_asset_id=transcript.source_asset_id,
        language=transcript.language,
        segments=tuple(aligned),
        duration_us=transcript.duration_us,
        speaker_turns=transcript.speaker_turns,
        engine=transcript.engine,
        engine_version=transcript.engine_version,
        model_name=transcript.model_name,
        model_digest=transcript.model_digest,
        source_sha256=transcript.source_sha256,
        parameters=transcript.parameters,
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
        engine=transcript.engine,
        engine_version=transcript.engine_version,
        model_name=transcript.model_name,
        model_digest=transcript.model_digest,
        source_sha256=transcript.source_sha256,
        parameters={
            **transcript.parameters,
            "diarization_backend": "deterministic-gap-grouping-heuristic",
            "speaker_identity_claimed": False,
        },
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": new_ref.model_dump(mode="json"),
            "transcript_id": new_ref.transcript_id,
            "diarization_backend": "deterministic-gap-grouping-heuristic",
            "speaker_identity_claimed": False,
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
        transcript_id=payload.output_transcript_id
        or f"{transcript.transcript_id}_t-{payload.target_language.lower()}",
        source_asset_id=transcript.source_asset_id,
        language=payload.target_language,
        segments=tuple(translated),
        duration_us=transcript.duration_us,
        speaker_turns=transcript.speaker_turns,
        engine="glossary-substitution",
        engine_version=None,
        model_name=None,
        model_digest=None,
        parameters={
            **transcript.parameters,
            "operation": OPERATION_TRANSLATE_LOCAL,
            "translation_backend": "deterministic_glossary_only",
        },
    )
    return OperationOutcome(
        project,
        context.history,
        {
            "transcript": new_ref.model_dump(mode="json"),
            "transcript_id": new_ref.transcript_id,
            "translation_backend": "deterministic_glossary_only",
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
            deterministic=False,
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
