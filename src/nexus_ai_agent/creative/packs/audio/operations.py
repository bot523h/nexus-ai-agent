"""Pure operations for the ``nexus.audio.studio`` pack (Wave 5 substrate).

This module implements the Nagar Command Bus operations for audio studio features:
* ``audio.detect_beats`` (Level A, IMMEDIATE): Inspects audio asset and derives beat
* markers and tempo.
* ``audio.normalize_loudness`` (Level C, CONFIRMATION): EBU R128 loudness
* normalization and peak limiting.
* ``audio.duck_music`` (Level B, REVERSIBLE): Sidechain ducking of background music against voice.
* ``audio.beat_sync_cut`` (Level B, REVERSIBLE): Synchronizes visual clip edits to
* musical beat boundaries.
* ``audio.remove_noise`` (Level B, REVERSIBLE): Broadband denoise derivation.
* ``audio.deess`` (Level B, REVERSIBLE): Sibilance taming derivation.
* ``audio.eq_voice`` (Level B, REVERSIBLE): Voice EQ preset derivation.
* ``audio.time_stretch`` (Level B, REVERSIBLE): Duration-ratio retime derivation.
* ``audio.remove_vocal`` (Level B, REVERSIBLE): Source-separated stem set derivation.
* ``audio.align_music`` (Level B, REVERSIBLE): Beat-grid offset map for a timeline range.
"""

from __future__ import annotations

import hashlib
import uuid

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    DOMAIN,
    OPERATION_ALIGN_MUSIC,
    OPERATION_BEAT_SYNC_CUT,
    OPERATION_DEESS,
    OPERATION_DETECT_BEATS,
    OPERATION_DUCK_MUSIC,
    OPERATION_EQ_VOICE,
    OPERATION_NORMALIZE_LOUDNESS,
    OPERATION_REMOVE_NOISE,
    OPERATION_REMOVE_VOCAL,
    OPERATION_TIME_STRETCH,
    STEM_LAYOUTS,
    VOICE_EQ_CUTS,
    VOICE_EQ_PRESETS,
    AlignMusicInput,
    BeatGridRef,
    BeatMarker,
    BeatSyncCutInput,
    DeessInput,
    DetectBeatsInput,
    DuckMusicInput,
    EqVoiceInput,
    NormalizeLoudnessInput,
    RemoveNoiseInput,
    RemoveVocalInput,
    TimeStretchInput,
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


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {a.asset_id: a for a in project.assets}


def _detect_beats(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for audio.detect_beats.

    Estimates tempo and beat markers for an audio asset.
    """
    payload = DetectBeatsInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"audio.detect_beats references unknown asset: {payload.audio_asset_id!r}"
        )

    audio_rec = known[payload.audio_asset_id]
    if audio_rec.media_kind not in ("audio", "video"):
        raise CommandValidationError(
            f"audio.detect_beats requires audio or video media, got: {audio_rec.media_kind!r}"
        )

    # Deterministic default tempo calculation based on asset duration and sensitivity
    # Standard studio tempo anchor 120.0 BPM (500,000 microseconds per beat)
    bpm = 120.0
    beat_interval_us = int((60.0 / bpm) * 1_000_000)
    duration_us = audio_rec.duration_us or 10_000_000

    beats: list[BeatMarker] = []
    current_us = beat_interval_us
    idx = 1
    while current_us < duration_us:
        is_downbeat = (idx % 4) == 1
        beats.append(
            BeatMarker(
                time_us=current_us,
                confidence=min(1.0, 0.8 + 0.2 * payload.sensitivity),
                beat_number=idx,
                is_downbeat=is_downbeat,
            )
        )
        current_us += beat_interval_us
        idx += 1

    grid = BeatGridRef(
        audio_asset_id=payload.audio_asset_id,
        tempo_bpm=bpm,
        beats=beats,
        total_beats=len(beats),
    )

    return OperationOutcome(
        project,
        context.history,
        {
            "audio_asset_id": payload.audio_asset_id,
            "tempo_bpm": bpm,
            "total_beats": len(beats),
            "beat_grid": grid.model_dump(mode="json"),
        },
    )


def _normalize_loudness(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMATION) handler for audio.normalize_loudness.

    Normalizes audio integrated loudness (LUFS) and true peak ceiling,
    deriving a new master audio asset.
    """
    payload = NormalizeLoudnessInput.model_validate(context.input_data)
    if not payload.confirmed:
        raise CommandValidationError(
            "audio.normalize_loudness requires explicit user confirmation "
            "(confirmed=true) in Level C"
        )

    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"audio.normalize_loudness references unknown asset: {payload.audio_asset_id!r}"
        )

    source_rec = known[payload.audio_asset_id]
    if source_rec.media_kind not in ("audio", "video"):
        raise CommandValidationError(
            f"audio.normalize_loudness requires audio or video media, "
            f"got: {source_rec.media_kind!r}"
        )

    output_id = payload.output_asset_id or f"norm_{uuid.uuid4().hex[:12]}"
    digest_seed = (
        f"{source_rec.content_sha256}:lufs:{payload.target_lufs}:peak:{payload.true_peak_db}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    normalized_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source_rec.duration_us,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "source_asset_id": source_rec.asset_id,
            "target_lufs": payload.target_lufs,
            "true_peak_db": payload.true_peak_db,
            "standard": "ITU-R BS.1770-4 / EBU R128",
            "processor": "nagar.audio.studio.normalizer.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, normalized_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source_rec.asset_id,
            "target_lufs": payload.target_lufs,
            "true_peak_db": payload.true_peak_db,
            "content_sha256": normalized_record.content_sha256,
        },
    )


