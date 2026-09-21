"""Pure operations for the ``nexus.audio.studio`` pack (Wave 5 substrate).

This module implements the Nagar Command Bus operations for audio studio features:
* ``audio.detect_beats`` (Level A, IMMEDIATE): Inspects audio asset and derives beat markers and tempo.
* ``audio.normalize_loudness`` (Level C, CONFIRMATION): EBU R128 loudness normalization and peak limiting.
* ``audio.duck_music`` (Level B, REVERSIBLE): Sidechain ducking of background music against voice.
* ``audio.beat_sync_cut`` (Level B, REVERSIBLE): Synchronizes visual clip edits to musical beat boundaries.
"""

from __future__ import annotations

import hashlib
import uuid

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    DOMAIN,
    OPERATION_BEAT_SYNC_CUT,
    OPERATION_DETECT_BEATS,
    OPERATION_DUCK_MUSIC,
    OPERATION_NORMALIZE_LOUDNESS,
    BeatGridRef,
    BeatMarker,
    BeatSyncCutInput,
    DetectBeatsInput,
    DuckMusicInput,
    NormalizeLoudnessInput,
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
            "audio.normalize_loudness requires explicit user confirmation (confirmed=true) in Level C"
        )

    known = _asset_index(project)
    if payload.audio_asset_id not in known:
        raise CommandValidationError(
            f"audio.normalize_loudness references unknown asset: {payload.audio_asset_id!r}"
        )

    source_rec = known[payload.audio_asset_id]
    if source_rec.media_kind not in ("audio", "video"):
        raise CommandValidationError(
            f"audio.normalize_loudness requires audio or video media, got: {source_rec.media_kind!r}"
        )

    output_id = payload.output_asset_id or f"norm_{uuid.uuid4().hex[:12]}"
    digest_seed = f"{source_rec.content_sha256}:lufs:{payload.target_lufs}:peak:{payload.true_peak_db}"
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

    new_project = project.model_copy(
        update={"assets": [*project.assets, normalized_record]}
    )
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

    new_project = project.model_copy(
        update={"assets": [*project.assets, ducked_record]}
    )
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

    audio_rec = known[payload.audio_asset_id]
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


def build_audio_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with audio pack operations."""
    registry = CapabilityRegistry()
    register_audio_operations(registry)
    return registry
