"""The audio pack's *refusal* and *degenerate* paths, with declared doubles.

Companion to ``test_omega_audio_bus.py`` (which runs the real engine): this file
covers the branches that a happy-path run never reaches — an unreadable source,
a signal with no measurable tempo, an engine that refuses its own artifact, a
replayed request, and the alignment arithmetic's clamping.  The engine is a
declared double here so each branch can be produced deterministically; the
numbers the real engine measures are pinned in the OMEGA suites instead.

The property under test everywhere is the same: **no branch may claim a
measurement it did not make.**  A refusal is an ``UNAVAILABLE`` with a code, an
artifact that fails verification is an exception (never an ``AssetRecord``), and
a replayed request reuses the verified artifact instead of re-publishing.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
from nexus_ai_agent.creative.studio.audio_engine import (
    ArtifactEvidence,
    BeatAnalysis,
    EngineRejected,
    SourcePrecheck,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandExecutionError,
    CommandValidationError,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)

HASH = "sha256:" + "7" * 64


class _Engine:
    """A scripted engine double: every response is chosen by the test."""

    def __init__(
        self,
        *,
        readable: bool = True,
        analysis: BeatAnalysis | None = None,
        evidence: ArtifactEvidence | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.readable = readable
        self.analysis = analysis
        self.evidence = evidence
        self.raises = raises
        self.calls: list[str] = []

    def precheck(self, request: Any) -> SourcePrecheck:  # noqa: ANN401
        self.calls.append("precheck")
        if not self.readable:
            return SourcePrecheck(
                asset_id=request.asset_id, exists=True, readable=False, detail="unreadable codec"
            )
        return SourcePrecheck(
            asset_id=request.asset_id,
            path=f"/media/{request.asset_id}.wav",
            exists=True,
            readable=True,
            sha256=request.expected_sha256 or HASH,
            sample_rate=48_000,
            channels=2,
        )

    def detect_beats(self, request: Any) -> BeatAnalysis:  # noqa: ANN401
        self.calls.append("detect_beats")
        if self.raises is not None:
            raise self.raises
        assert self.analysis is not None
        return self.analysis

    def normalize_loudness(self, request: Any) -> ArtifactEvidence:  # noqa: ANN401
        self.calls.append("normalize_loudness")
        if self.raises is not None:
            raise self.raises
        assert self.evidence is not None
        return self.evidence

    def remove_noise(self, request: Any) -> ArtifactEvidence:  # noqa: ANN401
        self.calls.append("remove_noise")
        if self.raises is not None:
            raise self.raises
        assert self.evidence is not None
        return self.evidence


def _project(*assets: AssetRecord) -> Project:
    project = new_project(
        "p_evidence", "Evidence", Timeline(timeline_id="tl", duration_us=10_000_000)
    )
    return project.model_copy(update={"assets": list(assets)})


def _audio(asset_id: str = "voice", kind: str = "audio") -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id, media_kind=kind, content_sha256=HASH, duration_us=8_000_000
    )


def _video(asset_id: str = "cam") -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id, media_kind="video", content_sha256=HASH, duration_us=8_000_000
    )


def _bus(project: Project, engine: _Engine) -> CommandBus:
    return CommandBus(
        project, registry=build_audio_registry(), allow_experimental=True, audio_engine=engine
    )


def _dispatch(
    bus: CommandBus, operation: str, payload: dict[str, Any], **kwargs: Any
) -> Any:  # noqa: ANN401
    return bus.dispatch(
        TypedCommand(
            command_id=f"cmd_{operation}_{abs(hash(str(payload))) % 10**6}",
            operation=operation,
            input=payload,
            **kwargs,
        )
    )


def _verified_evidence(*, verified: bool = True, passed: bool | None = True) -> ArtifactEvidence:
    return ArtifactEvidence(
        asset_id="out",
        output_path="/workspace/media/out.flac",
        sha256="sha256:" + "3" * 64,
        size_bytes=4096,
        duration_us=8_000_000,
        sample_rate=48_000,
        channels=2,
        codec="flac",
        integrated_lufs=-14.0,
        true_peak_dbtp=-1.2,
        loudness_checked=True,
        loudness_passed=passed,
        loudness_code=None if passed else "integrated_loudness_out_of_range",
        spectral_centroid_hz=880.0,
        rms_dbfs=-30.0,
        crest_factor_db=10.0,
        dynamic_range_db=6.0,
        acoustic_signature_sha256="sha256:" + "4" * 64,
        verified=verified,
        passport_id="pp_test",
        passport_signature="ab" * 32,
        canonical_profile_ok=True,
        elapsed_seconds=1.25,
        reused_existing=False,
    )


# --------------------------------------------------------------------------- #
# detect_beats: every honest non-answer
# --------------------------------------------------------------------------- #


def test_beats_accepts_video_media() -> None:
    engine = _Engine(
        analysis=BeatAnalysis(
            status="detected",
            tempo_bpm=100.0,
            beat_period_us=600_000,
            confidence=0.7,
            onset_count=12,
        )
    )
    result = _dispatch(
        _bus(_project(_video("cam")), engine),
        "audio.detect_beats",
        {"audio_asset_id": "cam", "min_bpm": 60, "max_bpm": 180},
    )
    assert result.output["tempo_bpm"] == 100.0
    assert result.output["tempo_source"] == "measured"


def test_beats_rejects_a_non_media_asset() -> None:
    still = AssetRecord(
        asset_id="still", media_kind="image", content_sha256=HASH, duration_us=0
    )
    with pytest.raises(CommandValidationError, match="requires audio or video media"):
        _dispatch(
            _bus(_project(still), _Engine()),
            "audio.detect_beats",
            {"audio_asset_id": "still"},
        )


def test_beats_reports_unreadable_bytes_without_a_code() -> None:
    result = _dispatch(
        _bus(_project(_audio()), _Engine(readable=False)),
        "audio.detect_beats",
        {"audio_asset_id": "voice"},
    )
    assert result.output["execution_status"] == "UNAVAILABLE"
    assert result.output["code"] == "REQUIRES_MEDIA_BYTES"
    assert result.output["detail"] == "unreadable codec"
    assert result.output["tempo_bpm"] is None
    assert result.output["tempo_source"] == "unavailable"


@pytest.mark.parametrize("status", ["insufficient_onsets", "irregular_onsets", "empty_audio"])
def test_beats_reports_no_tempo_as_a_reason_not_a_default(status: str) -> None:
    engine = _Engine(
        analysis=BeatAnalysis(status=status, tempo_bpm=None, confidence=0.0, onset_count=0)
    )
    result = _dispatch(
        _bus(_project(_audio()), engine),
        "audio.detect_beats",
        {"audio_asset_id": "voice"},
    )
    assert result.output["execution_status"] == "READY"
    assert result.output["tempo_bpm"] is None
    assert result.output["beat_grid"] is None
    assert result.output["tempo_source"] == status
    assert "no tempo could be measured" in result.output["detail"]


def test_beats_survives_an_engine_refusal_as_a_typed_error() -> None:
    engine = _Engine(raises=EngineRejected("decode reported corruption"))
    with pytest.raises(CommandExecutionError, match="corruption"):
        _dispatch(
            _bus(_project(_audio()), engine), "audio.detect_beats", {"audio_asset_id": "voice"}
        )


def test_beats_records_a_grid_of_the_measured_length() -> None:
    engine = _Engine(
        analysis=BeatAnalysis(
            status="detected",
            tempo_bpm=96.0,
            beat_period_us=625_000,
            confidence=0.62,
            onset_count=15,
            first_beat_us=10_000,
        )
    )
    result = _dispatch(
        _bus(_project(_audio()), engine),
        "audio.detect_beats",
        {"audio_asset_id": "voice", "max_bpm": 160},
    )
    grid = result.output["beat_grid"]
    assert grid["tempo_bpm"] == 96.0
    assert grid["beats"][0]["time_us"] == 10_000
    assert grid["beats"][1]["time_us"] == 635_000
    assert grid["beats"][0]["is_downbeat"] is True
    assert grid["beats"][1]["is_downbeat"] is False
    assert result.output["confidence"] == 0.62


# --------------------------------------------------------------------------- #
# normalize_loudness: refusals, replay, and the "did not verify" fence
# --------------------------------------------------------------------------- #


def test_normalize_rejects_a_non_media_asset() -> None:
    still = AssetRecord(asset_id="still", media_kind="image", content_sha256=HASH, duration_us=0)
    with pytest.raises(CommandValidationError, match="requires audio or video media"):
        _dispatch(
            _bus(_project(still), _Engine(evidence=_verified_evidence())),
            "audio.normalize_loudness",
            {"audio_asset_id": "still", "output_asset_id": "x", "confirmed": True},
            confirmed=True,
        )


def test_normalize_rejects_an_existing_output_id() -> None:
    with pytest.raises(CommandValidationError, match="output asset already exists"):
        _dispatch(
            _bus(_project(_audio("a"), _audio("taken")), _Engine(evidence=_verified_evidence())),
            "audio.normalize_loudness",
            {"audio_asset_id": "a", "output_asset_id": "taken", "confirmed": True},
            confirmed=True,
        )


def test_normalize_without_a_confirmation_flag_is_refused() -> None:
    with pytest.raises(CommandValidationError, match="explicit user confirmation"):
        _dispatch(
            _bus(_project(_audio()), _Engine(evidence=_verified_evidence())),
            "audio.normalize_loudness",
            {"audio_asset_id": "voice", "output_asset_id": "out"},
            confirmed=True,
        )


def test_normalize_refuses_an_artifact_that_missed_the_target() -> None:
    engine = _Engine(evidence=_verified_evidence(passed=False))
    with pytest.raises(CommandExecutionError, match="did not verify"):
        _dispatch(
            _bus(_project(_audio()), engine),
            "audio.normalize_loudness",
            {"audio_asset_id": "voice", "output_asset_id": "out", "confirmed": True},
            confirmed=True,
        )


def test_normalize_refuses_an_unverified_artifact() -> None:
    engine = _Engine(evidence=_verified_evidence(verified=False))
    with pytest.raises(CommandExecutionError, match="did not verify"):
        _dispatch(
            _bus(_project(_audio()), engine),
            "audio.normalize_loudness",
            {"audio_asset_id": "voice", "output_asset_id": "out", "confirmed": True},
            confirmed=True,
        )


def test_normalize_reports_a_reused_artifact_as_reused() -> None:
    reused = _verified_evidence().model_copy(update={"reused_existing": True})
    result = _dispatch(
        _bus(_project(_audio()), _Engine(evidence=reused)),
        "audio.normalize_loudness",
        {"audio_asset_id": "voice", "output_asset_id": "out", "confirmed": True},
        confirmed=True,
    )
    assert result.output["reused_existing"] is True


def test_normalize_generates_an_output_id_when_none_is_given() -> None:
    result = _dispatch(
        _bus(_project(_audio()), _Engine(evidence=_verified_evidence())),
        "audio.normalize_loudness",
        {"audio_asset_id": "voice", "confirmed": True},
        confirmed=True,
    )
    assert result.output["asset_id"].startswith("norm_")


# --------------------------------------------------------------------------- #
# remove_noise: the same fences
# --------------------------------------------------------------------------- #


def test_remove_noise_rejects_an_existing_output_id() -> None:
    with pytest.raises(CommandValidationError, match="output asset already exists"):
        _dispatch(
            _bus(
                _project(_audio("voice"), _audio("taken")),
                _Engine(evidence=_verified_evidence()),
            ),
            "audio.remove_noise",
            {"audio_asset_id": "voice", "output_asset_id": "taken"},
        )


def test_remove_noise_refuses_an_unverified_artifact() -> None:
    engine = _Engine(evidence=_verified_evidence(verified=False))
    with pytest.raises(CommandExecutionError, match="failed verification"):
        _dispatch(
            _bus(_project(_audio()), engine),
            "audio.remove_noise",
            {"audio_asset_id": "voice", "output_asset_id": "out"},
        )


def test_remove_noise_generates_an_output_id_when_none_is_given() -> None:
    result = _dispatch(
        _bus(_project(_audio()), _Engine(evidence=_verified_evidence())),
        "audio.remove_noise",
        {"audio_asset_id": "voice"},
    )
    assert result.output["asset_id"].startswith("denoised_")


# --------------------------------------------------------------------------- #
# state-only operations: media-kind and reference validation
# --------------------------------------------------------------------------- #


def test_duck_music_requires_two_known_audio_assets() -> None:
    with pytest.raises(CommandValidationError, match="unknown music asset"):
        _dispatch(
            _bus(_project(_audio("vo")), _Engine()),
            "audio.duck_music",
            {"music_asset_id": "nope", "voice_asset_id": "vo"},
        )
    with pytest.raises(CommandValidationError, match="unknown voice asset"):
        _dispatch(
            _bus(_project(_audio("music"), _audio("vo")), _Engine()),
            "audio.duck_music",
            {"music_asset_id": "music", "voice_asset_id": "nope"},
        )


def test_align_music_requires_audio_or_video_media() -> None:
    still = AssetRecord(asset_id="still", media_kind="image", content_sha256=HASH, duration_us=0)
    grid = {"tempo_bpm": 120.0, "target_start_us": 0}
    with pytest.raises(CommandValidationError, match="unknown music asset"):
        _dispatch(
            _bus(_project(), _Engine()),
            "audio.align_music",
            {"music_asset_id": "ghost", **grid},
        )
    with pytest.raises(CommandValidationError, match="requires audio or video media"):
        _dispatch(
            _bus(_project(still), _Engine()),
            "audio.align_music",
            {"music_asset_id": "still", **grid},
        )


def test_align_music_clamps_the_shift_and_reports_confidence() -> None:
    # 120 BPM ⇒ a beat every 500 ms.  The nearest beat to 900 ms is 1000 ms, so
    # the raw offset is +100 ms; a 50 ms allowance must clamp it and report it.
    result = _dispatch(
        _bus(_project(_audio("music")), _Engine()),
        "audio.align_music",
        {
            "music_asset_id": "music",
            "target_start_us": 900_000,
            "tempo_bpm": 120.0,
            "anchor": "nearest_beat",
            "max_shift_us": 50_000,
        },
    )
    assert result.output["raw_offset_us"] == 100_000
    assert result.output["offset_us"] == 50_000
    assert result.output["clamped"] is True
    # Fully clamped ⇒ zero of the requested alignment was delivered.  The
    # confidence is the *unmet* fraction, so it must not read as success.
    assert result.output["confidence"] == 0.0


def test_align_music_inside_the_allowance_is_not_clamped() -> None:
    result = _dispatch(
        _bus(_project(_audio("music")), _Engine()),
        "audio.align_music",
        {
            "music_asset_id": "music",
            "target_start_us": 400_000,
            "tempo_bpm": 120.0,
            "anchor": "nearest_beat",
            "max_shift_us": 200_000,
        },
    )
    assert result.output["raw_offset_us"] == result.output["offset_us"]
    assert result.output["clamped"] is False


def test_align_music_with_zero_max_shift_is_exact() -> None:
    result = _dispatch(
        _bus(_project(_audio("music")), _Engine()),
        "audio.align_music",
        {
            "music_asset_id": "music",
            "target_start_us": 500_000,
            "tempo_bpm": 120.0,
            "max_shift_us": 0,
        },
    )
    assert result.output["offset_us"] == 0
    assert result.output["confidence"] == 1.0


def test_beat_sync_cut_rejects_unknown_assets() -> None:
    with pytest.raises(CommandValidationError, match="unknown audio asset"):
        _dispatch(
            _bus(_project(), _Engine()),
            "audio.beat_sync_cut",
            {"audio_asset_id": "ghost", "clip_asset_ids": ["cam"]},
        )
    with pytest.raises(CommandValidationError, match="unknown clip asset"):
        _dispatch(
            _bus(_project(_audio("music")), _Engine()),
            "audio.beat_sync_cut",
            {"audio_asset_id": "music", "clip_asset_ids": ["ghost"]},
        )


# --------------------------------------------------------------------------- #
# the measured → beat-sync chain, and the model-level refusals it depends on
# --------------------------------------------------------------------------- #


def test_a_measured_grid_drives_beat_sync_cut_instead_of_the_anchor() -> None:
    """The end of the "no more fake 120 BPM" chain: cuts follow the measurement."""
    from nexus_ai_agent.creative.packs.audio.models import (
        DetectedBeatGrid,
        clear_beat_analysis,
        record_beat_analysis,
    )

    clear_beat_analysis("music")
    record_beat_analysis(
        DetectedBeatGrid(
            audio_asset_id="music",
            tempo_bpm=96.0,
            beat_period_us=625_000,
            first_beat_us=0,
            confidence=0.75,
            onset_count=16,
            min_bpm=60.0,
            max_bpm=180.0,
        )
    )
    try:
        result = _dispatch(
            _bus(_project(_audio("music")), _Engine()),
            "audio.beat_sync_cut",
            {"audio_asset_id": "music", "clip_asset_ids": ["music"], "beats_per_cut": 4},
        )
        assert result.output["tempo_source"] == "measured"
        assert result.output["tempo_bpm"] == 96.0
        assert result.output["beat_period_us"] == 625_000
        assert "follow the tempo measured" in result.output["note"]
        first = result.output["cuts"][0]
        assert first["end_us"] == 4 * 625_000
        assert first["snapped_beat"] == 1
    finally:
        clear_beat_analysis("music")


def test_clearing_the_measured_grid_falls_back_to_the_labelled_anchor() -> None:
    from nexus_ai_agent.creative.packs.audio.models import (
        DetectedBeatGrid,
        clear_beat_analysis,
        record_beat_analysis,
    )

    record_beat_analysis(
        DetectedBeatGrid(
            audio_asset_id="music",
            tempo_bpm=96.0,
            beat_period_us=625_000,
            first_beat_us=0,
            confidence=0.75,
            onset_count=16,
            min_bpm=60.0,
            max_bpm=180.0,
        )
    )
    clear_beat_analysis()
    result = _dispatch(
        _bus(_project(_audio("music")), _Engine()),
        "audio.beat_sync_cut",
        {"audio_asset_id": "music", "clip_asset_ids": ["music"]},
    )
    assert result.output["tempo_source"] == "default_120"
    assert result.output["tempo_bpm"] == 120.0
    assert "not beat-accurate" in result.output["note"]


def test_normalize_refuses_an_unknown_asset() -> None:
    with pytest.raises(CommandValidationError, match="references unknown asset"):
        _dispatch(
            _bus(_project(), _Engine(evidence=_verified_evidence())),
            "audio.normalize_loudness",
            {"audio_asset_id": "ghost", "output_asset_id": "out", "confirmed": True},
            confirmed=True,
        )


def test_custom_profile_needs_all_three_numbers() -> None:
    from pydantic import ValidationError

    from nexus_ai_agent.creative.packs.audio.models import NormalizeLoudnessInput

    with pytest.raises(ValidationError, match="requires true_peak_db, target_lra_lu"):
        NormalizeLoudnessInput(
            audio_asset_id="voice",
            profile="custom",
            target_lufs=-18.0,
            confirmed=True,
        )


def test_single_pass_loudness_is_refused_at_the_model() -> None:
    from pydantic import ValidationError

    from nexus_ai_agent.creative.packs.audio.models import NormalizeLoudnessInput

    with pytest.raises(ValidationError, match="single-pass"):
        NormalizeLoudnessInput(
            audio_asset_id="voice",
            profile="streaming_master",
            two_pass=False,
            confirmed=True,
        )


def test_beat_grid_derives_its_total_from_the_markers() -> None:
    from nexus_ai_agent.creative.packs.audio.models import BeatGridRef, BeatMarker

    grid = BeatGridRef(
        audio_asset_id="music",
        tempo_bpm=100.0,
        beats=(
            BeatMarker(time_us=0, confidence=0.9, beat_number=1, is_downbeat=True),
            BeatMarker(time_us=600_000, confidence=0.8, beat_number=2),
        ),
        # Deliberately wrong: the model reconciles the count with the markers,
        # so a caller cannot report a grid length its markers do not support.
        total_beats=99,
    )
    assert grid.total_beats == 2
