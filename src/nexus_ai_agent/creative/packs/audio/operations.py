"""Operations for the ``nexus.audio.studio`` pack — pure contract + real execution.

OMEGA-02 turned this pack from "plans that look like work" into operations that
either **measure/produce real media** or **refuse with a typed reason**.  The
substrate rules did not change: this package stays stdlib + pydantic +
``creative.studio`` (``tests/architecture/test_audio_pack_boundary.py`` enforces
it with an AST walk, no I/O allowed here).  What changed is where the numbers
come from:

* ``audio.detect_beats`` measures the decoded signal through the injected
  :class:`~nexus_ai_agent.creative.studio.audio_engine.AudioEnginePort`.  No
  tempo exists without media, so without an engine or bytes the answer is
  ``UNAVAILABLE`` / ``REQUIRES_MEDIA_BYTES`` — never a default BPM.
* ``audio.normalize_loudness`` runs real two-pass FFmpeg ``loudnorm`` and a
  fresh ``ebur128`` verification of the produced file.  The ``AssetRecord`` it
  derives carries the *measured* ``sha256`` of the published file, the measured
  loudness, and the signed passport id — not a hash derived from a seed string.
* ``audio.remove_noise`` runs real ``highpass`` + ``afftdn`` filtering and
  extracts the acoustic signature (RMS, crest, dynamic range, spectral centroid,
  band distribution) from the decoded output.
* ``audio.remove_vocal`` refuses honestly: no local separation model ships in
  this repository, so it returns ``UNAVAILABLE`` / ``REQUIRES_LOCAL_MODEL`` and
  derives nothing.  A fabricated stem set would be worse than an honest gap.
* ``audio.beat_sync_cut`` uses the grid the analyser last measured for that
  asset.  With no measurement it falls back to the **declared** 120 BPM anchor
  and says so (``tempo_source="default_120"``) instead of pretending to know.
* ``audio.duck_music``, ``audio.deess``, ``audio.eq_voice``, ``audio.time_stretch``
  are still state derivations (no media bytes are produced) and say so in their
  evidence (``derivation="state_only"``).  Their hashes are **requests**, not
  artifacts; the key is named ``request_sha256`` so no consumer can mistake it
  for measured content.

Every handler stays a pure function of ``(project, context)``: it never mutates
its input, never opens a file, and reaches the outside world only through the
port the composition root injected.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    CANONICAL_SAMPLE_RATE,
    CANONICAL_STREAM_PROFILE_ID,
    CODE_REQUIRES_AUDIO_ENGINE,
    CODE_REQUIRES_LOCAL_MODEL,
    CODE_REQUIRES_MEDIA_BYTES,
    DOMAIN,
    EXECUTION_STATUS_READY,
    EXECUTION_STATUS_UNAVAILABLE,
    OPERATION_ALIGN_MUSIC,
    OPERATION_BARK_MASTER,
    OPERATION_BEAT_SYNC_CUT,
    OPERATION_DEESS,
    OPERATION_DETECT_BEATS,
    OPERATION_DUCK_MUSIC,
    OPERATION_EQ_VOICE,
    OPERATION_NORMALIZE_LOUDNESS,
    OPERATION_REMOVE_NOISE,
    OPERATION_REMOVE_VOCAL,
    OPERATION_RESTORE_VOICE,
    OPERATION_SPATIALIZE,
    OPERATION_SYNC_FOLEY,
    OPERATION_TIME_STRETCH,
    STAGE_B_INPUT_MODELS,
    VOICE_EQ_CUTS,
    VOICE_EQ_PRESETS,
    AlignMusicInput,
    BeatGridRef,
    BeatMarker,
    BeatSyncCutInput,
    DeessInput,
    DetectBeatsInput,
    DetectedBeatGrid,
    DuckMusicInput,
    EqVoiceInput,
    NormalizeLoudnessInput,
    RemoveNoiseInput,
    RemoveVocalInput,
    StageBAudioRequest,
    TimeStretchInput,
    kernel_source_ids,
    last_beat_analysis,
    record_beat_analysis,
)
from nexus_ai_agent.creative.studio.audio_engine import (
    BeatRequest,
    DenoiseRequest,
    LoudnessRequest,
    SourceRequest,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandExecutionError,
    CommandValidationError,
    PermissionLevel,
    Project,
)


def _asset_index(project: Project) -> dict[str, AssetRecord]:
    return {a.asset_id: a for a in project.assets}


def _new_asset_id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex[:12]}"


def _request_fingerprint(seed: str) -> str:
    """``sha256:<hex>`` of an explicit request — *not* of produced media.

    Named and used as a request identity so no consumer can mistake it for the
    hash of an artifact (which only an engine can measure)."""
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _state_only_evidence(
    *,
    processor: str,
    request_sha256: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "execution_status": EXECUTION_STATUS_READY,
        "derivation": "state_only",
        "artifact_verified": False,
        "processor": processor,
        "request_sha256": request_sha256,
        "parameters": parameters,
        "note": (
            "state derivation only: no media bytes were produced, so this asset "
            "records the request identity and must not be presented as a "
            "rendered artifact"
        ),
    }


def _require_number(value: float | None, field: str) -> float:
    """Return a validated loudness number, or fail loudly.

    ``NormalizeLoudnessInput`` guarantees these are present (a locked profile
    supplies them, a custom profile requires them).  This keeps that invariant
    explicit instead of silencing it with a cast.
    """
    if value is None:  # pragma: no cover - guarded by the input validator
        raise CommandValidationError(f"audio.normalize_loudness requires {field}")
    return float(value)


def _engine(context: OperationContext, operation_id: str) -> Any:
    """Return the injected engine port or refuse the operation honestly."""
    engine = getattr(context, "audio_engine", None)
    if engine is None:
        raise CommandValidationError(
            f"{operation_id} requires the native audio engine; none was composed "
            f"into this command bus (code {CODE_REQUIRES_AUDIO_ENGINE}). "
            "Audio rendering is not simulated: compose NativeAudioEngine (or an "
            "in-memory double in tests) to execute this operation."
        )
    return engine


def _beat_grid_from_analysis(
    asset_id: str, evidence: Any
) -> tuple[DetectedBeatGrid | None, BeatGridRef | None]:
    """Convert engine beat evidence into the pack's records (honest or nothing).

    ``confidence`` is copied verbatim from the measurement: a caller's
    ``sensitivity`` chooses *how much evidence is required*, it never inflates
    the reported confidence of a detection (that would be a displayed number).
    """
    if getattr(evidence, "status", None) != "detected" or evidence.tempo_bpm is None:
        return None, None
    period_us = int(evidence.beat_period_us or round(60_000_000 / evidence.tempo_bpm))
    if period_us <= 0:
        return None, None
    confidence = float(evidence.confidence)
    grid = DetectedBeatGrid(
        audio_asset_id=asset_id,
        tempo_bpm=float(evidence.tempo_bpm),
        beat_period_us=period_us,
        first_beat_us=int(evidence.first_beat_us),
        confidence=round(confidence, 4),
        onset_count=int(evidence.onset_count),
        min_bpm=float(evidence.min_bpm),
        max_bpm=float(evidence.max_bpm),
        analyzer=str(evidence.analyzer),
    )
    total_beats = max(0, int(evidence.onset_count))
    markers: list[BeatMarker] = []
    for index in range(1, total_beats + 1):
        time_us = grid.first_beat_us + (index - 1) * period_us
        markers.append(
            BeatMarker(
                time_us=max(0, time_us),
                confidence=round(min(1.0, max(0.0, confidence)), 4),
                beat_number=index,
                is_downbeat=(index - 1) % 4 == 0,
            )
        )
    ref = BeatGridRef(
        audio_asset_id=asset_id,
        tempo_bpm=grid.tempo_bpm,
        beats=markers,
        total_beats=len(markers),
    )
    return grid, ref


def _detect_beats(project: Project, context: OperationContext) -> OperationOutcome:
    """Level A (READ) handler for ``audio.detect_beats`` — real measurement.

    The tempo comes from the decoded waveform (onset envelope + dominant
    inter-onset interval) through the engine port.  There is no default BPM: a
    track with no decodable media, no onsets, or an irregular pulse yields an
    honest ``UNAVAILABLE`` with the reason, and the measured grid is remembered
    so ``audio.beat_sync_cut`` can snap to *this* tempo instead of a constant.
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
    engine = _engine(context, OPERATION_DETECT_BEATS)
    precheck = engine.precheck(
        SourceRequest(
            asset_id=payload.audio_asset_id,
            expected_sha256=audio_rec.content_sha256 or None,
        )
    )
    if not precheck.readable:
        return OperationOutcome(
            project,
            context.history,
            {
                "audio_asset_id": payload.audio_asset_id,
                "execution_status": EXECUTION_STATUS_UNAVAILABLE,
                "code": CODE_REQUIRES_MEDIA_BYTES,
                "detail": precheck.detail or "the asset has no readable media bytes",
                "tempo_bpm": None,
                "total_beats": 0,
                "beat_grid": None,
                "tempo_source": "unavailable",
            },
        )
    evidence = engine.detect_beats(
        BeatRequest(
            request_id=f"beats:{payload.audio_asset_id}",
            source_asset_id=payload.audio_asset_id,
            source_sha256=precheck.sha256,
            min_bpm=payload.min_bpm,
            max_bpm=payload.max_bpm,
            sensitivity=payload.sensitivity,
        )
    )
    if evidence.status != "detected" or evidence.tempo_bpm is None:
        return OperationOutcome(
            project,
            context.history,
            {
                "audio_asset_id": payload.audio_asset_id,
                "execution_status": EXECUTION_STATUS_READY,
                "tempo_bpm": None,
                "total_beats": 0,
                "beat_grid": None,
                "tempo_source": evidence.status,
                "onset_count": evidence.onset_count,
                "confidence": evidence.confidence,
                "analysis_rate_hz": precheck.sample_rate,
                "analyzer": evidence.analyzer,
                "detail": (
                    "no tempo could be measured from this signal: "
                    f"{evidence.status.replace('_', ' ')}"
                ),
            },
        )
    grid, beat_ref = _beat_grid_from_analysis(payload.audio_asset_id, evidence)
    assert grid is not None and beat_ref is not None  # status=="detected"
    grid = grid.model_copy(update={"content_sha256": precheck.sha256})
    record_beat_analysis(grid)
    return OperationOutcome(
        project,
        context.history,
        {
            "audio_asset_id": payload.audio_asset_id,
            "execution_status": EXECUTION_STATUS_READY,
            "tempo_bpm": grid.tempo_bpm,
            "total_beats": len(beat_ref.beats),
            "beat_grid": beat_ref.model_dump(mode="json"),
            "tempo_source": "measured",
            "confidence": grid.confidence,
            "onset_count": grid.onset_count,
            "beat_period_us": grid.beat_period_us,
            "first_beat_us": grid.first_beat_us,
            "bpm_window": [payload.min_bpm, payload.max_bpm],
            "analysis_rate_hz": precheck.sample_rate,
            "analyzer": grid.analyzer,
            "measured_sha256": precheck.sha256,
        },
    )


