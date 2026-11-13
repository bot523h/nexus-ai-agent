"""K1..K4 reachable through the pure pack/CommandBus; real private FFmpeg bytes.

This module imports the pack and is a canonical PACK_TEST_TARGETS entry. No
changes to studio bus/DB are required; this is not AssetVersion persistence.
"""

from __future__ import annotations

import json
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import execution
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessCancelled
from nexus_ai_agent.creative.audio_engine.verify import verify_passport
from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace, hash_file
from nexus_ai_agent.creative.packs.audio.models import (
    STAGE_B_INPUT_MODELS,
    BarkMasterInput,
    RestoreVoiceInput,
    SpatializeInput,
    StageBAudioRequest,
    kernel_source_ids,
)
from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandExecutionError,
    CommandValidationError,
    Timeline,
    TypedCommand,
    new_project,
)

FS = 48000


def _write(path, pcm):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(FS)
        writer.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _events(t):
    return sum(
        a * np.exp(-0.5 * ((t - c) / w) ** 2)
        for c, w, a in [(0.7, 0.09, 1), (1.45, 0.12, 0.8), (2.3, 0.09, 0.9)]
    )


@pytest.fixture
def harness(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    t = np.arange(FS) / FS
    fade = np.sin(np.pi * t) ** 2
    voice = fade * 0.06 * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
    music = fade * (
        0.12 * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
        + 0.03 * np.sin(2 * np.pi * 160 * t)
        + 0.03 * np.sin(2 * np.pi * 7000 * t)
    )
    noise = np.random.default_rng(91).normal(size=FS) * 0.003
    long_t = np.arange(FS * 3) / FS
    arrays = dict(
        voice=voice,
        music=music,
        sfx=0.01 * fade * np.sin(2 * np.pi * 2500 * t),
        noise=noise,
        clipped=np.clip(voice + noise, -0.035, 0.035),
        sync=0.08 * _events(long_t - 0.02371) * np.cos(2 * np.pi * 1000 * long_t),
    )
    paths = {name: _write(media / f"{name}.wav", pcm) for name, pcm in arrays.items()}
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "work",
            signing_key=bytes(range(32)),
            signer_key_id="stageb-key",
            max_analysis_seconds=5,
        ),
    )
    project = new_project("stageb", "Stage B", Timeline(timeline_id="tl", duration_us=3000000))
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id=name,
                    media_kind="audio",
                    content_sha256=hash_file(path),
                    duration_us=round(arrays[name].size / FS * 1000000),
                )
                for name, path in paths.items()
            ]
        }
    )
    return SimpleNamespace(
        engine=engine,
        project=project,
        paths=paths,
        hashes={name: hash_file(path) for name, path in paths.items()},
        root=tmp_path,
    )


def _bus(harness, *, engine="native", project=None):
    return CommandBus(
        project or harness.project,
        registry=build_audio_registry(),
        allow_experimental=True,
        audio_engine=harness.engine if engine == "native" else engine,
    )


def _command(operation, payload, command_id="stageb-command"):
    return TypedCommand(command_id=command_id, operation=operation, input=payload)


def _request(harness, operation="audio.spatialize", **payload):
    model = STAGE_B_INPUT_MODELS[operation]
    typed = model.model_validate({"audio_asset_id": "voice", **payload})
    return StageBAudioRequest(
        request_id="stageb-request",
        operation=operation,
        output_asset_id="result",
        payload=typed,
        source_hashes={name: hash_file(harness.paths[name]) for name in kernel_source_ids(typed)},
    )


def _assert_no_output(harness):
    workspace = RuntimeWorkspace(harness.engine._settings.workspace_root).prepare()
    assert list(workspace.media_dir.iterdir()) == []
    assert workspace.leftovers() == []


def test_four_kernels_are_typed_registered_and_declared_by_the_data_only_manifest(harness):
    registry = build_audio_registry()
    assert set(STAGE_B_INPUT_MODELS) <= set(registry.list_operations())
    path = Path(execution.__file__).parents[1] / "packs" / "audio" / "pack.manifest.json"
    manifest = json.loads(path.read_text())
    assert set(STAGE_B_INPUT_MODELS) <= set(manifest["capabilities"])
    health = harness.engine.health()
    assert all(health.capabilities[name].available for name in STAGE_B_INPUT_MODELS)
    from nexus_ai_agent.creative.audio_engine import (
        binaural_decode,
        consistent_declip,
        gcc_phat,
        master_dialogue_music_sfx,
    )

    assert all(
        callable(fn)
        for fn in (binaural_decode, consistent_declip, gcc_phat, master_dialogue_music_sfx)
    )


