"""Unit tests for ``nexus.audio.studio`` pack operations, permissions, and contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.application.ports.audio_engine import ArtifactEvidence
from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    BeatGridRef,
    BeatMarker,
    DetectBeatsInput,
)
from nexus_ai_agent.creative.packs.audio.operations import (
    build_audio_registry,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionDeniedError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)


class _StubEngine:
    """An in-memory engine double: the pack contract, without any media bytes.

    The handlers under test are pure: they validate, decide and report.  What
    the engine *measures* is pinned separately against real FFmpeg
    (``tests/unit/test_omega_*``); here the double only proves that the handler
    forwards the request and reports what came back — including refusing.
    """

    def __init__(self) -> None:
        self.loudness_requests: list[object] = []
        self.beat_requests: list[object] = []

    def health(self) -> object:  # pragma: no cover - not exercised here
        raise AssertionError("health() is not part of this contract test")

    def precheck(self, request: object) -> object:
        from nexus_ai_agent.application.ports.audio_engine import SourcePrecheck

        asset_id = request.asset_id
        return SourcePrecheck(
            asset_id=asset_id,
            path=f"/media/{asset_id}.wav",
            exists=True,
            sha256="sha256:" + "1" * 64,
            size_bytes=1024,
            sample_rate=48_000,
            channels=1,
            duration_us=10_000_000,
            codec="pcm_s16le",
            readable=True,
        )

    def detect_beats(self, request: object) -> object:
        from nexus_ai_agent.application.ports.audio_engine import BeatAnalysis

        self.beat_requests.append(request)
        return BeatAnalysis(
            status="detected",
            tempo_bpm=96.0,
            beat_period_us=625_000,
            confidence=0.82,
            first_beat_us=0,
            onset_count=16,
            min_bpm=request.min_bpm,
            max_bpm=request.max_bpm,
        )

    def normalize_loudness(self, request: object) -> ArtifactEvidence:
        self.loudness_requests.append(request)
        return ArtifactEvidence(
            asset_id=request.output_asset_id,
            output_path="/workspace/media/master.flac",
            sha256="sha256:" + "2" * 64,
            size_bytes=4096,
            duration_us=10_000_000,
            sample_rate=48_000,
            channels=2,
            codec="flac",
            integrated_lufs=request.target_lufs,
            true_peak_dbtp=-1.4,
            loudness_range_lu=6.0,
            loudness_checked=True,
            loudness_passed=True,
            verified=True,
            passport_id="pp_test",
            passport_signature="ab" * 64,
            spectral_centroid_hz=1200.0,
            rms_dbfs=-18.0,
            acoustic_signature_sha256="sha256:" + "3" * 64,
        )

    def remove_noise(self, request: object) -> ArtifactEvidence:  # pragma: no cover
        raise AssertionError("remove_noise is covered by the omega suite")


def _setup_audio_bus() -> tuple[Project, CommandBus]:
    registry = build_audio_registry()

    audio_rec = AssetRecord(
        asset_id="music_track_01",
        media_kind="audio",
        content_sha256="sha256:music1234567890",
        duration_us=10_000_000,
    )
    voice_rec = AssetRecord(
        asset_id="voice_track_01",
        media_kind="audio",
        content_sha256="sha256:voice1234567890",
        duration_us=8_000_000,
    )
    clip_1 = AssetRecord(
        asset_id="clip_01",
        media_kind="video",
        content_sha256="sha256:video01",
        duration_us=4_000_000,
    )
    clip_2 = AssetRecord(
        asset_id="clip_02",
        media_kind="video",
        content_sha256="sha256:video02",
        duration_us=4_000_000,
    )

    timeline = Timeline(timeline_id="tl_audio", duration_us=10_000_000)
    project = new_project("test_audio_proj", "Audio Studio Test", timeline)
    project = project.model_copy(update={"assets": [audio_rec, voice_rec, clip_1, clip_2]})
    bus = CommandBus(
        project, registry=registry, allow_experimental=True, audio_engine=_StubEngine()
    )
    return project, bus


def test_detect_beats_input_validation() -> None:
    # valid
    inp = DetectBeatsInput(audio_asset_id="music_track_01", min_bpm=80, max_bpm=160)
    assert inp.min_bpm == 80.0
    assert inp.max_bpm == 160.0

    # invalid: max_bpm <= min_bpm
    with pytest.raises(ValidationError, match="max_bpm"):
        DetectBeatsInput(audio_asset_id="music_track_01", min_bpm=120, max_bpm=100)


def test_beat_marker_and_grid_models() -> None:
    marker = BeatMarker(time_us=500_000, confidence=0.95, beat_number=1, is_downbeat=True)
    assert marker.time_us == 500_000
    assert marker.is_downbeat is True

    grid = BeatGridRef(
        audio_asset_id="music_track_01",
        tempo_bpm=120.0,
        beats=[marker],
        total_beats=1,
    )
    assert grid.tempo_bpm == 120.0
    assert len(grid.beats) == 1
    assert grid.total_beats == 1


def test_operation_specs_permission_levels() -> None:
    registry = build_audio_registry()

    detect_spec = registry.get_spec("audio.detect_beats")
    assert detect_spec.permission_level == PermissionLevel.IMMEDIATE
    assert AUDIO_PACKAGE_ID in detect_spec.required_packs

    norm_spec = registry.get_spec("audio.normalize_loudness")
    assert norm_spec.permission_level == PermissionLevel.CONFIRMATION
    assert AUDIO_PACKAGE_ID in norm_spec.required_packs

    duck_spec = registry.get_spec("audio.duck_music")
    assert duck_spec.permission_level == PermissionLevel.REVERSIBLE

    cut_spec = registry.get_spec("audio.beat_sync_cut")
    assert cut_spec.permission_level == PermissionLevel.REVERSIBLE


def test_detect_beats_reports_the_measured_tempo_not_a_constant() -> None:
    """The tempo is what the analyser measured (96 BPM from the stub), never 120."""
    from nexus_ai_agent.creative.packs.audio.models import clear_beat_analysis

    clear_beat_analysis()
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_detect_01",
        operation="audio.detect_beats",
        input={
            "audio_asset_id": "music_track_01",
            "sensitivity": 0.8,
            "min_bpm": 60,
            "max_bpm": 180,
        },
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["tempo_bpm"] == 96.0
    assert result.output["tempo_source"] == "measured"
    assert result.output["beat_period_us"] == 625_000
    assert result.output["confidence"] == 0.82
    assert result.output["bpm_window"] == [60.0, 180.0]
    grid = result.output["beat_grid"]
    assert len(grid["beats"]) == result.output["total_beats"]
    # Beat markers follow the measured period (625 ms), not a 500 ms constant.
    assert grid["beats"][1]["time_us"] - grid["beats"][0]["time_us"] == 625_000
    assert grid["beats"][0]["is_downbeat"] is True
    assert grid["beats"][1]["is_downbeat"] is False


def test_detect_beats_refuses_when_no_media_bytes_exist() -> None:
    """No readable bytes ⇒ an honest UNAVAILABLE, never an invented tempo."""
    from nexus_ai_agent.application.ports.audio_engine import SourcePrecheck
    from nexus_ai_agent.creative.packs.audio.models import clear_beat_analysis

    class _NoMedia:
        def precheck(self, request):  # noqa: ANN001, ANN202 - test double
            return SourcePrecheck(
                asset_id=request.asset_id, exists=False, readable=False, detail="missing"
            )

        def detect_beats(self, request):  # noqa: ANN001, ANN202
            raise AssertionError("detect_beats must not run without readable bytes")

    clear_beat_analysis()
    project, _ = _setup_audio_bus()
    bus = CommandBus(
        project,
        registry=build_audio_registry(),
        allow_experimental=True,
        audio_engine=_NoMedia(),
    )
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd_detect_nomedia",
            operation="audio.detect_beats",
            input={"audio_asset_id": "music_track_01"},
        )
    )
    assert result.output["execution_status"] == "UNAVAILABLE"
    assert result.output["tempo_bpm"] is None
    assert result.output["total_beats"] == 0


def test_beat_dependent_operations_require_an_engine() -> None:
    """Without a composed engine the operation refuses loudly instead of faking."""
    project, _ = _setup_audio_bus()
    bare = CommandBus(project, registry=build_audio_registry(), allow_experimental=True)
    with pytest.raises(CommandValidationError, match="native audio engine"):
        bare.dispatch(
            TypedCommand(
                command_id="cmd_no_engine",
                operation="audio.detect_beats",
                input={"audio_asset_id": "music_track_01"},
            )
        )


def test_detect_beats_rejects_missing_asset() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_detect_fail",
        operation="audio.detect_beats",
        input={"audio_asset_id": "non_existent_audio"},
    )
    with pytest.raises(CommandValidationError, match="unknown asset"):
        bus.dispatch(cmd)


def test_normalize_loudness_requires_level_c_confirmation() -> None:
    project, bus = _setup_audio_bus()

    # Unconfirmed command -> PermissionDeniedError at gate
    cmd_unconf = TypedCommand(
        command_id="cmd_norm_unconf",
        operation="audio.normalize_loudness",
        confirmed=False,
        input={
            "audio_asset_id": "music_track_01",
            # A custom target states all three numbers; the profile is the
            # authoritative form for the two locked targets.
            "profile": "custom",
            "target_lufs": -16.0,
            "true_peak_db": -1.0,
            "target_lra_lu": 11.0,
        },
    )
    with pytest.raises(PermissionDeniedError, match="level C"):
        bus.dispatch(cmd_unconf)

    # Confirmed command -> succeeds and creates derived AssetRecord
    cmd_conf = TypedCommand(
        command_id="cmd_norm_ok",
        operation="audio.normalize_loudness",
        confirmed=True,
        input={
            "audio_asset_id": "music_track_01",
            "target_lufs": -14.0,
            "true_peak_db": -1.0,
            "confirmed": True,
        },
    )
    result = bus.dispatch(cmd_conf)
    assert result.status == "applied"
    norm_asset_id = result.output["asset_id"]

    # Verify project has new asset with provenance
    derived_asset = next(a for a in bus.project.assets if a.asset_id == norm_asset_id)
    assert derived_asset.media_kind == "audio"
    assert derived_asset.parent_asset_ids == ("music_track_01",)
    assert derived_asset.provenance["target_lufs"] == -14.0
    assert derived_asset.provenance["true_peak_db"] == -1.0
    # The recorded hash is the *measured* artifact hash from the engine, and the
    # evidence carries the measured loudness — not a digest of the request.
    assert derived_asset.content_sha256 == "sha256:" + "2" * 64
    assert derived_asset.provenance["measured"]["integrated_lufs"] == -14.0
    assert derived_asset.provenance["artifact_verified"] is True
    assert result.output["artifact_verified"] is True
    assert result.output["derivation"] == "rendered_artifact"
    assert result.output["profile"] == "nagar_streaming_preset"


def test_normalize_loudness_profile_is_authoritative_over_loose_numbers() -> None:
    """Pinning ``ebu_r128_broadcast`` and asking for -14 LUFS is a contradiction."""
    project, bus = _setup_audio_bus()
    with pytest.raises(CommandValidationError, match="locks target_lufs"):
        bus.dispatch(
            TypedCommand(
                command_id="cmd_norm_conflict",
                operation="audio.normalize_loudness",
                confirmed=True,
                input={
                    "audio_asset_id": "music_track_01",
                    "profile": "ebu_r128_broadcast",
                    "target_lufs": -14.0,
                    "confirmed": True,
                },
            )
        )


def test_duck_music_execution() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_duck_01",
        operation="audio.duck_music",
        input={
            "music_asset_id": "music_track_01",
            "voice_asset_id": "voice_track_01",
            "duck_attenuation_db": -14.0,
        },
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    duck_id = result.output["asset_id"]

    derived_asset = next(a for a in bus.project.assets if a.asset_id == duck_id)
    assert derived_asset.parent_asset_ids == ("music_track_01", "voice_track_01")
    assert derived_asset.provenance["duck_attenuation_db"] == -14.0


def test_beat_sync_cut_execution() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_cut_01",
        operation="audio.beat_sync_cut",
        input={
            "audio_asset_id": "music_track_01",
            "clip_asset_ids": ["clip_01", "clip_02"],
            "beats_per_cut": 4,
            "timeline_track_id": "video_main",
        },
    )
    result = bus.dispatch(cmd)
    assert result.status == "applied"
    assert result.output["total_cuts"] == 2
    cuts = result.output["cuts"]
    assert cuts[0]["clip_asset_id"] == "clip_01"
    assert cuts[0]["start_us"] == 0
    assert cuts[0]["duration_us"] == 2_000_000  # 4 beats @ 120 bpm = 2.0s = 2_000_000 us
    assert cuts[1]["clip_asset_id"] == "clip_02"
    assert cuts[1]["start_us"] == 2_000_000


def test_command_bus_idempotency() -> None:
    project, bus = _setup_audio_bus()

    cmd = TypedCommand(
        command_id="cmd_idem_01",
        idempotency_key="key-audio-12345",
        operation="audio.detect_beats",
        input={"audio_asset_id": "music_track_01"},
    )
    res1 = bus.dispatch(cmd)
    res2 = bus.dispatch(cmd)
    assert res1.output == res2.output
    assert res1.transaction_id == res2.transaction_id