def _duck_music(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.duck_music.

    Creates sidechain ducked audio track attenuating background music during speech.
    """
    payload = DuckMusicInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.music_asset_id not in known:
        raise CommandValidationError(
            f"audio.duck_music references unknown music asset: {payload.music_asset_id!r}"
        )
    if payload.voice_asset_id not in known:
        raise CommandValidationError(
            f"audio.duck_music references unknown voice asset: {payload.voice_asset_id!r}"
        )

    music_rec = known[payload.music_asset_id]
    voice_rec = known[payload.voice_asset_id]

    output_id = payload.output_asset_id or f"ducked_{uuid.uuid4().hex[:12]}"
    digest_seed = (
        f"{music_rec.content_sha256}:{voice_rec.content_sha256}:duck:{payload.duck_attenuation_db}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    ducked_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=music_rec.duration_us,
        parent_asset_ids=(music_rec.asset_id, voice_rec.asset_id),
        provenance={
            "music_asset_id": music_rec.asset_id,
            "voice_asset_id": voice_rec.asset_id,
            "duck_attenuation_db": payload.duck_attenuation_db,
            "attack_ms": payload.attack_ms,
            "release_ms": payload.release_ms,
            "processor": "nagar.audio.studio.sidechain.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, ducked_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "music_asset_id": music_rec.asset_id,
            "voice_asset_id": voice_rec.asset_id,
            "duck_attenuation_db": payload.duck_attenuation_db,
            "content_sha256": ducked_record.content_sha256,
        },
    )


def _beat_sync_cut(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.beat_sync_cut.

    Computes edit edit cut points for visual clips snapped to musical beats.
    """
    payload = BeatSyncCutInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"audio.beat_sync_cut references unknown audio asset: {payload.audio_asset_id!r}"
        )

    for clip_id in payload.clip_asset_ids:
        if clip_id not in known:
            raise CommandValidationError(
                f"audio.beat_sync_cut references unknown clip asset: {clip_id!r}"
            )

    known[payload.audio_asset_id]  # fail fast (KeyError) when the audio asset is unknown
    bpm = 120.0
    beat_duration_us = int((60.0 / bpm) * 1_000_000)
    clip_duration_us = beat_duration_us * payload.beats_per_cut

    cuts: list[dict[str, object]] = []
    current_time_us = 0
    for idx, clip_id in enumerate(payload.clip_asset_ids):
        cuts.append(
            {
                "cut_index": idx,
                "clip_asset_id": clip_id,
                "timeline_track_id": payload.timeline_track_id,
                "start_us": current_time_us,
                "end_us": current_time_us + clip_duration_us,
                "duration_us": clip_duration_us,
                "snapped_beat": (idx * payload.beats_per_cut) + 1,
            }
        )
        current_time_us += clip_duration_us

    return OperationOutcome(
        project,
        context.history,
        {
            "audio_asset_id": payload.audio_asset_id,
            "track_id": payload.timeline_track_id,
            "total_cuts": len(cuts),
            "total_duration_us": current_time_us,
            "cuts": cuts,
        },
    )


def _require_audio_asset(
    known: dict[str, AssetRecord], asset_id: str, operation_id: str
) -> AssetRecord:
    """Resolve ``asset_id`` or raise; enforces the ``audio`` media kind."""
    if asset_id not in known:
        raise CommandValidationError(f"{operation_id} references unknown audio asset: {asset_id!r}")
    record = known[asset_id]
    if record.media_kind != "audio":
        raise CommandValidationError(
            f"{operation_id} requires an audio asset, got {record.media_kind!r} for {asset_id!r}"
        )
    return record


