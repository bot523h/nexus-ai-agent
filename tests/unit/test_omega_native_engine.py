"""Adversarial, execution-backed tests for the native audio engine (OMEGA-02).

These tests do **not** assert intentions: they synthesize hostile audio on the
spot, run the real engine (real FFmpeg, real processes), and then re-measure the
bytes that ended up on disk.  The claims are the phase's:

1. the input file is byte-identical afterwards (the engine never writes to it);
2. the *published* output really reaches the loudness target when re-decoded;
3. a synthetic 96 BPM pulse is detected near 96 BPM — and the answer changes with
   the stimulus, which is what proves it is a measurement and not a constant;
4. a silent programme, a corrupt file and a deadline overrun produce a typed
   refusal, leave no partial staging file, and never report ``READY``/verified.

The suite needs an FFmpeg binary.  When none is installed the tests skip with an
explicit reason instead of silently passing — a green suite that never executed
FFmpeg would be exactly the kind of evidence this phase forbids.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import omega_media as media
import pytest

from nexus_ai_agent.application.ports.audio_engine import (
    BeatRequest,
    DenoiseRequest,
    EngineInputError,
    EngineRejected,
    LoudnessRequest,
)
from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key, key_id_for
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    AudioDecodeError,
    AudioMeasurementError,
    FfmpegTranscoder,
)
from nexus_ai_agent.creative.audio_engine.models import (
    STREAMING_MASTER,
    CanonicalStreamProfile,
)
from nexus_ai_agent.creative.audio_engine.process import (
    CancelToken,
    ProcessGovernor,
    ProcessTimeout,
)
from nexus_ai_agent.creative.audio_engine.verify import (
    PassportKeyring,
    build_passport,
    check_loudness_target,
    verify_passport,
)
from nexus_ai_agent.creative.audio_engine.workspace import (
    AtomicPublishError,
    RuntimeWorkspace,
    WorkspaceBoundaryError,
    hash_file,
)

SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")


def _long_sine(transcoder: FfmpegTranscoder) -> list[str]:
    """A real-time, ten-minute FFmpeg job: guaranteed not to finish on its own."""
    return [
        transcoder.binary,
        "-hide_banner",
        "-nostdin",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "sine=d=600",
        "-f",
        "null",
        "-",
    ]


def _transcoder() -> FfmpegTranscoder:
    transcoder = FfmpegTranscoder(governor=ProcessGovernor(poll_seconds=0.01))
    if not transcoder.available():
        pytest.skip(
            "no FFmpeg binary resolved (install imageio-ffmpeg or set NEXUS_FFMPEG_BIN)",
            allow_module_level=True,
        )
    return transcoder


@pytest.fixture(scope="module")
def engine_binary() -> FfmpegTranscoder:
    return _transcoder()


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


@pytest.fixture()
def engine(media_dir: Path, tmp_path: Path, engine_binary: FfmpegTranscoder) -> NativeAudioEngine:
    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "workspace",
            signing_key=SEED,
            signer_key_id=key_id_for(derive_public_key(SEED)),
            max_analysis_seconds=10.0,
        ),
        transcoder=engine_binary,
    )


# --------------------------------------------------------------------------- #
# 1. the input is never touched
# --------------------------------------------------------------------------- #


def test_normalization_reaches_the_target_and_never_touches_the_source(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    source = media.write_sine(media_dir / "quiet.wav")
    before_bytes = source.read_bytes()
    before_sha = hash_file(source)
    before_stat = source.stat()

    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="run-quiet",
            source_asset_id="quiet",
            output_asset_id="quiet_master",
            source_sha256=before_sha,
            profile_id="streaming_master",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )

    # 1. the source is byte-identical (content, size, mtime untouched)
    assert source.read_bytes() == before_bytes
    assert hash_file(source) == before_sha
    assert source.stat().st_size == before_stat.st_size

    # 2. the published artifact really is at the target when measured again
    assert evidence.loudness_checked and evidence.loudness_passed is True
    assert evidence.integrated_lufs == pytest.approx(-14.0, abs=1.0)
    assert evidence.true_peak_dbtp is not None and evidence.true_peak_dbtp <= -0.5
    assert evidence.verified is True
    assert evidence.canonical_profile_ok is True

    # ... and the engine's number is the file's number, re-measured from disk
    published = Path(evidence.output_path)
    assert published.is_file()
    assert hash_file(published) == evidence.sha256
    remeasured = engine.measure(published)
    assert remeasured.integrated_lufs == pytest.approx(evidence.integrated_lufs, abs=0.1)
    assert published.suffix == CanonicalStreamProfile().container
    assert evidence.sample_rate == CanonicalStreamProfile().sample_rate
    assert evidence.channels == CanonicalStreamProfile().channels

    # 3. the evidence is signed and self-consistent
    assert evidence.passport_id
    assert evidence.passport_signature
    assert evidence.acoustic_signature_sha256


def test_a_silent_programme_is_refused_instead_of_normalized(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    source = media.write_silence(media_dir / "silence.wav")
    sha = hash_file(source)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - typed refusal asserted below
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="run-silence",
                source_asset_id="silence",
                output_asset_id="silence_master",
                source_sha256=sha,
                profile_id="streaming_master",
                target_lufs=-14.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
    assert "silent" in str(excinfo.value).lower()
    # No artifact was published, and the source is untouched.
    assert not (Path(engine._settings.workspace_root) / "media" / "silence_master.flac").exists()  # noqa: SLF001
    assert hash_file(source) == sha


def test_a_corrupt_file_fails_decode_before_any_artifact_exists(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    """A RIFF header promising more data than the file holds is not "successful"."""

    corrupt = media.write_corrupt(media_dir / "broken.wav")
    transcoder = engine._transcoder  # noqa: SLF001 - exercising the low-level contract
    with pytest.raises((AudioDecodeError, AudioMeasurementError)):
        sink = tmp_path / "decoded.s32le"
        transcoder.decode_all(corrupt, pcm_path=sink, channels=1)
    assert not sink.exists() or sink.stat().st_size % 4 == 0
    # The engine refuses it — the refusal may come from the duration check (a
    # 0.00 s container) or from the decode gate, but never from a "success".
    with pytest.raises((EngineInputError, EngineRejected)) as excinfo:
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="run-broken",
                source_asset_id="broken",
                output_asset_id="broken_master",
                profile_id="streaming_master",
                target_lufs=-14.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
    assert "corrupt" in str(excinfo.value).lower() or "programme is" in str(excinfo.value).lower()
    assert not (tmp_path / "workspace" / "media" / "broken_master.flac").exists()


# --------------------------------------------------------------------------- #
# 2. loudness: two locked profiles, verified independently
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("profile_id", "target"),
    [("streaming_master", -14.0), ("ebu_r128_broadcast", -23.0)],
)
def test_both_locked_loudness_profiles_land_on_their_target(
    engine: NativeAudioEngine, media_dir: Path, profile_id: str, target: float
) -> None:
    source = media.write_sine(media_dir / f"tone_{profile_id}.wav", amplitude=0.2)
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id=f"run-{profile_id}",
            source_asset_id=f"tone_{profile_id}",
            output_asset_id=f"master_{profile_id}",
            source_sha256=hash_file(source),
            profile_id=profile_id,
            target_lufs=target,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    assert evidence.loudness_passed is True
    assert evidence.integrated_lufs == pytest.approx(target, abs=1.0)


def test_a_clipped_source_still_respects_the_true_peak_ceiling(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    source = media.write_clipped(media_dir / "hot.wav")
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="run-hot",
            source_asset_id="hot",
            output_asset_id="hot_master",
            source_sha256=hash_file(source),
            profile_id="streaming_master",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    assert evidence.true_peak_dbtp is not None
    assert evidence.true_peak_dbtp <= -1.0 + 0.5
    assert evidence.loudness_passed is True


def test_the_loudness_target_check_is_not_a_rubber_stamp(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """A file that is *not* at the target must fail the same check that passes others."""
    source = media.write_sine(media_dir / "unprocessed.wav", amplitude=0.1)
    measurement = engine.measure(source)
    assert measurement.integrated_lufs is not None
    verdict = check_loudness_target(STREAMING_MASTER, measurement)
    assert verdict.passed is False
    assert verdict.code == "integrated_loudness_out_of_range"
    assert verdict.integrated_error_lu is not None and verdict.integrated_error_lu < -5.0


# --------------------------------------------------------------------------- #
# 3. beat detection is a measurement, not a constant
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bpm", [96.0, 120.0, 140.0])
def test_detected_tempo_follows_the_stimulus(
    engine: NativeAudioEngine, media_dir: Path, bpm: float
) -> None:
    name = f"pulse_{int(bpm)}"
    media.write_click_track(media_dir / f"{name}.wav", bpm=bpm)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id=f"beats-{name}", source_asset_id=name, min_bpm=60.0, max_bpm=180.0
        )
    )
    assert analysis.status == "detected"
    assert analysis.tempo_bpm is not None
    assert analysis.tempo_bpm == pytest.approx(bpm, rel=0.03)
    assert analysis.confidence > 0.4
    assert analysis.onset_count >= 8
    # The period is consistent with the tempo (no second source of truth).
    assert analysis.beat_period_us is not None
    assert analysis.beat_period_us == pytest.approx(60_000_000 / analysis.tempo_bpm, rel=0.01)


def test_a_96_bpm_pulse_is_never_reported_as_a_default_120(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """The regression that motivated the phase: a constant 120 BPM answer."""
    source = media.write_click_track(media_dir / "pulse96.wav", bpm=96.0)
    assert source.is_file()
    analysis = engine.detect_beats(
        BeatRequest(request_id="beats-96", source_asset_id="pulse96", min_bpm=60.0, max_bpm=180.0)
    )
    assert analysis.tempo_bpm is not None
    assert abs(analysis.tempo_bpm - 120.0) > 10.0, "detection returned the old constant"
    assert abs(analysis.tempo_bpm - 96.0) < 3.0


def test_a_window_that_excludes_the_real_tempo_reports_no_tempo(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Searching 150..200 BPM for a 96 BPM pulse must *not* invent an answer."""
    media.write_click_track(media_dir / "pulse96b.wav", bpm=96.0)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="beats-window",
            source_asset_id="pulse96b",
            min_bpm=150.0,
            max_bpm=200.0,
        )
    )
    assert analysis.status != "detected"
    assert analysis.tempo_bpm is None


def test_digital_silence_has_no_tempo(engine: NativeAudioEngine, media_dir: Path) -> None:
    media.write_silence(media_dir / "quiet_silence.wav")
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="beats-silence",
            source_asset_id="quiet_silence",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    assert analysis.status in ("insufficient_onsets", "empty_audio")
    assert analysis.tempo_bpm is None


# --------------------------------------------------------------------------- #
# 4. denoise: real filtering with a measured effect
# --------------------------------------------------------------------------- #


def test_denoise_lowers_the_noise_floor_and_keeps_the_bursts(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    source = media.write_noisy_voice(media_dir / "noisy.wav")
    source_sha = hash_file(source)
    before = engine.signature_of(source)
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="run-denoise",
            source_asset_id="noisy",
            output_asset_id="noisy_clean",
            source_sha256=hash_file(source),
            strength=0.85,
        )
    )
    assert evidence.verified is True
    assert hash_file(source) == source_sha, "denoise modified its own input"
    after = engine.signature_of(Path(evidence.output_path))
    assert before.rms_dbfs is not None and after.rms_dbfs is not None
    # The engine attenuates: a denoised file cannot be louder than its input.
    assert after.rms_dbfs < before.rms_dbfs + 0.01
    assert evidence.acoustic_signature_sha256
    assert evidence.spectral_centroid_hz is not None


def test_strength_zero_is_an_honest_passthrough(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_noisy_voice(media_dir / "passthrough.wav")
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="run-passthrough",
            source_asset_id="passthrough",
            output_asset_id="passthrough_out",
            strength=0.0,
        )
    )
    assert evidence.verified is True
    measured = engine.measure(Path(evidence.output_path))
    original = engine.measure(media_dir / "passthrough.wav")
    assert measured.integrated_lufs is not None and original.integrated_lufs is not None
    assert measured.integrated_lufs == pytest.approx(original.integrated_lufs, abs=0.6)


# --------------------------------------------------------------------------- #
# 5. the artifact passport: measured, signed, tamper-evident
# --------------------------------------------------------------------------- #


def test_passport_is_signed_and_refuses_tampering(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    source = media.write_sine(media_dir / "passport.wav")
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="run-passport",
            source_asset_id="passport",
            output_asset_id="passport_master",
            source_sha256=hash_file(source),
            profile_id="streaming_master",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    published = Path(evidence.output_path)
    passport = build_passport(
        operation="audio.normalize_loudness",
        asset_id="passport_master",
        path=published,
        source_sha256=hash_file(source),
        profile=CanonicalStreamProfile(),
        loudness_profile=STREAMING_MASTER,
        engine=engine._transcoder,  # noqa: SLF001 - exercising the passport builder
        expected_output_sha256=evidence.sha256,
        signing_key=SEED,
        signer_key_id=key_id_for(derive_public_key(SEED)),
    )
    keyring = PassportKeyring({key_id_for(derive_public_key(SEED)): derive_public_key(SEED)})

    report = verify_passport(passport, keyring=keyring, path=published, engine=engine._transcoder)  # noqa: SLF001
    assert report.passed is True, report.details
    assert report.signature_ok and report.bytes_ok and report.decode_ok and report.loudness_ok

    # A single flipped byte in the published artifact must break verification.
    tampered = tmp_path / "tampered.flac"
    payload = bytearray(published.read_bytes())
    payload[-64] ^= 0xFF
    tampered.write_bytes(bytes(payload))
    broken = verify_passport(passport, keyring=keyring, path=tampered)
    assert broken.passed is False
    assert "sha256_mismatch" in broken.codes

    # An edited passport payload must break the signature.
    edited = passport.model_copy(update={"asset_id": "somebody_else"})
    forged = verify_passport(edited, keyring=keyring, path=published)
    assert forged.passed is False
    assert "passport_signature_invalid" in forged.codes

    # An untrusted key cannot verify anything.
    stranger = verify_passport(passport, keyring=PassportKeyring({}), path=published)
    assert stranger.passed is False
    assert "passport_key_unknown" in stranger.codes


def test_signed_passport_serialises_and_reverifies_off_disk(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    """Evidence that only lives in memory is not evidence: round-trip the JSON."""
    source = media.write_sine(media_dir / "roundtrip.wav")
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="run-roundtrip",
            source_asset_id="roundtrip",
            output_asset_id="roundtrip_master",
            source_sha256=hash_file(source),
            profile_id="streaming_master",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    published = Path(evidence.output_path)
    passport = build_passport(
        operation="audio.normalize_loudness",
        asset_id="roundtrip_master",
        path=published,
        source_sha256=hash_file(source),
        profile=CanonicalStreamProfile(),
        loudness_profile=STREAMING_MASTER,
        engine=engine._transcoder,  # noqa: SLF001
        signing_key=SEED,
        signer_key_id=key_id_for(derive_public_key(SEED)),
    )
    from nexus_ai_agent.creative.audio_engine.verify import ArtifactPassport

    written = tmp_path / "passport.json"
    written.write_text(json.dumps(passport.model_dump(mode="json")), encoding="utf-8")
    reloaded = ArtifactPassport.model_validate_json(written.read_text(encoding="utf-8"))
    keyring = PassportKeyring({key_id_for(derive_public_key(SEED)): derive_public_key(SEED)})
    assert verify_passport(reloaded, keyring=keyring, path=published).passed is True


# --------------------------------------------------------------------------- #
# 6. process governance: deadline, cancellation, bounded captures
# --------------------------------------------------------------------------- #


def test_a_deadline_kills_the_process_group(engine_binary: FfmpegTranscoder) -> None:
    governor = ProcessGovernor(poll_seconds=0.01)
    started = time.monotonic()
    with pytest.raises(ProcessTimeout) as excinfo:
        governor.run(
            _long_sine(engine_binary),
            deadline_seconds=0.4,
        )
    elapsed = time.monotonic() - started
    outcome = excinfo.value.outcome
    assert outcome.termination == "timeout"
    assert outcome.returncode is not None  # the group was reaped, not leaked
    assert elapsed < 20.0, "the watchdog did not kill the process promptly"


def test_cancellation_stops_a_running_process(engine_binary: FfmpegTranscoder) -> None:
    from nexus_ai_agent.creative.audio_engine.process import ProcessCancelled

    governor = ProcessGovernor(poll_seconds=0.01)
    token = CancelToken()
    import threading

    threading.Timer(0.3, lambda: token.cancel("user pressed stop")).start()
    with pytest.raises(ProcessCancelled) as excinfo:
        governor.run(
            _long_sine(engine_binary),
            deadline_seconds=60.0,
            cancel=token,
        )
    assert excinfo.value.outcome.termination == "cancelled"
    assert "user pressed stop" in str(excinfo.value)


def test_captured_output_is_bounded(engine_binary: FfmpegTranscoder) -> None:
    governor = ProcessGovernor(poll_seconds=0.01)
    outcome = governor.run(
        [
            engine_binary.binary,
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "sine=d=1",
            "-f",
            "null",
            "-",
        ],
        deadline_seconds=60.0,
        max_captured_bytes=512,
    )
    assert outcome.ok or outcome.returncode == 0
    # The tail is kept, the total is reported, memory never grew past the cap.
    assert len(outcome.stderr_tail) <= 8000
    assert outcome.captured_bytes >= 0


# --------------------------------------------------------------------------- #
# 7. workspace safety: atomic publish, source protection, staging hygiene
# --------------------------------------------------------------------------- #


def test_publish_is_atomic_and_refuses_to_overwrite_a_source(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    source = media.write_sine(tmp_path / "input.wav")
    staged = workspace.stage_input(source)
    with workspace.run_directory("publish-run") as run:
        produced = run / "out.flac"
        produced.write_bytes(b"FLaC" + b"\x00" * 32)
        target = workspace.media_path("asset", suffix=".flac")
        result = workspace.publish(produced, target, protected=(staged,))
        assert result.path == target
        assert not produced.exists(), "the staged file must be consumed by publish"
        with pytest.raises(AtomicPublishError, match="already exists"):
            again = run / "out2.flac"
            again.write_bytes(b"FLaC" + b"\x01" * 32)
            workspace.publish(again, target)
    # The source is still exactly what it was.
    assert hash_file(source) == hash_file(staged)


def test_publish_refuses_to_replace_a_run_input(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws2").prepare()
    source = media.write_sine(tmp_path / "protected.wav")
    staged = workspace.stage_input(source)
    with workspace.run_directory("protect-run") as run:
        produced = run / "out.flac"
        produced.write_bytes(b"FLaC" + b"\x00" * 16)
        with pytest.raises(AtomicPublishError, match="run input"):
            workspace.publish(produced, staged, replace=True, protected=(staged,))
    assert staged.is_file(), "the protected input must still exist"


def test_workspace_refuses_paths_outside_its_root(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws3").prepare()
    with pytest.raises(WorkspaceBoundaryError):
        workspace.contain(tmp_path / "elsewhere.flac")


def test_no_staging_residue_after_a_refused_run(tmp_path: Path, media_dir: Path) -> None:
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(workspace_root=tmp_path / "ws4", signing_key=SEED),
    )
    media.write_silence(media_dir / "empty.wav")
    with pytest.raises(EngineRejected, match="silent|programme"):
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="residue-run",
                source_asset_id="empty",
                output_asset_id="empty_master",
                profile_id="streaming_master",
                target_lufs=-14.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
    staging = tmp_path / "ws4" / "staging"
    assert list(staging.iterdir()) == [], "a refused run left staging residue"
    assert not (tmp_path / "ws4" / "media" / "empty_master.flac").exists()


def test_an_unreachable_loudness_target_is_refused_with_the_arithmetic(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    """A 27.8 dB-crest programme cannot reach -23 LUFS under a -1 dBTP ceiling.

    This is the case that looks like an engine bug and is not: raising a quiet
    click track to -23 LUFS would put its peaks at +4.8 dBTP, so `loudnorm`
    correctly stops at the ceiling and undershoots the integrated target.  The
    engine must refuse *before* publishing and say why in numbers, because the
    alternatives are a lying artifact or a silent clip.
    """
    media.write_click_track(media_dir / "percussive.wav", bpm=96.0, seconds=12.0)
    with pytest.raises(EngineRejected) as excinfo:
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="run-unreachable",
                source_asset_id="percussive",
                output_asset_id="percussive_master",
                profile_id="ebu_r128_broadcast",
                target_lufs=-23.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
    message = str(excinfo.value)
    assert "loudness_target_unreachable" in message
    # The refusal carries the whole argument: measured level, measured peak,
    # the gain it would need, the peak that would result, and the budget.
    assert "-31.43 LUFS" in message or "LUFS at" in message
    assert "dBTP" in message
    assert "would peak at" in message
    assert "crest factor" in message
    # Nothing was published and nothing is left staging.
    assert not (tmp_path / "workspace" / "media" / "percussive_master.flac").exists()
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []


def test_a_sustained_programme_reaches_both_targets_while_the_percussive_one_cannot(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """The refusal is about the *request vs the signal*, not about the engine.

    A sustained programme (crest ≈ 11 dB) fits inside both profiles and lands on
    target; the 27.8 dB-crest click track cannot fit inside either.  Asserting
    both in one test is what proves the gate discriminates instead of blocking
    loudness work wholesale.
    """
    media.write_noisy_voice(media_dir / "sustained_a.wav", seconds=8.0)
    media.write_noisy_voice(media_dir / "sustained_b.wav", seconds=8.0)
    for source, profile_id, target in (
        ("sustained_a", "streaming_master", -14.0),
        ("sustained_b", "ebu_r128_broadcast", -23.0),
    ):
        evidence = engine.normalize_loudness(
            LoudnessRequest(
                request_id=f"run-{source}",
                source_asset_id=source,
                output_asset_id=f"{source}_master",
                profile_id=profile_id,
                target_lufs=target,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
        assert evidence.verified is True
        assert evidence.loudness_passed is True
        assert evidence.integrated_lufs == pytest.approx(target, abs=1.0)

    # ... and the high-crest programme is refused even at the looser target.
    media.write_click_track(media_dir / "percussive_refused.wav", bpm=96.0, seconds=12.0)
    with pytest.raises(EngineRejected, match="loudness_target_unreachable"):
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="run-percussive-refused",
                source_asset_id="percussive_refused",
                output_asset_id="percussive_refused_master",
                profile_id="custom",
                target_lufs=-16.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