@pytest.mark.parametrize(
    "operation,payload",
    [
        (
            "audio.spatialize",
            dict(
                audio_asset_id="voice",
                order=2,
                yaw=0.3,
                pitch=0.1,
                roll=-0.2,
                room_wet=0.1,
                tail_seconds=0.05,
            ),
        ),
        (
            "audio.restore_voice",
            dict(
                audio_asset_id="clipped",
                noise_asset_id="noise",
                clipping_threshold=0.035,
                bandwidth_hz=3000,
                dereverberate=True,
                wpe_taps=2,
            ),
        ),
        (
            "audio.sync_foley",
            dict(
                audio_asset_id="sync",
                visual_motion=_events(np.arange(90) / 30).tolist(),
                frame_rate=30,
                collisions=[dict(sample_index=1000, material="wood", contact_seconds=0)],
                foley_gain=0.02,
            ),
        ),
        (
            "audio.bark_master",
            dict(audio_asset_id="voice", music_asset_id="music", sfx_foley_asset_id="sfx"),
        ),
    ],
)
def test_command_bus_runs_each_kernel_then_verifies_private_bytes_before_publication(
    harness, monkeypatch, operation, payload
):
    reports = []

    def observe(passport, **kwargs):
        path = kwargs["path"]
        assert (
            "staging" in path.parts
            and not (harness.root / "work" / "media" / "result.flac").exists()
        )
        report = verify_passport(passport, **kwargs)
        assert report.passed and report.signature_ok and report.bytes_ok
        if operation == "audio.bark_master":
            assert passport.loudness_target.integrated_tolerance_lu == 0.1
            assert passport.loudness_target.true_peak_tolerance_db == 0
        reports.append(report)
        return report

    monkeypatch.setattr(execution, "verify_passport", observe)
    bus = _bus(harness)
    outcome = bus.dispatch(_command(operation, dict(payload, output_asset_id="result")))
    assert reports and outcome.output["artifact_verified"] and outcome.output["passport_signed"]
    result = next(a for a in bus.project.assets if a.asset_id == "result")
    path = Path(result.provenance["artifact_path"])
    assert result.content_sha256 == hash_file(path)
    assert result.parent_asset_ids == kernel_source_ids(
        STAGE_B_INPUT_MODELS[operation].model_validate(payload)
    )
    assert result.content_sha256 not in harness.hashes.values()
    assert {name: hash_file(path) for name, path in harness.paths.items()} == harness.hashes
    measurements = outcome.output["parameters"]["kernel_measurements"]
    if operation == "audio.spatialize":
        assert measurements["rotation_norm_error"] < 1e-12
        assert len(measurements["prime_delays_samples"]) == 8
    if operation == "audio.restore_voice":
        assert measurements["wpe_prediction_delay_ms"] >= 35
    if operation == "audio.sync_foley":
        assert abs(measurements["input_offset_ms"] - 23.71) < 0.5
        assert abs(measurements["residual_offset_ms"]) <= 0.5
        assert measurements["collision_samples"] == [1000]
    if operation == "audio.bark_master":
        assert abs(outcome.output["measured_integrated_lufs"] + 24) <= 0.1
        assert outcome.output["measured_true_peak_dbtp"] <= -1
    assert RuntimeWorkspace(harness.root / "work").prepare().leftovers() == []


def test_replay_rechecks_signed_bytes_and_does_not_fabricate_kernel_remeasurements(harness):
    request = _request(harness, room_wet=0)
    first = harness.engine.execute_stage_b(request)
    replay = harness.engine.execute_stage_b(request)
    assert replay.reused_existing and first.sha256 == replay.sha256
    assert (
        "kernel_measurements" in first.parameters and "kernel_measurements" not in replay.parameters
    )