def _normalize_loudness(project: Project, context: OperationContext) -> OperationOutcome:
    """Level C (CONFIRMATION) handler for ``audio.normalize_loudness`` — real DSP.

    Two-pass FFmpeg ``loudnorm`` (measure, then apply the measured parameters)
    followed by an independent ``ebur128`` re-measurement of the *published*
    file.  The derived ``AssetRecord`` carries the measured ``sha256``, the
    measured loudness, and the signed passport id; if the produced file misses
    the target the operation fails instead of recording a "normalized" asset.
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
    output_id = payload.output_asset_id or _new_asset_id("norm")
    if output_id in known:
        raise CommandValidationError(
            f"audio.normalize_loudness output asset already exists: {output_id!r}"
        )
    engine = _engine(context, OPERATION_NORMALIZE_LOUDNESS)
    request_id = hashlib.sha256(
        f"{OPERATION_NORMALIZE_LOUDNESS}:{source_rec.content_sha256}:{output_id}:"
        f"{payload.profile}:{payload.target_lufs}:{payload.true_peak_db}".encode()
    ).hexdigest()[:32]
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id=request_id,
            source_asset_id=payload.audio_asset_id,
            output_asset_id=output_id,
            source_sha256=source_rec.content_sha256 or None,
            profile_id=payload.profile,
            # ``payload`` is a validated ``NormalizeLoudnessInput``: for a locked
            # profile the validator fills the three numbers from the profile, and
            # for ``custom`` it refuses to build unless the caller stated all
            # three.  So they are non-``None`` here by construction — asserted
            # rather than coerced, so a validator regression fails loudly.
            target_lufs=_require_number(payload.target_lufs, "target_lufs"),
            true_peak_dbtp=_require_number(payload.true_peak_db, "true_peak_db"),
            target_lra_lu=_require_number(payload.target_lra_lu, "target_lra_lu"),
            two_pass=True,
            transaction_id=context.command.command_id,
        )
    )
    if not evidence.verified or evidence.loudness_passed is not True:
        raise CommandExecutionError(
            f"audio.normalize_loudness produced an artifact that did not verify the "
            f"{payload.profile} target ({evidence.loudness_code or 'unverified'})"
        )
    normalized_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=evidence.sha256,
        duration_us=evidence.duration_us or source_rec.duration_us,
        parent_asset_ids=(source_rec.asset_id,),
        provenance={
            "source_asset_id": source_rec.asset_id,
            "source_sha256": source_rec.content_sha256,
            "profile": payload.profile,
            "target_lufs": payload.target_lufs,
            "true_peak_db": payload.true_peak_db,
            "target_lra_lu": payload.target_lra_lu,
            "standard": "ITU-R BS.1770-4 / EBU R128",
            "processor": "nagar.audio.studio.loudnorm.v2",
            "measured": {
                "integrated_lufs": evidence.integrated_lufs,
                "true_peak_dbtp": evidence.true_peak_dbtp,
                "loudness_range_lu": evidence.loudness_range_lu,
                "sample_rate": evidence.sample_rate,
                "channels": evidence.channels,
                "codec": evidence.codec,
                "spectral_centroid_hz": evidence.spectral_centroid_hz,
                "rms_dbfs": evidence.rms_dbfs,
                "acoustic_signature_sha256": evidence.acoustic_signature_sha256,
            },
            "stream_profile": CANONICAL_STREAM_PROFILE_ID,
            "canonical_sample_rate": CANONICAL_SAMPLE_RATE,
            "passport_id": evidence.passport_id,
            "passport_signed": bool(evidence.passport_signature),
            "artifact_verified": True,
            "artifact_path": evidence.output_path,
            "execution_status": EXECUTION_STATUS_READY,
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, normalized_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source_rec.asset_id,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "rendered_artifact",
            "artifact_verified": True,
            "profile": payload.profile,
            "target_lufs": payload.target_lufs,
            "true_peak_db": payload.true_peak_db,
            "content_sha256": evidence.sha256,
            "measured_integrated_lufs": evidence.integrated_lufs,
            "measured_true_peak_dbtp": evidence.true_peak_dbtp,
            "measured_lra_lu": evidence.loudness_range_lu,
            "loudness_passed": evidence.loudness_passed,
            "sample_rate": evidence.sample_rate,
            "channels": evidence.channels,
            "codec": evidence.codec,
            "passport_id": evidence.passport_id,
            "passport_signed": bool(evidence.passport_signature),
            "acoustic_signature_sha256": evidence.acoustic_signature_sha256,
            "spectral_centroid_hz": evidence.spectral_centroid_hz,
            "reused_existing": evidence.reused_existing,
            "elapsed_seconds": evidence.elapsed_seconds,
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

    output_id = payload.output_asset_id or _new_asset_id("ducked")
    request_sha256 = _request_fingerprint(
        f"{music_rec.content_sha256}:{voice_rec.content_sha256}:duck:{payload.duck_attenuation_db}"
    )

    ducked_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=request_sha256,
        duration_us=music_rec.duration_us,
        parent_asset_ids=(music_rec.asset_id, voice_rec.asset_id),
        provenance={
            "music_asset_id": music_rec.asset_id,
            "voice_asset_id": voice_rec.asset_id,
            "duck_attenuation_db": payload.duck_attenuation_db,
            "attack_ms": payload.attack_ms,
            "release_ms": payload.release_ms,
            "processor": "nagar.audio.studio.sidechain.v1",
            **_state_only_evidence(
                processor="nagar.audio.studio.sidechain.v1",
                request_sha256=request_sha256,
                parameters={
                    "duck_attenuation_db": payload.duck_attenuation_db,
                    "attack_ms": payload.attack_ms,
                    "release_ms": payload.release_ms,
                },
            ),
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
            "request_sha256": ducked_record.content_sha256,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "state_only",
            "artifact_verified": False,
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
    measured = last_beat_analysis(payload.audio_asset_id)
    if measured is not None:
        bpm = measured.tempo_bpm
        beat_duration_us = measured.beat_period_us
        tempo_source = "measured"
        confidence = measured.confidence
    else:
        # The declared studio anchor.  Labelled as such everywhere it appears:
        # an unmeasured tempo is a *choice*, never a detection.
        bpm = 120.0
        beat_duration_us = 500_000
        tempo_source = "default_120"
        confidence = 0.0
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
            "tempo_bpm": bpm,
            "beat_period_us": beat_duration_us,
            "tempo_source": tempo_source,
            "tempo_confidence": confidence,
            "audio_content_sha256": audio_rec.content_sha256,
            "execution_status": EXECUTION_STATUS_READY,
            "total_cuts": len(cuts),
            "total_duration_us": current_time_us,
            "cuts": cuts,
            "note": (
                "cut points follow the tempo measured by audio.detect_beats"
                if tempo_source == "measured"
                else "no measurement for this asset: cut points use the declared "
                "120 BPM anchor and are not beat-accurate"
            ),
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
    """Level B (REVERSIBLE) handler for ``audio.remove_noise`` — real filtering.

    ``highpass`` + ``afftdn`` (stationary spectral denoise) run for real and the
    output is verified like any other artifact: measured hash, decoded, and an
    acoustic signature that a later shot can be compared against.  No claim of
    semantic separation is made anywhere in the evidence — this removes a static
    noise floor, it does not isolate a speaker.
    """
    payload = RemoveNoiseInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_REMOVE_NOISE)
    output_id = payload.output_asset_id or _new_asset_id("denoised")
    if output_id in known:
        raise CommandValidationError(
            f"audio.remove_noise output asset already exists: {output_id!r}"
        )
    engine = _engine(context, OPERATION_REMOVE_NOISE)
    request_id = hashlib.sha256(
        f"{OPERATION_REMOVE_NOISE}:{source.content_sha256}:{output_id}:{payload.strength}".encode()
    ).hexdigest()[:32]
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id=request_id,
            source_asset_id=payload.audio_asset_id,
            output_asset_id=output_id,
            source_sha256=source.content_sha256 or None,
            strength=payload.strength,
            preserve_speech=payload.preserve_speech,
            transaction_id=context.command.command_id,
        )
    )
    if not evidence.verified:
        raise CommandExecutionError(
            "audio.remove_noise produced an artifact that failed verification"
        )
    record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=evidence.sha256,
        duration_us=evidence.duration_us or source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "source_sha256": source.content_sha256,
            "strength": payload.strength,
            "preserve_speech": payload.preserve_speech,
            "processor": "nagar.audio.studio.denoise.v2",
            "filter_chain": ["highpass=f=80", "afftdn"],
            "claims": "static broadband noise reduction only; no semantic separation",
            "measured": {
                "spectral_centroid_hz": evidence.spectral_centroid_hz,
                "rms_dbfs": evidence.rms_dbfs,
                "crest_factor_db": evidence.crest_factor_db,
                "dynamic_range_db": evidence.dynamic_range_db,
                "acoustic_signature_sha256": evidence.acoustic_signature_sha256,
                "integrated_lufs": evidence.integrated_lufs,
            },
            "passport_id": evidence.passport_id,
            "artifact_verified": True,
            "artifact_path": evidence.output_path,
            "execution_status": EXECUTION_STATUS_READY,
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": source.asset_id,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "rendered_artifact",
            "artifact_verified": True,
            "strength": payload.strength,
            "preserve_speech": payload.preserve_speech,
            "content_sha256": evidence.sha256,
            "acoustic_signature_sha256": evidence.acoustic_signature_sha256,
            "spectral_centroid_hz": evidence.spectral_centroid_hz,
            "rms_dbfs": evidence.rms_dbfs,
            "crest_factor_db": evidence.crest_factor_db,
            "dynamic_range_db": evidence.dynamic_range_db,
            "passport_id": evidence.passport_id,
            "reused_existing": evidence.reused_existing,
            "elapsed_seconds": evidence.elapsed_seconds,
        },
    )


def _deess(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for audio.deess.

    Derives a de-essed audio track plan (narrow-band sibilance compression).
    """
    payload = DeessInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_DEESS)

    output_id = payload.output_asset_id or _new_asset_id("deessed")
    request_sha256 = _request_fingerprint(
        f"{source.content_sha256}:deess:{payload.frequency_hz}:{payload.threshold_db}"
    )

    deessed_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=request_sha256,
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "frequency_hz": payload.frequency_hz,
            "threshold_db": payload.threshold_db,
            "processor": "nagar.audio.studio.deess.v1",
            **_state_only_evidence(
                processor="nagar.audio.studio.deess.v1",
                request_sha256=request_sha256,
                parameters={
                    "frequency_hz": payload.frequency_hz,
                    "threshold_db": payload.threshold_db,
                },
            ),
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
            "request_sha256": deessed_record.content_sha256,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "state_only",
            "artifact_verified": False,
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

    output_id = payload.output_asset_id or _new_asset_id("eqvoice")
    request_sha256 = _request_fingerprint(
        f"{source.content_sha256}:eqvoice:{payload.preset}:{payload.gain_db}"
    )

    eq_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=request_sha256,
        duration_us=source.duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "preset": payload.preset,
            "gain_db": payload.gain_db,
            "applied_bands": dict(applied_bands),
            "cuts": cuts,
            "processor": "nagar.audio.studio.eq.v1",
            **_state_only_evidence(
                processor="nagar.audio.studio.eq.v1",
                request_sha256=request_sha256,
                parameters={
                    "preset": payload.preset,
                    "gain_db": payload.gain_db,
                    "applied_bands": dict(applied_bands),
                },
            ),
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
            "request_sha256": eq_record.content_sha256,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "state_only",
            "artifact_verified": False,
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
    output_id = payload.output_asset_id or _new_asset_id("stretched")
    request_sha256 = _request_fingerprint(
        f"{source.content_sha256}:stretch:{payload.factor}:{payload.preserve_pitch}"
    )

    stretched_record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=request_sha256,
        duration_us=new_duration_us,
        parent_asset_ids=(source.asset_id,),
        provenance={
            "source_asset_id": source.asset_id,
            "factor": payload.factor,
            "preserve_pitch": payload.preserve_pitch,
            "source_duration_us": source.duration_us,
            "processor": "nagar.audio.studio.stretch.v1",
            **_state_only_evidence(
                processor="nagar.audio.studio.stretch.v1",
                request_sha256=request_sha256,
                parameters={
                    "factor": payload.factor,
                    "preserve_pitch": payload.preserve_pitch,
                },
            ),
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
            "request_sha256": stretched_record.content_sha256,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "state_only",
            "artifact_verified": False,
        },
    )