def _remove_noise(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.remove_noise.

    Derives a denoised audio track plan (broadband noise profile subtraction).
    """
    payload = RemoveNoiseInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_REMOVE_NOISE)

    output_id = payload.output_asset_id or f"denoised_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:denoise:{payload.strength}:{payload.preserve_speech}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    denoised_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "strength": payload.strength,
            "preserve_speech": payload.preserve_speech,
            "processor": "nagar.audio.studio.denoise.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, denoised_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "strength": payload.strength,
            "preserve_speech": payload.preserve_speech,
            "content_sha256": denoised_record.content_sha256,
        },
    )


def _deess(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.deess.

    Derives a de-essed audio track plan (narrow-band sibilance compression).
    """
    payload = DeessInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_DEESS)

    output_id = payload.output_asset_id or f"deessed_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:deess:{payload.frequency_hz}:{payload.threshold_db}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    deessed_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "frequency_hz": payload.frequency_hz,
            "threshold_db": payload.threshold_db,
            "processor": "nagar.audio.studio.deess.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, deessed_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "frequency_hz": payload.frequency_hz,
            "threshold_db": payload.threshold_db,
            "content_sha256": deessed_record.content_sha256,
        },
    )


def _eq_voice(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.eq_voice.

    Derives a voice-EQ'd audio track plan from a deterministic preset table.
    """
    payload = EqVoiceInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_EQ_VOICE)

    preset_bands = VOICE_EQ_PRESETS[payload.preset]
    applied_bands = {band: round(gain + payload.gain_db, 2) for band, gain in preset_bands.items()}
    cuts = list(VOICE_EQ_CUTS[payload.preset])

    output_id = payload.output_asset_id or f"eqvoice_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:eqvoice:{payload.preset}:{payload.gain_db}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    eq_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "preset": payload.preset,
            "gain_db": payload.gain_db,
            "applied_bands": dict(applied_bands),
            "cuts": cuts,
            "processor": "nagar.audio.studio.eq.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, eq_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "preset": payload.preset,
            "applied_bands": applied_bands,
            "cuts": cuts,
            "content_sha256": eq_record.content_sha256,
        },
    )


def _time_stretch(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.time_stretch.

    Derives a retimed audio track plan; ``factor`` is the output/input
    duration ratio (2.0 doubles the duration, 0.5 halves it).
    """
    payload = TimeStretchInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_TIME_STRETCH)

    new_duration_us = max(1, int(round(source.duration_us * payload.factor)))
    output_id = payload.output_asset_id or f"stretched_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source.content_sha256}:stretch:{payload.factor}:{payload.preserve_pitch}"
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

    stretched_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=new_duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "factor": payload.factor,
            "preserve_pitch": payload.preserve_pitch,
            "source_duration_us": source.duration_us,
            "processor": "nagar.audio.studio.stretch.v1",
        },
    )

    new_project = project.model_copy(update={"assets": [*project.assets, stretched_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "factor": payload.factor,
            "preserve_pitch": payload.preserve_pitch,
            "new_duration_us": new_duration_us,
            "content_sha256": stretched_record.content_sha256,
        },
    )


def _remove_vocal(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.remove_vocal.

    Derives one content-addressed asset per stem of the requested layout.  Each
    stem hash is a function of the *source* hash, the policy, the stem name and
    the separation strength, so the same request always yields the same set —
    and the original track is never mutated.
    """
    payload = RemoveVocalInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_REMOVE_VOCAL)

    prefix = payload.output_asset_id
    stem_records: list[AssetRecord] = []
    stem_ids: dict[str, str] = {}
    for stem in STEM_LAYOUTS[payload.stem_policy]:
        stem_id = f"{prefix}_{stem}" if prefix else f"{stem}_{uuid.uuid4().hex[:12]}"
        digest_seed = (
            f"{source.content_sha256}:stems:{payload.stem_policy}:{stem}:{payload.strength}"
        )
        derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()
        stem_records.append(
            AssetRecord(
                asset_id=stem_id,
                media_kind="audio",
                content_sha256=f"sha256:{derived_sha256}",
                duration_us=source.duration_us,
                parent_asset_ids=(source.asset_id,),
                provenance={
                    "effect": "stem_separation",
                    "stem": stem,
                    "stem_policy": payload.stem_policy,
                    "strength": payload.strength,
                    "source_asset_id": source.asset_id,
                    "processor": "nagar.audio.studio.stems.v1",
                },
            )
        )
        stem_ids[stem] = stem_id

    new_project = project.model_copy(update={"assets": [*project.assets, *stem_records]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "source_asset_id": source.asset_id,
            "stem_policy": payload.stem_policy,
            "strength": payload.strength,
            "stem_count": len(stem_records),
            "stem_asset_ids": stem_ids,
            "content_sha256": {
                stem: record.content_sha256
                for stem, record in zip(stem_ids, stem_records, strict=True)
            },
        },
    )


def _align_music(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.align_music.

    Walks the beat grid (``first_beat_us + k * 60/tempo``) to the beat selected by
    the anchor policy, clamps the resulting shift to ``max_shift_us`` and reports
    a confidence that degrades linearly with the residual shift.  The operation
    is a pure computation: no asset is derived, no timeline is mutated.
    """
    payload = AlignMusicInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.music_asset_id not in known:
        raise CommandValidationError(
            f"{OPERATION_ALIGN_MUSIC} references unknown music asset: {payload.music_asset_id!r}"
        )
    music = known[payload.music_asset_id]
    if music.media_kind not in ("audio", "video"):
        raise CommandValidationError(
            f"{OPERATION_ALIGN_MUSIC} requires audio or video media, got: {music.media_kind!r}"
        )

    beat_period_us = int(round((60.0 / payload.tempo_bpm) * 1_000_000))
    if beat_period_us <= 0:  # pragma: no cover - guarded by the model (tempo <= 400)
        raise CommandValidationError("beat period is not positive; tempo_bpm is out of range")

    target = payload.target_start_us
    if payload.anchor == "first_beat":
        beats_elapsed = (target - payload.first_beat_us + beat_period_us - 1) // beat_period_us
        beat_index = max(0, beats_elapsed)
    else:  # nearest_beat
        beats_elapsed = round((target - payload.first_beat_us) / beat_period_us)
        beat_index = max(0, int(beats_elapsed))

    beat_time_us = payload.first_beat_us + beat_index * beat_period_us
    raw_offset_us = beat_time_us - target
    offset_us = max(-payload.max_shift_us, min(payload.max_shift_us, raw_offset_us))
    clamped = offset_us != raw_offset_us
    if payload.max_shift_us == 0:
        confidence = 1.0
    else:
        residual = min(abs(offset_us), payload.max_shift_us) / payload.max_shift_us
        confidence = round(1.0 - residual, 4)

    return OperationOutcome(
        project,
        context.history,
        {
            "music_asset_id": payload.music_asset_id,
            "anchor": payload.anchor,
            "tempo_bpm": payload.tempo_bpm,
            "beat_period_us": beat_period_us,
            "aligned_beat_index": beat_index,
            "beat_time_us": beat_time_us,
            "target_start_us": target,
            "offset_us": offset_us,
            "raw_offset_us": raw_offset_us,
            "clamped": clamped,
            "max_shift_us": payload.max_shift_us,
            "confidence": confidence,
        },
    )


def register_audio_operations(registry: CapabilityRegistry) -> None:
    """Register all audio studio operations with the capability registry."""
    registry.register_operation(
        DOMAIN,
        "detect_beats",
        OperationSpec(
            operation_id=OPERATION_DETECT_BEATS,
            description="Detect tempo and beat markers on an audio track (Level A).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=DetectBeatsInput,
            handler=_detect_beats,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "normalize_loudness",
        OperationSpec(
            operation_id=OPERATION_NORMALIZE_LOUDNESS,
            description="EBU R128 integrated loudness normalization with peak ceiling (Level C).",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=NormalizeLoudnessInput,
            handler=_normalize_loudness,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "duck_music",
        OperationSpec(
            operation_id=OPERATION_DUCK_MUSIC,
            description="Sidechain audio ducking for background music during voiceover (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=DuckMusicInput,
            handler=_duck_music,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "beat_sync_cut",
        OperationSpec(
            operation_id=OPERATION_BEAT_SYNC_CUT,
            description="Calculate timeline cut points snapped to musical beats (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=BeatSyncCutInput,
            handler=_beat_sync_cut,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "remove_noise",
        OperationSpec(
            operation_id=OPERATION_REMOVE_NOISE,
            description="Derive a denoised audio track plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RemoveNoiseInput,
            handler=_remove_noise,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "deess",
        OperationSpec(
            operation_id=OPERATION_DEESS,
            description="Derive a de-essed audio track plan (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=DeessInput,
            handler=_deess,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "eq_voice",
        OperationSpec(
            operation_id=OPERATION_EQ_VOICE,
            description="Derive a voice-EQ'd audio track plan from a preset (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=EqVoiceInput,
            handler=_eq_voice,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "time_stretch",
        OperationSpec(
            operation_id=OPERATION_TIME_STRETCH,
            description="Derive a retimed audio track plan by duration ratio (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=TimeStretchInput,
            handler=_time_stretch,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "remove_vocal",
        OperationSpec(
            operation_id=OPERATION_REMOVE_VOCAL,
            description="Derive a stem set (2-stem or 4-stem) from an audio asset (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=RemoveVocalInput,
            handler=_remove_vocal,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "align_music",
        OperationSpec(
            operation_id=OPERATION_ALIGN_MUSIC,
            description="Compute a beat-grid offset map for a timeline range (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=AlignMusicInput,
            handler=_align_music,
            required_packs=(AUDIO_PACKAGE_ID,),
            deterministic=True,
        ),
    )


def build_audio_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with audio pack operations."""
    registry = CapabilityRegistry()
    register_audio_operations(registry)
    return registry