def test_all_auxiliary_stem_hashes_bind_the_recipe_and_prevent_wrong_reuse(harness):
    request = _request(
        harness, "audio.bark_master", music_asset_id="music", sfx_foley_asset_id="sfx"
    )
    first = harness.engine.execute_stage_b(request)
    # A genuinely different bass/air component must produce a different master,
    # even though the primary dialogue, operation and destination are unchanged.
    t = np.arange(FS) / FS
    _write(harness.paths["music"], 0.1 * np.sin(2 * np.pi * 300 * t))
    changed = _request(
        harness, "audio.bark_master", music_asset_id="music", sfx_foley_asset_id="sfx"
    )
    second = harness.engine.execute_stage_b(changed)
    assert not second.reused_existing and first.sha256 != second.sha256


@pytest.mark.parametrize(
    "attack",
    ["no_engine", "old_engine", "unverified", "collision", "unknown", "wrong_kind", "no_hash"],
)
def test_pack_refuses_unavailable_unbound_or_unverified_work(harness, attack):
    payload = dict(audio_asset_id="voice", output_asset_id="result")
    engine = "native"
    project = harness.project
    if attack == "no_engine":
        engine = None
    if attack == "old_engine":
        engine = object()
    if attack == "unverified":
        engine = SimpleNamespace(execute_stage_b=lambda request: SimpleNamespace(verified=False))
    if attack == "collision":
        payload["output_asset_id"] = "music"
    if attack == "unknown":
        payload["audio_asset_id"] = "missing"
    if attack in ("wrong_kind", "no_hash"):
        assets = [
            a.model_copy(
                update={"media_kind": "video"} if attack == "wrong_kind" else {"content_sha256": ""}
            )
            if a.asset_id == "voice"
            else a
            for a in project.assets
        ]
        project = project.model_copy(update={"assets": assets})
    with pytest.raises((CommandValidationError, CommandExecutionError)):
        _bus(harness, engine=engine, project=project).dispatch(
            _command("audio.spatialize", payload)
        )
    _assert_no_output(harness)


def test_source_identity_mismatch_signing_policy_and_duration_bounds_refuse(harness):
    request = _request(harness)
    bad = request.model_copy(update={"source_hashes": {"voice": "sha256:" + "0" * 64}})
    with pytest.raises(execution.EngineInputError):
        harness.engine.execute_stage_b(bad)
    harness.engine._settings = replace(harness.engine._settings, max_analysis_seconds=0.1)
    with pytest.raises(execution.EngineInputError, match="whole-signal bound"):
        harness.engine.execute_stage_b(request)
    harness.engine._settings = replace(harness.engine._settings, signing_key=None)
    assert not harness.engine.health().capabilities["audio.spatialize"].available
    with pytest.raises(execution.EngineRejected, match="signing key/id"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)


def test_private_verifier_refusal_and_meter_absence_have_no_public_bytes(harness, monkeypatch):
    request = _request(harness, room_wet=0)
    original = execution.verify_passport
    monkeypatch.setattr(
        execution,
        "verify_passport",
        lambda *a, **kw: original(*a, **kw).model_copy(update={"passed": False}),
    )
    with pytest.raises(execution.EngineRejected, match="private passport refused"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)
    monkeypatch.setattr(execution, "verify_passport", original)
    monkeypatch.setattr(execution, "decode_for_analysis", lambda *a, **kw: (None, 0, 0))
    with pytest.raises(execution.EngineRejected, match="private passport refused"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)


def test_staged_tampering_and_false_decoder_duration_refuse(harness, monkeypatch):
    request = _request(harness, room_wet=0)
    stage = RuntimeWorkspace.stage_input

    def tamper(self, *a, **kw):
        path = stage(self, *a, **kw)
        path.write_bytes(b"tampered staged input")
        return path

    monkeypatch.setattr(RuntimeWorkspace, "stage_input", tamper)
    with pytest.raises(execution.EngineInputError, match="changed after precheck"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)
    # Staged input copies persist content-addressed; use a fresh workspace for
    # the independent decoder attack rather than trusting the corrupted copy.
    harness.engine._settings = replace(
        harness.engine._settings, workspace_root=harness.root / "other"
    )
    monkeypatch.setattr(RuntimeWorkspace, "stage_input", stage)
    decode = harness.engine._transcoder.decode_all

    def false_duration(*args, **kwargs):
        outcome, frames = decode(*args, **kwargs)
        return outcome, frames + FS * 6

    monkeypatch.setattr(harness.engine._transcoder, "decode_all", false_duration)
    with pytest.raises(execution.EngineInputError, match="actual decoded duration"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)


