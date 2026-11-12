"""End-to-end ``CommandBus`` tests for the audio pack with a real engine.

This is the seam that matters: a typed command goes through authorization,
lifecycle gate, permission ladder, handler and atomic apply, the handler calls
the injected engine, and the resulting ``AssetRecord`` must be backed by bytes
that can be verified on disk.

The "before" behaviour is pinned as a negative: the audio pack used to answer
``tempo_bpm=120`` for any asset and to record a ``content_sha256`` computed from
a seed string.  The tests below fail if either comes back.
"""

from __future__ import annotations

from pathlib import Path

import omega_media as media
import pytest

from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key, key_id_for
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder, FfmpegUnavailableError
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.packs.audio.models import clear_beat_analysis
from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
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

SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


@pytest.fixture(scope="module")
def transcoder() -> FfmpegTranscoder:
    engine = FfmpegTranscoder()
    try:
        _ = engine.binary
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available", allow_module_level=True)
    return engine


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


@pytest.fixture()
def engine(media_dir: Path, tmp_path: Path, transcoder: FfmpegTranscoder) -> NativeAudioEngine:
    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "workspace",
            signing_key=SEED,
            signer_key_id=key_id_for(derive_public_key(SEED)),
            max_analysis_seconds=10.0,
        ),
        transcoder=transcoder,
    )


def _project(media_dir: Path, names: dict[str, str]) -> Project:
    assets = [
        AssetRecord(
            asset_id=asset_id,
            media_kind="audio",
            content_sha256=hash_file(media_dir / filename),
            duration_us=10_000_000,
        )
        for asset_id, filename in names.items()
    ]
    project = new_project("omega_proj", "OMEGA", Timeline(timeline_id="tl", duration_us=12_000_000))
    return project.model_copy(update={"assets": assets})


def _bus(project: Project, engine: NativeAudioEngine) -> CommandBus:
    return CommandBus(
        project,
        registry=build_audio_registry(),
        allow_experimental=True,
        audio_engine=engine,
    )


def _dispatch(
    bus: CommandBus, operation: str, payload: dict[str, object], **kwargs: object
) -> object:
    return bus.dispatch(
        TypedCommand(command_id=f"cmd_{operation}", operation=operation, input=payload, **kwargs)
    )