def _remove_vocal(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for ``audio.remove_vocal`` — honest refusal.

    Source separation needs a local model (Demucs/Spleeter-class).  This
    repository ships none, and no FFmpeg filter performs semantic separation, so
    the operation **derives nothing** and returns
    ``UNAVAILABLE / REQUIRES_LOCAL_MODEL``.  A fabricated stem set (hashes of a
    seed string, presented as separated audio) is exactly the behaviour this
    phase removes: a caller who receives stems must be able to play them.

    When a local separation engine is composed into
    :class:`~nexus_ai_agent.creative.studio.audio_engine.AudioEnginePort`, this
    handler is the single place that starts producing stems — the input/output
    contract does not change.
    """
    payload = RemoveVocalInput.model_validate(context.input_data)
    known = _asset_index(project)
    source = _require_audio_asset(known, payload.audio_asset_id, OPERATION_REMOVE_VOCAL)
    return OperationOutcome(
        project,
        context.history,
        {
            "source_asset_id": source.asset_id,
            "execution_status": EXECUTION_STATUS_UNAVAILABLE,
            "code": CODE_REQUIRES_LOCAL_MODEL,
            "derivation": "unavailable",
            "artifact_verified": False,
            "stem_policy": payload.stem_policy,
            "strength": payload.strength,
            "stem_count": 0,
            "stem_asset_ids": {},
            "content_sha256": {},
            "requires": {
                "engine": "local source-separation model (Demucs/Spleeter class)",
                "network": False,
                "reason": "no separation model is installed in this deployment",
            },
            "detail": (
                "audio.remove_vocal is UNAVAILABLE: separating vocals from a mix "
                "requires a local model that is not installed. No stems were "
                "derived and no asset was recorded."
            ),
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


def _execute_stage_b(project: Project, context: OperationContext) -> OperationOutcome:
    """Typed K1..K4 dispatch through an injected port; never imports any DSP."""
    operation = context.command.operation
    payload = STAGE_B_INPUT_MODELS[operation].model_validate(context.input_data)
    known = _asset_index(project)
    parents = tuple(
        _require_audio_asset(known, asset_id, operation) for asset_id in kernel_source_ids(payload)
    )
    output_id = (
        payload.output_asset_id
        or "stageb_" + hashlib.sha256(context.command.command_id.encode()).hexdigest()[:16]
    )
    if output_id in known:
        raise CommandValidationError(f"{operation} output asset already exists: {output_id!r}")
    engine = _engine(context, operation)
    execute = getattr(engine, "execute_stage_b", None)
    if not callable(execute):
        raise CommandValidationError("the composed audio engine does not implement Stage-B kernels")
    try:
        request = StageBAudioRequest.model_validate(
            {
                "request_id": hashlib.sha256(
                    f"{operation}:{context.command.command_id}".encode()
                ).hexdigest()[:32],
                "operation": operation,
                "output_asset_id": output_id,
                "payload": payload,
                "source_hashes": {parent.asset_id: parent.content_sha256 for parent in parents},
                "transaction_id": context.command.command_id,
            }
        )
    except ValueError as error:
        raise CommandValidationError(
            f"{operation} requires content-bound source assets: {error}"
        ) from error
    evidence = execute(request)
    if not evidence.verified:
        raise CommandExecutionError(f"{operation} produced an unverified artifact")
    record = AssetRecord(
        asset_id=output_id,
        media_kind="audio",
        content_sha256=evidence.sha256,
        duration_us=evidence.duration_us,
        parent_asset_ids=tuple(parent.asset_id for parent in parents),
        provenance={
            "processor": "omega.stage_b.v1",
            "operation": operation,
            "source_hashes": request.source_hashes,
            "parameters": evidence.parameters,
            "artifact_path": evidence.output_path,
            "artifact_verified": True,
            "passport_id": evidence.passport_id,
            "passport_signed": bool(evidence.passport_signature),
            "measured_integrated_lufs": evidence.integrated_lufs,
            "measured_true_peak_dbtp": evidence.true_peak_dbtp,
        },
    )
    return OperationOutcome(
        project.model_copy(update={"assets": [*project.assets, record]}),
        context.history,
        {
            "asset_id": output_id,
            "execution_status": EXECUTION_STATUS_READY,
            "derivation": "rendered_artifact",
            "artifact_verified": True,
            "content_sha256": evidence.sha256,
            "artifact_path": evidence.output_path,
            "passport_id": evidence.passport_id,
            "passport_signed": bool(evidence.passport_signature),
            "measured_integrated_lufs": evidence.integrated_lufs,
            "measured_true_peak_dbtp": evidence.true_peak_dbtp,
            "reused_existing": evidence.reused_existing,
            "parameters": evidence.parameters,
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

    for operation, description in (
        (OPERATION_SPATIALIZE, "K1 ACN/SN3D spatial rotation, binaural head model and passive FDN"),
        (OPERATION_RESTORE_VOICE, "K2 constrained declipping, MMSE-LSA and delayed WPE"),
        (OPERATION_SYNC_FOLEY, "K3 measured sub-frame A/V alignment and sample-exact modal Foley"),
        (OPERATION_BARK_MASTER, "K4 masking-aware three-stem -24 LKFS / -1 dBTP master"),
    ):
        registry.register_operation(
            DOMAIN,
            operation.split(".", 1)[1],
            OperationSpec(
                operation_id=operation,
                description=description,
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=STAGE_B_INPUT_MODELS[operation],
                handler=_execute_stage_b,
                required_packs=(AUDIO_PACKAGE_ID,),
                deterministic=True,
            ),
        )


def build_audio_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry pre-loaded with audio pack operations."""
    registry = CapabilityRegistry()
    register_audio_operations(registry)
    return registry