def test_cancellation_before_and_after_cpu_kernel_never_publishes(harness, monkeypatch):
    request = _request(harness, room_wet=0)
    token = CancelToken()
    token.cancel()
    with pytest.raises(ProcessCancelled):
        harness.engine.execute_stage_b(request, cancel=token)
    _assert_no_output(harness)
    token = CancelToken()
    process = harness.engine._process_stage_b

    def cancel_after(*args, **kw):
        result = process(*args, **kw)
        token.cancel()
        return result

    monkeypatch.setattr(harness.engine, "_process_stage_b", cancel_after)
    with pytest.raises(ProcessCancelled):
        harness.engine.execute_stage_b(request, cancel=token)
    _assert_no_output(harness)


def test_request_union_binds_operation_sources_and_rejects_unphysical_room(harness):
    request = _request(harness)
    with pytest.raises(ValueError):
        StageBAudioRequest.model_validate(request.model_dump() | {"source_hashes": {}})
    with pytest.raises(ValueError):
        StageBAudioRequest.model_validate(
            request.model_dump()
            | {"payload": RestoreVoiceInput(audio_asset_id="voice").model_dump()}
        )
    with pytest.raises(ValueError):
        SpatializeInput(audio_asset_id="voice", rt60_high=2)
    with pytest.raises(ValueError):
        SpatializeInput(audio_asset_id="voice", yaw=float("nan"))
    assert kernel_source_ids(
        BarkMasterInput(audio_asset_id="voice", music_asset_id="voice", sfx_foley_asset_id="voice")
    ) == ("voice",)


def test_default_asset_id_is_deterministic_without_mutating_parent_assets(harness):
    bus = _bus(harness)
    result = bus.dispatch(_command("audio.spatialize", dict(audio_asset_id="voice", room_wet=0)))
    assert result.output["asset_id"].startswith("stageb_")
    assert len(bus.project.assets) == len(harness.project.assets) + 1
    assert [a for a in bus.project.assets if a.asset_id == "voice"] == [
        a for a in harness.project.assets if a.asset_id == "voice"
    ]


def test_invalid_kernel_pcm_or_unphysical_room_refuse_before_encoding(harness, monkeypatch):
    request = _request(harness, volume_m3=10000, surface_area_m2=0.01)
    with pytest.raises(execution.EngineRejected, match="kernel refused"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)
    monkeypatch.setattr(
        harness.engine, "_process_stage_b", lambda *a, **kw: (np.full((2, 10), np.nan), {}, None)
    )
    with pytest.raises(execution.EngineRejected, match="nonfinite stereo PCM"):
        harness.engine.execute_stage_b(_request(harness, room_wet=0))
    _assert_no_output(harness)


def test_failed_sync_residual_and_missing_meter_evidence_refuse(harness, monkeypatch):
    sync = execution.k3.synchronize_audio_to_visual
    monkeypatch.setattr(
        execution.k3,
        "synchronize_audio_to_visual",
        lambda *a, **kw: replace(sync(*a, **kw), residual_offset_ms=0.51),
    )
    request = _request(
        harness,
        "audio.sync_foley",
        audio_asset_id="sync",
        visual_motion=_events(np.arange(90) / 30).tolist(),
        frame_rate=30,
    )
    with pytest.raises(execution.EngineRejected, match="residual exceeds"):
        harness.engine.execute_stage_b(request)
    _assert_no_output(harness)
    build = execution.build_passport
    monkeypatch.setattr(
        execution,
        "build_passport",
        lambda *a, **kw: build(*a, **kw).model_copy(update={"loudness": None}),
    )
    with pytest.raises(execution.EngineRejected, match="no loudness measurement"):
        harness.engine.execute_stage_b(_request(harness, room_wet=0))
    _assert_no_output(harness)


def test_cancellation_after_decode_stops_before_the_cpu_kernel(harness, monkeypatch):
    token = CancelToken()
    decode = harness.engine._transcoder.decode_all

    def cancel_decoding(*a, **kw):
        result = decode(*a, **kw)
        token.cancel()
        return result

    monkeypatch.setattr(harness.engine._transcoder, "decode_all", cancel_decoding)
    monkeypatch.setattr(
        harness.engine,
        "_process_stage_b",
        lambda *a, **kw: pytest.fail("cancelled kernel must not start"),
    )
    with pytest.raises(ProcessCancelled):
        harness.engine.execute_stage_b(_request(harness), cancel=token)
    _assert_no_output(harness)