def test_normalize_loudness_records_a_measured_verified_artifact(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    source = media.write_sine(media_dir / "track.wav")
    source_sha = hash_file(source)
    bus = _bus(_project(media_dir, {"track": "track.wav"}), engine)

    result = _dispatch(
        bus,
        "audio.normalize_loudness",
        {
            "audio_asset_id": "track",
            "output_asset_id": "track_master",
            "profile": "streaming_master",
            "confirmed": True,
        },
        confirmed=True,
    )

    record = next(asset for asset in bus.project.assets if asset.asset_id == "track_master")
    # The recorded hash is the file's hash — an independent read agrees.
    published = Path(record.provenance["artifact_path"])
    assert published.is_file()
    assert record.content_sha256 == hash_file(published)
    assert record.content_sha256 != source_sha
    assert record.provenance["artifact_verified"] is True
    assert record.provenance["measured"]["integrated_lufs"] == pytest.approx(-14.0, abs=1.0)
    assert record.provenance["passport_signed"] is True
    assert result.output["measured_integrated_lufs"] == pytest.approx(-14.0, abs=1.0)
    assert result.diagnostics["authoritative"] is True
    # The input is untouched.
    assert hash_file(source) == source_sha


def test_remove_noise_and_beat_detection_produce_measured_evidence(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    from nexus_ai_agent.creative.packs.audio.models import last_beat_analysis

    clear_beat_analysis()
    media.write_click_track(media_dir / "pulse.wav", bpm=96.0)
    source = media.write_noisy_voice(media_dir / "voice.wav")
    source_sha = hash_file(source)
    bus = _bus(_project(media_dir, {"pulse": "pulse.wav", "voice": "voice.wav"}), engine)

    beats = _dispatch(
        bus,
        "audio.detect_beats",
        {"audio_asset_id": "pulse", "min_bpm": 60.0, "max_bpm": 180.0},
    )
    assert beats.output["tempo_source"] == "measured"
    assert beats.output["tempo_bpm"] == pytest.approx(96.0, rel=0.03)
    assert beats.output["tempo_bpm"] != 120.0

    # The measured grid is what a later beat-sync cut snaps to.
    cuts = _dispatch(
        bus,
        "audio.beat_sync_cut",
        {"audio_asset_id": "pulse", "clip_asset_ids": ["pulse"], "beats_per_cut": 4},
    )
    assert cuts.output["tempo_source"] == "measured"
    assert cuts.output["tempo_bpm"] == pytest.approx(96.0, rel=0.03)
    assert cuts.output["cuts"][0]["duration_us"] == pytest.approx(4 * 625_000, rel=0.02)
    assert last_beat_analysis("pulse") is not None

    denoised = _dispatch(
        bus,
        "audio.remove_noise",
        {"audio_asset_id": "voice", "output_asset_id": "voice_clean", "strength": 0.8},
    )
    record = next(asset for asset in bus.project.assets if asset.asset_id == "voice_clean")
    assert record.provenance["artifact_verified"] is True
    assert record.provenance["processor"] == "nagar.audio.studio.denoise.v2"
    assert record.content_sha256 == hash_file(Path(record.provenance["artifact_path"]))
    assert denoised.output["acoustic_signature_sha256"]
    assert hash_file(source) == source_sha


def test_remove_vocal_never_claims_ready_without_a_model(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_sine(media_dir / "mix.wav")
    bus = _bus(_project(media_dir, {"mix": "mix.wav"}), engine)
    result = _dispatch(bus, "audio.remove_vocal", {"audio_asset_id": "mix"})
    assert result.output["execution_status"] == "UNAVAILABLE"
    assert result.output["code"] == "REQUIRES_LOCAL_MODEL"
    assert result.output["stem_asset_ids"] == {}
    assert not any(asset.asset_id.startswith("mix_") for asset in bus.project.assets)


def test_state_only_operations_are_labelled_and_carry_no_artifact_claim(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_sine(media_dir / "vo.wav")
    media.write_sine(media_dir / "music.wav")
    bus = _bus(_project(media_dir, {"vo": "vo.wav", "music": "music.wav"}), engine)
    for operation, payload, out_id in (
        ("audio.duck_music", {"music_asset_id": "music", "voice_asset_id": "vo"}, "ducked"),
        ("audio.deess", {"audio_asset_id": "vo"}, "deessed"),
        ("audio.eq_voice", {"audio_asset_id": "vo", "preset": "broadcast"}, "eqved"),
        ("audio.time_stretch", {"audio_asset_id": "vo", "factor": 1.5}, "stretched"),
    ):
        payload = {**payload, "output_asset_id": out_id}
        result = _dispatch(bus, operation, payload)
        assert result.output["derivation"] == "state_only"
        assert result.output["artifact_verified"] is False
        record = next(asset for asset in bus.project.assets if asset.asset_id == out_id)
        assert record.provenance["artifact_verified"] is False
        assert record.provenance["derivation"] == "state_only"
        assert record.provenance["request_sha256"] == record.content_sha256


def test_wrong_output_asset_is_refused_before_any_process_starts(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_sine(media_dir / "dup.wav")
    bus = _bus(_project(media_dir, {"dup": "dup.wav"}), engine)
    with pytest.raises(CommandValidationError, match="already exists"):
        _dispatch(
            bus,
            "audio.normalize_loudness",
            {
                "audio_asset_id": "dup",
                "output_asset_id": "dup",
                "confirmed": True,
            },
            confirmed=True,
        )


def test_a_source_whose_bytes_drifted_from_the_registry_is_refused(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    source = media.write_sine(media_dir / "drift.wav")
    project = _project(media_dir, {"drift": "drift.wav"})
    bus = _bus(project, engine)
    # The bytes on disk are replaced after ingestion (a stale/foreign file).
    media.write_clipped(source, seconds=3.0)
    with pytest.raises((CommandExecutionError, CommandValidationError)) as excinfo:
        _dispatch(
            bus,
            "audio.normalize_loudness",
            {"audio_asset_id": "drift", "output_asset_id": "drift_master", "confirmed": True},
            confirmed=True,
        )
    assert "match the registered hash" in str(excinfo.value)


def test_replaying_the_same_command_reuses_the_verified_artifact(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_sine(media_dir / "replay.wav")
    project = _project(media_dir, {"replay": "replay.wav"})
    first_bus = _bus(project, engine)
    payload = {
        "audio_asset_id": "replay",
        "output_asset_id": "replay_master",
        "confirmed": True,
    }
    first = _dispatch(first_bus, "audio.normalize_loudness", payload, confirmed=True)
    second_bus = _bus(project, engine)  # a fresh bus, same workspace on disk
    second = _dispatch(second_bus, "audio.normalize_loudness", payload, confirmed=True)
    assert second.output["reused_existing"] is True
    assert first.output["content_sha256"] == second.output["content_sha256"]
    assert first.output["passport_id"] == second.output["passport_id"]


def test_a_silent_source_fails_the_command_and_writes_nothing(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    media.write_silence(media_dir / "hush.wav")
    bus = _bus(_project(media_dir, {"hush": "hush.wav"}), engine)
    with pytest.raises(CommandExecutionError):
        _dispatch(
            bus,
            "audio.normalize_loudness",
            {"audio_asset_id": "hush", "output_asset_id": "hush_master", "confirmed": True},
            confirmed=True,
        )
    assert not (tmp_path / "workspace" / "media" / "hush_master.flac").exists()
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []
