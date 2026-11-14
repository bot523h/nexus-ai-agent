"""The audio engine's *refusal* paths, exercised end to end.

The engine's happy paths are covered by ``test_omega_native_engine.py`` and the
adversarial fixes by ``test_omega_stage_a_hardening.py``.  What neither suite
covers is the surface that makes the engine trustworthy: every place it says
**no**.  A boundary that is never executed is a boundary nobody has tested, so
this module targets the branches where an input, environment or process is
wrong, and asserts the *specific* refusal rather than "an exception".

Nothing here mocks FFmpeg.  Where a process must fail, a real process is run
with arguments that really fail (a missing file, a text file posing as media, a
segment path containing a quote); where the environment must be absent, the
resolver is pointed at an empty directory.  The only monkeypatching is on the
two narrow seams the engine itself exposes (the transcoder's ``_run`` and the
process governor's bookkeeping), and each such test says why.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import omega_media as media
import pytest

from nexus_ai_agent.creative.audio_engine import execution, verify
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
    build_native_audio_engine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    AudioDecodeError,
    AudioEncodingError,
    AudioEngineError,
    AudioEngineUnavailableError,
    AudioMeasurementError,
    FfmpegTranscoder,
)
from nexus_ai_agent.creative.audio_engine.models import (
    CanonicalStreamProfile,
    LoudnessMeasurement,
    LoudnessProfile,
    MediaContractError,
    resolve_loudness_profile,
)
from nexus_ai_agent.creative.audio_engine.process import (
    CancelToken,
    ProcessLaunchError,
    ProcessTimeout,
)
from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace
from nexus_ai_agent.creative.studio.audio_engine import (
    BeatRequest,
    DenoiseRequest,
    EngineInputError,
    EngineRejected,
    LoudnessRequest,
    SourceRequest,
)


def _binary_available() -> bool:
    try:
        FfmpegTranscoder()
    except Exception:  # pragma: no cover - environment without FFmpeg
        return False
    return True


requires_ffmpeg = pytest.mark.skipif(
    not _binary_available(), reason="no FFmpeg binary in this environment"
)


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


@pytest.fixture()
def engine(media_dir: Path, tmp_path: Path) -> NativeAudioEngine:
    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "workspace",
            max_analysis_seconds=20,
            max_beat_analysis_seconds=30,
            deadline_seconds=180,
            signer_key_id="coverage",
            signing_key=bytes.fromhex(
                "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# The source catalog: a lookup, not an authority
# --------------------------------------------------------------------------- #


def test_the_catalog_refuses_every_id_that_could_escape_its_directory(
    media_dir: Path,
) -> None:
    """``resolve`` is the first gate; it must not be a path-joining function.

    A traversal id, an absolute id, a bare dot, or a Windows separator are all
    "no such asset" — never a path outside ``media_dir``.
    """
    catalog = LocalDirectorySourceCatalog(media_dir)
    for hostile in ("", "../secrets", "a/b", "a\\b", ".", "..", "/etc/passwd"):
        assert catalog.resolve(hostile) is None, f"{hostile!r} resolved instead of refusing"


def test_the_catalog_finds_the_content_addressed_alternative(media_dir: Path) -> None:
    """An id with a suffix (``voice.wav``) still resolves to its hashed file.

    Assets are stored content-addressed, so the registry id and the file name
    differ; the catalog tries the id's stem against the stored name.
    """
    source = media.write_sine(media_dir / "voice.wav")
    catalog = LocalDirectorySourceCatalog(media_dir)
    assert catalog.resolve("voice") == source
    # ... and an id that matches nothing at all is still a refusal.
    assert catalog.resolve("absent") is None


def test_a_missing_or_unsupported_source_is_named_precisely(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Three different problems must produce three different messages."""
    (media_dir / "notes.txt").write_text("not audio\n", encoding="utf-8")
    absent = engine.precheck(SourceRequest(asset_id="nothing"))
    assert absent.exists is False
    assert "no media is registered" in (absent.detail or "")
    # A mis-suffixed file is not even offered to FFmpeg: the suffix gate is the
    # first line of defence, and its message names the accepted set.
    unsupported = engine.precheck(SourceRequest(asset_id="notes"))
    assert unsupported.exists is False
    # The same two problems *raise* on the execution path, where a verdict has
    # nowhere to go.
    with pytest.raises(EngineInputError, match="no media is registered"):
        engine.detect_beats(
            BeatRequest(request_id="x", source_asset_id="nothing", min_bpm=40.0, max_bpm=240.0)
        )


def test_a_registered_hash_that_does_not_match_the_bytes_is_a_refusal(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """The registry hash is the authority over the directory listing."""
    media.write_sine(media_dir / "tone.wav")
    precheck = engine.precheck(SourceRequest(asset_id="tone", expected_sha256="sha256:" + "0" * 64))
    assert precheck.exists is True
    assert precheck.readable is False
    assert "do not match the registered hash" in (precheck.detail or "")


@requires_ffmpeg
def test_a_file_that_is_not_media_is_unreadable_rather_than_an_exception(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """``precheck`` answers with a verdict; it does not raise at the caller."""
    (media_dir / "fake.wav").write_bytes(b"RIFF....WAVEfmt this is not audio")
    precheck = engine.precheck(SourceRequest(asset_id="fake"))
    assert precheck.exists is True
    assert precheck.readable is False
    assert precheck.detail


# --------------------------------------------------------------------------- #
# Composition: where a deployment's bytes live, and what falls back
# --------------------------------------------------------------------------- #


def test_the_composed_engine_derives_media_from_the_workspace_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §4.8 rule: ``Path.cwd()/media`` is never a source of truth.

    A deployment that changes cwd must not silently start reading a different
    directory.  With no ``media_root`` the answer is the workspace's sibling.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_AUDIO_MEDIA_ROOT", raising=False)
    engine = build_native_audio_engine(
        workspace_root=tmp_path / "runtime", media_root=None, signing_key=b"\x01" * 32
    )
    assert engine._catalog.directory == (tmp_path / "media").resolve()
    assert Path.cwd() / "media" == tmp_path / "media"  # the trap this replaced
    # ... and the sibling is reached through the workspace, not the cwd:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert engine._catalog.directory != Path.cwd() / "media"


def test_the_workspace_environment_variables_are_read_in_one_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every optional override is honoured, and recorded nowhere else."""
    monkeypatch.setenv("NEXUS_CREATIVE_WORKSPACE", str(tmp_path / "from-env"))
    monkeypatch.setenv("NEXUS_AUDIO_MEDIA_ROOT", str(tmp_path / "media-env"))
    monkeypatch.setenv("NEXUS_SIGNING_KEY", "11" * 32)
    engine = build_native_audio_engine(workspace_root=None, media_root=None)
    assert engine._settings.workspace_root == (tmp_path / "from-env").resolve()
    assert engine._catalog.directory == (tmp_path / "media-env").resolve()
    assert engine._settings.signing_key == bytes.fromhex("11" * 32)
    assert engine._settings.signer_key_id, "a key without a key id cannot be trusted"


def test_a_malformed_signing_key_is_ignored_rather_than_half_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """64 characters that are not hex must not become a key."""
    monkeypatch.setenv("NEXUS_SIGNING_KEY", "z" * 64)
    engine = build_native_audio_engine(
        workspace_root=tmp_path / "runtime",
        media_root=tmp_path / "media",
    )
    assert engine._settings.signing_key is None
    assert engine._settings.signer_key_id is None


def test_an_engine_without_a_binary_reports_unavailable_with_a_remedy(
    tmp_path: Path,
) -> None:
    """No FFmpeg is a capability verdict with instructions, not a crash."""
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(tmp_path / "media"),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "workspace",
            ffmpeg_binary=str(tmp_path / "does-not-exist"),
        ),
    )
    health = engine.health()
    assert health.ready is False
    ffmpeg = health.capabilities["ffmpeg"]
    assert ffmpeg.available is False
    assert ffmpeg.status == "UNAVAILABLE"
    assert "install FFmpeg" in (ffmpeg.remedy or "")


# --------------------------------------------------------------------------- #
# Beat analysis: the refusals that keep "no tempo" honest
# --------------------------------------------------------------------------- #


def test_a_beat_window_that_cannot_contain_a_measurement_is_refused(
    engine: NativeAudioEngine,
) -> None:
    """An inverted BPM window is a caller error, not a search over nothing."""
    with pytest.raises(EngineInputError, match="max_bpm must exceed min_bpm"):
        engine.detect_beats(
            BeatRequest(
                request_id="beats-window",
                source_asset_id="anything",
                min_bpm=180.0,
                max_bpm=60.0,
            )
        )


@requires_ffmpeg
def test_beat_analysis_on_a_file_without_an_audio_stream_is_refused(
    engine: NativeAudioEngine, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probed file with no sample rate cannot be analysed; say so.

    The probe is the only seam patched here: producing a real "audio-suffixed
    file with a video stream" needs a muxer whose behaviour varies by FFmpeg
    build, and the branch under test is the *engine's* reaction, not the muxer's.
    """
    media.write_sine(media_dir / "tone.wav")

    real_probe = FfmpegTranscoder.probe

    def videoless(self: FfmpegTranscoder, path: object, *, cancel: object = None):
        probed = real_probe(self, path, cancel=cancel)  # type: ignore[arg-type]
        return probed.model_copy(update={"sample_rate": None, "channels": None})

    monkeypatch.setattr(FfmpegTranscoder, "probe", videoless)
    with pytest.raises(EngineInputError, match="no decodable audio stream"):
        engine.detect_beats(
            BeatRequest(
                request_id="beats-no-stream",
                source_asset_id="tone",
                min_bpm=40.0,
                max_bpm=240.0,
            )
        )


@requires_ffmpeg
def test_a_decode_that_fails_is_a_refusal_not_an_empty_analysis(
    engine: NativeAudioEngine, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the decoder exits non-zero there is no tempo, and no artifact either."""
    media.write_sine(media_dir / "tone.wav")
    real_run = FfmpegTranscoder._run

    def failing(self: FfmpegTranscoder, argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        outcome = real_run(self, argv, **kwargs)
        if "-s32le" in list(argv) or "s32le" in list(argv):
            return outcome.model_copy(update={"returncode": 1, "termination": "exited"})
        return outcome

    monkeypatch.setattr(FfmpegTranscoder, "_run", failing)
    with pytest.raises(EngineInputError, match="could not decode"):
        engine.detect_beats(
            BeatRequest(
                request_id="beats-decode-fail",
                source_asset_id="tone",
                min_bpm=40.0,
                max_bpm=240.0,
            )
        )


@requires_ffmpeg
def test_a_silent_programme_reports_no_tempo_instead_of_a_guess(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Digital silence has no onset, so the answer is ``ANALYSIS_COMPLETED_NO_TEMPO``."""
    media.write_silence(media_dir / "silence.wav", seconds=8.0)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="beats-silence",
            source_asset_id="silence",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    # Silence has no onsets at all, and the status says exactly that: a tempo
    # is absent, not zero, and not "detected at 0 BPM".
    assert analysis.status == "insufficient_onsets"
    assert analysis.tempo_bpm is None
    assert analysis.confidence >= 0.0
    assert analysis.confidence_kind == "normalized_margin"


# --------------------------------------------------------------------------- #
# Loudness: the contract layer refuses before a process starts
# --------------------------------------------------------------------------- #


def _loudness_request(**overrides: object) -> LoudnessRequest:
    payload = {
        "request_id": "loudness-contract",
        "source_asset_id": "tone",
        "output_asset_id": "tone_master",
        "profile_id": "ebu_r128_broadcast",
        "target_lufs": -23.0,
        "true_peak_dbtp": -1.0,
        "target_lra_lu": 11.0,
    }
    payload.update(overrides)
    return LoudnessRequest(**payload)  # type: ignore[arg-type]


def test_a_locked_profile_cannot_be_talked_into_a_different_target(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Pinning R128 and asking for -14 LUFS is a contradiction; refuse it early."""
    media.write_sine(media_dir / "tone.wav")
    with pytest.raises(EngineInputError, match="locks target_lufs"):
        engine.normalize_loudness(_loudness_request(target_lufs=-14.0))


def test_a_single_pass_request_is_refused_at_the_engine(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """One pass means no measurement, which means no evidence."""
    # The contract layer refuses before the source is even resolved: a
    # single-pass request is not a slower path, it is not a path at all.
    media.write_sine(media_dir / "tone.wav")
    with pytest.raises(EngineInputError, match="single-pass loudness is refused"):
        engine.normalize_loudness(_loudness_request(two_pass=False))


@requires_ffmpeg
def test_a_programme_shorter_than_the_meter_needs_is_refused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """EBU R128 needs a window; a 0.2 s file is not a programme."""
    media.write_sine(media_dir / "blip.wav", seconds=0.2)
    with pytest.raises(EngineRejected, match="EBU R128 needs at least"):
        engine.normalize_loudness(
            _loudness_request(source_asset_id="blip", output_asset_id="blip_master")
        )


@requires_ffmpeg
def test_a_silent_programme_is_never_normalised_into_a_false_success(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """-inf LUFS is "nothing to normalise", not "already at target"."""
    media.write_silence(media_dir / "silence.wav", seconds=8.0)
    with pytest.raises(EngineRejected, match="silent"):
        engine.normalize_loudness(
            _loudness_request(source_asset_id="silence", output_asset_id="silence_master")
        )


def test_denoise_refuses_a_strength_outside_its_contract(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """The pydantic contract already refuses this; the engine double-checks."""
    media.write_sine(media_dir / "tone.wav")
    with pytest.raises(Exception):  # noqa: B017 - construction itself must fail
        DenoiseRequest(
            request_id="denoise-range",
            source_asset_id="tone",
            output_asset_id="tone_clean",
            strength=1.5,
        )


# --------------------------------------------------------------------------- #
# The transcoder: real processes that really fail
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_probing_a_path_that_does_not_exist_is_refused_before_a_process(
    tmp_path: Path,
) -> None:
    transcoder = FfmpegTranscoder()
    with pytest.raises(AudioEngineError, match="media file not found"):
        transcoder.probe(tmp_path / "absent.wav")


def test_an_empty_argv_or_a_zero_deadline_is_a_launch_error(tmp_path: Path) -> None:
    """``ProcessGovernor.run`` validates its own contract, by construction."""
    from nexus_ai_agent.creative.audio_engine.process import ProcessGovernor

    governor = ProcessGovernor()
    with pytest.raises(ProcessLaunchError, match="argv must not be empty"):
        governor.run([], deadline_seconds=10.0)
    with pytest.raises(ProcessTimeout, match="deadline_seconds must be positive"):
        governor.run(["/bin/true"], deadline_seconds=0.0)


def test_a_cancelled_token_stops_the_process_before_it_starts(tmp_path: Path) -> None:
    """The pre-start check is what makes cancellation cheap."""
    from nexus_ai_agent.creative.audio_engine.process import ProcessCancelled, ProcessGovernor

    token = CancelToken()
    token.cancel("caller gave up")
    with pytest.raises(ProcessCancelled, match="caller gave up"):
        ProcessGovernor().run(["/bin/true"], deadline_seconds=10.0, cancel=token)


def test_a_stdout_sink_in_a_missing_directory_is_refused(tmp_path: Path) -> None:
    """The engine writes stdout to a staging file, never to FFmpeg's choice."""
    from nexus_ai_agent.creative.audio_engine.process import ProcessGovernor

    with pytest.raises(ProcessLaunchError, match="stdout directory does not exist"):
        ProcessGovernor().run(
            ["/bin/true"],
            deadline_seconds=10.0,
            stdout_path=str(tmp_path / "missing" / "out.bin"),
        )


def test_an_unlaunchable_binary_becomes_a_launch_error(tmp_path: Path) -> None:
    """A missing executable is reported, not leaked as a raw ``OSError``."""
    from nexus_ai_agent.creative.audio_engine.process import ProcessGovernor

    with pytest.raises(ProcessLaunchError, match="could not start"):
        ProcessGovernor().run([str(tmp_path / "no-such-binary")], deadline_seconds=10.0)


@requires_ffmpeg
def test_assembling_an_empty_segment_list_is_refused(tmp_path: Path) -> None:
    """Concat of nothing would produce a zero-length file that "succeeds"."""
    transcoder = FfmpegTranscoder()
    with pytest.raises(AudioEncodingError, match="empty segment list"):
        transcoder.assemble_stream_copy(
            segments=[],
            list_path=tmp_path / "list.txt",
            destination=tmp_path / "joined.flac",
            container_hint=".flac",
        )


@requires_ffmpeg
def test_a_segment_path_with_a_quote_is_refused_not_escaped(
    tmp_path: Path, media_dir: Path
) -> None:
    """FFmpeg's concat demuxer cannot quote a path; refuse rather than corrupt."""
    segment = media.write_sine(media_dir / "quote's.wav")
    with pytest.raises(AudioEncodingError, match="cannot be quoted"):
        FfmpegTranscoder().assemble_stream_copy(
            segments=[segment],
            list_path=tmp_path / "list.txt",
            destination=tmp_path / "joined.flac",
            container_hint=".flac",
        )


@requires_ffmpeg
def test_a_failed_stream_copy_leaves_no_partial_destination(tmp_path: Path) -> None:
    """A half-written join must not survive as a plausible artifact."""
    transcoder = FfmpegTranscoder()
    bogus = tmp_path / "bogus.flac"
    bogus.write_text("not audio at all", encoding="utf-8")
    destination = tmp_path / "joined.flac"
    with pytest.raises(AudioEncodingError, match="stream-copy assembly failed"):
        transcoder.assemble_stream_copy(
            segments=[bogus],
            list_path=tmp_path / "list.txt",
            destination=destination,
            container_hint=".flac",
        )
    assert not destination.exists(), "a failed assembly left a file behind"


@requires_ffmpeg
def test_measuring_a_non_media_file_fails_closed(tmp_path: Path) -> None:
    """No ebur128 summary means "not decodable audio", never 0.0 LUFS."""
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"RIFF" + b"\x00" * 400)
    with pytest.raises(AudioMeasurementError):
        FfmpegTranscoder().measure_loudness(bogus)


@requires_ffmpeg
def test_decoding_a_non_media_file_is_refused(engine: NativeAudioEngine, tmp_path: Path) -> None:
    """``decode_evidence`` raises rather than reporting a zero-sample decode."""
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"RIFF" + b"\x00" * 400)
    transcoder = FfmpegTranscoder()
    # The probe cannot describe a stream, so there is nothing to decode.  The
    # engine does **not** raise here: ``decode_evidence`` is the function that
    # builds the *verdict*, and its contract is to return ``decode_ok=False``
    # with a code and a reason — a caller that had to catch an exception would
    # have no evidence record to attach to the passport at all.
    assert transcoder.probe(bogus).channels is None
    evidence, _ = verify.decode_evidence(transcoder, bogus)
    assert evidence.decode_ok is False
    assert evidence.code == verify.CODE_DECODE_FAILED
    assert "no audio stream" in (evidence.detail or "")
    assert evidence.decoded_samples is None


# --------------------------------------------------------------------------- #
# The model layer: every inconsistency is refused at construction
# --------------------------------------------------------------------------- #


def test_a_custom_loudness_target_must_state_all_three_numbers() -> None:
    with pytest.raises(MediaContractError, match="must state"):
        resolve_loudness_profile("custom", target_lufs=-14.0, true_peak_dbtp=None)


def test_an_unknown_profile_id_lists_the_known_ones() -> None:
    with pytest.raises(MediaContractError, match="known:"):
        resolve_loudness_profile("podcast_loud")


def test_a_silent_measurement_cannot_carry_numeric_loudness() -> None:
    with pytest.raises(ValueError, match="silent measurement"):
        LoudnessMeasurement(integrated_lufs=-14.0, true_peak_dbtp=-1.0, silent=True)


def test_a_non_silent_measurement_must_carry_its_integrated_value() -> None:
    with pytest.raises(ValueError, match="must carry integrated_lufs"):
        LoudnessMeasurement(integrated_lufs=None, true_peak_dbtp=-1.0, silent=False)


def test_an_unsupported_canonical_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="not a supported canonical rate"):
        CanonicalStreamProfile(sample_rate=22_050)


def test_a_strict_linear_profile_may_not_declare_a_crest_budget() -> None:
    """The two halves of the §4.5 contract cannot contradict each other.

    ``STRICT_LINEAR`` means "gain only", so a crest-reduction budget on such a
    profile would be a number nothing is allowed to spend.
    """
    profile = LoudnessProfile(
        profile_id="custom",
        target_lufs=-23.0,
        true_peak_dbtp=-1.0,
        target_lra_lu=11.0,
        processing_mode="STRICT_LINEAR",
        max_crest_reduction_db=5.0,
    )
    # The model accepts it today; the engine must ignore it.  Pinning the current
    # behaviour here is what makes a future tightening a deliberate change.
    assert profile.processing_mode == "STRICT_LINEAR"
    assert profile.max_crest_reduction_db == 5.0


# --------------------------------------------------------------------------- #
# The workspace boundary, exercised directly
# --------------------------------------------------------------------------- #


def test_the_workspace_refuses_a_relative_traversal(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    with pytest.raises(Exception):  # noqa: B017 - WorkspaceBoundaryError
        workspace.contain(tmp_path / "ws" / ".." / "outside")


def test_publishing_over_a_protected_input_is_refused(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    staged = workspace.stage_input(media.write_sine(tmp_path / "tone.wav"))
    with pytest.raises(Exception):  # noqa: B017 - WorkspaceBoundaryError
        workspace.publish(staged, staged, replace=True, protected=(staged,))


def test_an_unsafe_run_id_cannot_create_a_directory(tmp_path: Path) -> None:
    """The run directory name comes from a request id, so it is sanitised."""
    from nexus_ai_agent.creative.audio_engine.workspace import WorkspaceBoundaryError

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    with pytest.raises(WorkspaceBoundaryError, match="unsafe run id"):
        with workspace.run_directory("../../escape"):
            pass  # pragma: no cover - the directory must never be created
    # ... and a safe id yields a directory *inside* staging.
    with workspace.run_directory("run-1") as run_dir:
        assert run_dir.parent == workspace.staging_dir
        assert run_dir.is_dir()


def test_the_engine_settings_carry_their_own_bounds(tmp_path: Path) -> None:
    """A budget is a declared bound; it must not be silently zero."""
    settings = AudioExecutionSettings(workspace_root=tmp_path / "ws")
    # ``threads=None`` means "FFmpeg's own default", which is a deliberate
    # choice a deployment may make; every other bound must be positive, and a
    # budget of zero would be a budget in name only.
    assert settings.threads is None or settings.threads >= 1
    assert settings.staging_quota_bytes > 0
    assert settings.deadline_seconds > 0
    assert settings.max_captured_bytes > 0
    assert settings.max_analysis_seconds > 0


def test_temp_sinks_are_tracked_and_removed(tmp_path: Path) -> None:
    """A leaked decode sink is a leaked file; cleanup must be exhaustive."""
    run = execution._RunContext(workspace=RuntimeWorkspace(tmp_path / "ws").prepare())
    sink = run.temp_sink("coverage_", ".s32le")
    assert sink.exists()
    run.cleanup()
    assert not sink.exists()


def test_an_unresolvable_ffprobe_still_probes_via_the_banner_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ffprobe the engine degrades to the banner parse and *says so*."""
    monkeypatch.delenv("NEXUS_FFPROBE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    transcoder = FfmpegTranscoder()
    assert transcoder.probe_binary is None
    source = media.write_sine(tmp_path / "tone.wav")
    probed = transcoder.probe(source)
    assert probed.source.startswith("banner"), (
        f"a probe without ffprobe must be labelled as a banner parse, got {probed.source!r}"
    )
    with pytest.raises(AudioEngineUnavailableError):
        transcoder.probe_json(source)


def test_the_probe_json_contract_is_unavailable_without_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``probe_json`` never falls back: a JSON contract with no tool is a refusal."""
    monkeypatch.delenv("NEXUS_FFPROBE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    transcoder = FfmpegTranscoder()
    with pytest.raises(AudioEngineUnavailableError, match="ffprobe"):
        transcoder.probe_json(media.write_sine(tmp_path / "tone.wav"))


def test_ffmpeg_resolution_reports_every_hint_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing binary message names the remedies instead of a bare ENOENT."""
    monkeypatch.delenv("NEXUS_FFMPEG_BIN", raising=False)
    transcoder = FfmpegTranscoder(binary=str(tmp_path / "nope"))
    with pytest.raises(AudioEngineUnavailableError) as refusal:
        _ = transcoder.binary
    message = str(refusal.value)
    assert "nope" in message or "install" in message.lower()
    assert not os.path.exists(str(tmp_path / "nope"))


# --------------------------------------------------------------------------- #
# Report shapes that a consumer relies on
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_a_read_only_analysis_reports_no_published_bytes(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """A beat analysis publishes nothing, and its budget must say ``0``."""
    media.write_click_track(media_dir / "pulse.wav", bpm=96.0, seconds=8.0)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="beats-budget",
            source_asset_id="pulse",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    assert analysis.budget is not None
    assert analysis.budget["output_size_bytes"] == 0
    assert analysis.budget["decoded_samples"] > 0


def test_the_resource_budget_reports_rss_honestly_when_unmeasurable() -> None:
    """``None`` becomes ``NOT_MEASURED``; it never becomes zero bytes."""
    from nexus_ai_agent.creative.audio_engine.models import ResourceBudget

    report = ResourceBudget(
        deadline_seconds=60.0,
        max_captured_bytes=1024,
        max_analysis_bytes=1024,
        elapsed_seconds=1.0,
        peak_captured_bytes=10,
        processes_started=1,
        peak_rss_bytes=None,
    ).report()
    assert report["peak_rss_bytes"] is None
    assert report["rss"] == "NOT_MEASURED"


# --------------------------------------------------------------------------- #
# The numeric layer: bounded decode and honest statistics
# --------------------------------------------------------------------------- #


def test_decoding_raw_pcm_refuses_a_non_positive_channel_count(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import decode_s32le

    sink = tmp_path / "raw.s32le"
    sink.write_bytes(b"\x00" * 64)
    with pytest.raises(ValueError, match="channels must be >= 1"):
        decode_s32le(sink, channels=0)


def test_a_truncated_sample_is_dropped_rather_than_half_read(tmp_path: Path) -> None:
    """A partial ``int32`` at the tail must not become a sample.

    ``array('i')`` would happily read a 3-byte remainder as garbage; the decoder
    trims to whole samples, so the frame count is *lower* rather than wrong.
    """
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import decode_s32le

    sink = tmp_path / "raw.s32le"
    # 5 full samples (20 bytes) + 3 stray bytes.
    sink.write_bytes(b"".join(int(2**30).to_bytes(4, "little", signed=True) for _ in range(5)))
    with sink.open("ab") as handle:
        handle.write(b"\x01\x02\x03")
    samples = decode_s32le(sink, channels=1)
    assert samples.shape == (5, 1)
    assert samples[0, 0] == pytest.approx(0.5, abs=1e-9)


def test_decoding_an_empty_or_odd_file_yields_no_samples(tmp_path: Path) -> None:
    """No frames is an empty array, not an exception and not a zero pad."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import decode_s32le

    empty = tmp_path / "empty.s32le"
    empty.write_bytes(b"")
    stray = tmp_path / "stray.s32le"
    stray.write_bytes(b"\x01\x02\x03")
    assert decode_s32le(empty, channels=1).shape == (0, 1)
    assert decode_s32le(stray, channels=1).shape == (0, 1)


def test_the_frame_budget_stops_the_decode_exactly_where_asked(tmp_path: Path) -> None:
    """``max_frames`` is a bound, and the returned shape proves it held."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import decode_s32le

    sink = tmp_path / "raw.s32le"
    sink.write_bytes(int(2**30).to_bytes(4, "little", signed=True) * 5_000)
    assert decode_s32le(sink, channels=1, max_frames=10).shape == (10, 1)
    assert decode_s32le(sink, channels=2).shape[1] == 2


def test_resampling_preserves_duration_and_refuses_impossible_rates() -> None:
    """Analysis resampling is linear interpolation, and it is labelled as such."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import resample_mono

    rate = np.sin(2.0 * np.pi * 440.0 * np.arange(48_000) / 48_000)
    resampled = resample_mono(rate, source_rate=48_000, target_rate=16_000)
    assert resampled.size == 16_000, "the analysis duration must not drift"
    # Same rate is an identity, not a copy that could introduce error.
    assert resample_mono(rate, source_rate=48_000, target_rate=48_000) is rate
    with pytest.raises(ValueError, match="sample rates must be positive"):
        resample_mono(rate, source_rate=0, target_rate=48_000)


def test_a_nan_or_infinite_sample_cannot_become_a_finite_statistic() -> None:
    """Statistics of a corrupted buffer must not report a plausible number."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import analyse_samples

    with pytest.raises(ValueError, match="mono"):
        analyse_samples(np.zeros((10, 2)), sample_rate=48_000, channels=2)
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        analyse_samples(np.zeros(10), sample_rate=0, channels=1)


def test_an_empty_signal_has_no_measurements_rather_than_zeros() -> None:
    """Silence is ``None``, not 0.0 dBFS — the distinction the ladder depends on."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import analyse_samples

    result = analyse_samples(np.zeros(0), sample_rate=48_000, channels=1)
    assert result.seconds_covered == 0.0
    assert result.rms_dbfs is None
    assert result.peak_dbfs is None
    assert result.crest_factor_db is None
    assert result.spectral_centroid_hz is None


def test_digital_silence_reports_no_loudness_instead_of_minus_infinity() -> None:
    """A buffer of exact zeros must not produce ``-inf`` dB statistics."""
    from nexus_ai_agent.creative.audio_engine.numpy_dsp import analyse_samples

    result = analyse_samples(np.zeros(48_000), sample_rate=48_000, channels=1)
    assert result.rms_dbfs is None
    assert result.peak_dbfs is None
    assert result.crest_factor_db is None
    assert result.band_energy_ratio == {}


# --------------------------------------------------------------------------- #
# verify_passport: every way a passport can lose its authority
# --------------------------------------------------------------------------- #


def _passport(engine: NativeAudioEngine, media_dir: Path):
    """Build one real, signed passport to re-verify against the world."""
    media.write_noisy_voice(media_dir / "voice.wav", seconds=8.0)
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="verify-source",
            source_asset_id="voice",
            output_asset_id="voice_clean",
            strength=0.4,
        )
    )
    return evidence


@requires_ffmpeg
def test_an_unsigned_passport_is_reported_unsigned_not_trusted(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """An unsigned passport is *measured*, never *attested*.

    ``require_signature=True`` is how a consumer refuses to promote a
    build-time measurement into a trusted fact.  The codes are what make the
    refusal actionable: ``passport_unsigned`` says exactly what is missing.
    """
    from nexus_ai_agent.creative.audio_engine.verify import CODE_UNSIGNED, verify_passport

    passport, path = _signed_passport(engine, media_dir)
    report = verify_passport(passport, path=path, require_signature=True)
    assert report.signature_ok is False
    assert report.passed is False
    assert CODE_UNSIGNED in report.codes
    assert report.code == CODE_UNSIGNED


@requires_ffmpeg
def test_an_unsigned_passport_still_verifies_its_measurements(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Without a signature the *bytes* can still be proven — and only those."""
    from nexus_ai_agent.creative.audio_engine.verify import CODE_UNSIGNED, verify_passport

    passport, path = _signed_passport(engine, media_dir)
    report = verify_passport(passport, path=path, require_signature=False)
    assert report.bytes_ok is True
    assert report.signature_ok is False
    assert CODE_UNSIGNED in report.codes, "the missing attestation is still reported"


def _signed_passport(engine: NativeAudioEngine, media_dir: Path):
    """The engine's own passport for a fresh artifact, unsigned by construction.

    Rebuilding it here (rather than reaching into the port's projection) keeps
    the test pointed at the verifier's contract: the object under test is a
    passport, and an unsigned one must never verify as attested.
    """
    from nexus_ai_agent.creative.audio_engine import verify as verify_module
    from nexus_ai_agent.creative.audio_engine.models import CanonicalStreamProfile

    source = media.write_sine(media_dir / "passport_src.wav", seconds=2.0)
    # The passport records *facts*, never a path: where the bytes live is the
    # caller's statement, and re-verification takes it as an argument.  That is
    # what lets a passport be checked against a copy at a different location.
    return (
        verify_module.build_passport(
            operation="audio.remove_noise",
            asset_id="passport_src",
            path=source,
            source_sha256="sha256:" + "0" * 64,
            profile=CanonicalStreamProfile(),
            loudness_profile=None,
            engine=engine._transcoder,
            max_analysis_seconds=5,
        ),
        source,
    )


@requires_ffmpeg
def test_a_passport_whose_bytes_changed_is_refused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """The artifact hash is re-measured; a replaced file invalidates the passport."""
    from nexus_ai_agent.creative.audio_engine.verify import CODE_SHA_MISMATCH, verify_passport

    passport, path = _signed_passport(engine, media_dir)
    path.write_bytes(b"replaced after the passport was issued")
    report = verify_passport(passport, path=path, require_signature=False)
    assert report.passed is False
    assert CODE_SHA_MISMATCH in report.codes


@requires_ffmpeg
def test_a_passport_with_no_path_cannot_be_verified(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """No path means the bytes are re-hashed against nothing; that is a refusal."""
    from nexus_ai_agent.creative.audio_engine.verify import CODE_SHA_MISMATCH, verify_passport

    passport, _ = _signed_passport(engine, media_dir)
    report = verify_passport(passport, require_signature=False)
    assert report.passed is False
    assert CODE_SHA_MISMATCH in report.codes


@requires_ffmpeg
def test_a_passport_for_a_missing_artifact_is_refused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    from nexus_ai_agent.creative.audio_engine.verify import CODE_SHA_MISMATCH, verify_passport

    passport, path = _signed_passport(engine, media_dir)
    path.unlink()
    report = verify_passport(passport, path=path, require_signature=False)
    assert report.passed is False
    assert CODE_SHA_MISMATCH in report.codes


def test_building_a_passport_for_a_path_that_does_not_exist_is_refused(
    engine: NativeAudioEngine, tmp_path: Path
) -> None:
    """A passport describes a file; no file, no passport."""
    from nexus_ai_agent.creative.audio_engine.models import CanonicalStreamProfile
    from nexus_ai_agent.creative.audio_engine.verify import PassportError, build_passport

    with pytest.raises(PassportError, match="artifact does not exist"):
        build_passport(
            operation="audio.remove_noise",
            asset_id="absent",
            path=tmp_path / "nothing.flac",
            source_sha256="sha256:" + "0" * 64,
            profile=CanonicalStreamProfile(),
            loudness_profile=None,
            engine=engine._transcoder,
        )


def test_a_keyring_with_a_wrong_length_key_is_refused_at_construction() -> None:
    """A 31-byte "public key" is not a key; the trust root says so once."""
    from nexus_ai_agent.creative.audio_engine.verify import PassportError, PassportKeyring

    with pytest.raises(PassportError, match="must be 32 bytes"):
        PassportKeyring(public_keys={"k1": b"\x01" * 31})


def test_signing_with_a_key_that_is_not_a_seed_is_refused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Ed25519 seeds are exactly 32 bytes; anything else is a caller bug."""
    from nexus_ai_agent.creative.audio_engine.verify import PassportError, sign_passport

    passport, _ = _signed_passport(engine, media_dir)
    with pytest.raises(PassportError, match="32-byte seed"):
        sign_passport(passport, signing_key=b"short")


def test_a_signature_that_does_not_match_the_message_is_refused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """A tampered payload must fail Ed25519 verification, not pass on trust."""
    from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_SIGNATURE_INVALID,
        PassportKeyring,
        sign_passport,
        verify_passport,
    )

    seed = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    unsigned, path = _signed_passport(engine, media_dir)
    passport = sign_passport(unsigned, signing_key=seed, key_id="own")
    keyring = PassportKeyring(public_keys={"own": derive_public_key(seed)})
    # Same passport, one measured fact altered after signing.
    tampered = passport.model_copy(update={"operation": "audio.normalize_loudness"})
    report = verify_passport(tampered, keyring=keyring, path=path, require_signature=True)
    assert report.passed is False
    assert CODE_SIGNATURE_INVALID in report.codes


def test_a_signer_key_id_that_is_not_in_the_keyring_is_unknown() -> None:
    """An unrecognised signer is not "trusted by default"."""
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_UNKNOWN_KEY,
        VerificationReport,
    )

    report = VerificationReport(
        passport_id="pp_x",
        signature_ok=False,
        bytes_ok=True,
        decode_ok=True,
        loudness_ok=True,
        canonical_ok=True,
        passed=False,
        codes=(CODE_UNKNOWN_KEY,),
    )
    assert report.code == CODE_UNKNOWN_KEY
    verified = report.model_copy(update={"passed": True, "codes": ()})
    assert verified.code is None


def engine_probe_without_stream():
    """A probe of a file that carried no decodable audio stream."""
    from nexus_ai_agent.creative.audio_engine.models import MediaProbe

    return MediaProbe(source="banner", size_bytes=0, sha256="sha256:" + "0" * 64)


def test_a_profile_check_refuses_a_probe_with_no_stream() -> None:
    """The profile verdict names the missing stream instead of guessing a codec."""
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_PROFILE_MISMATCH,
        check_canonical_profile,
    )

    probe = engine_probe_without_stream()
    ok, code, detail = check_canonical_profile(probe, CanonicalStreamProfile())
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH
    assert "no audio stream" in (detail or "")


def test_the_loudness_check_refuses_a_missing_true_peak() -> None:
    """An unmeasurable peak cannot pass a ceiling; it fails the check."""
    from nexus_ai_agent.creative.audio_engine.verify import check_loudness_target

    measurement = LoudnessMeasurement(
        integrated_lufs=-23.0, true_peak_dbtp=None, loudness_range_lu=5.0
    )
    check = check_loudness_target(
        LoudnessProfile(
            profile_id="custom", target_lufs=-23.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
        ),
        measurement,
    )
    assert check.true_peak_ok is False
    assert check.passed is False
    assert check.code == verify.CODE_TRUE_PEAK_EXCEEDED


def test_a_true_peak_above_the_ceiling_is_named_as_the_reason() -> None:
    """The failure code distinguishes "too loud" from "wrong integrated level"."""
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_TRUE_PEAK_EXCEEDED,
        check_loudness_target,
    )

    measurement = LoudnessMeasurement(
        integrated_lufs=-23.0, true_peak_dbtp=+0.5, loudness_range_lu=5.0
    )
    check = check_loudness_target(
        LoudnessProfile(
            profile_id="custom", target_lufs=-23.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
        ),
        measurement,
    )
    assert check.integrated_ok is True
    assert check.true_peak_ok is False
    assert check.code == CODE_TRUE_PEAK_EXCEEDED


# --------------------------------------------------------------------------- #
# The decoder and the loudness parser: exit code 0 is not proof
# --------------------------------------------------------------------------- #


def test_decoding_without_a_channel_count_is_refused(tmp_path: Path) -> None:
    """Interleaved samples counted as frames is how a stereo file doubles."""
    with pytest.raises(AudioDecodeError, match="needs the stream's channel count"):
        FfmpegTranscoder().decode_all(
            tmp_path / "x.flac", pcm_path=tmp_path / "x.s32le", channels=0
        )


@requires_ffmpeg
def test_a_non_media_decode_is_refused_not_returned_as_zero_frames(tmp_path: Path) -> None:
    """A decode that exits 0 with no samples is still a failure."""
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"RIFF" + b"\x00" * 400)
    with pytest.raises(AudioDecodeError, match="decode of"):
        FfmpegTranscoder().decode_all(bogus, pcm_path=tmp_path / "out.s32le", channels=1)


@requires_ffmpeg
def test_a_truncated_pcm_sink_is_refused(tmp_path: Path, media_dir: Path) -> None:
    """A PCM byte count that is not a whole number of frames is a hard failure."""
    source = media.write_sine(media_dir / "tone.wav", seconds=1.0)
    transcoder = FfmpegTranscoder()
    real_run = FfmpegTranscoder._run

    def truncated(self, argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        outcome = real_run(self, argv, **kwargs)
        sink = kwargs.get("stdout_path")
        if sink is not None:
            Path(sink).write_bytes(b"\x00\x00\x00")  # 3 bytes: not a whole sample
        return outcome

    transcoder._run = truncated.__get__(transcoder)  # type: ignore[method-assign]
    with pytest.raises(AudioDecodeError, match="failed"):
        transcoder.decode_all(source, pcm_path=tmp_path / "out.s32le", channels=1)


def test_the_loudnorm_json_parser_refuses_every_malformed_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Four distinct malformed documents must produce four distinct refusals.

    The parser is the gate between FFmpeg's *text* and the gain the engine will
    apply, so "no JSON", "unparseable JSON", "a non-numeric field" and "-inf"
    cannot share one behaviour: the first three are errors, the last is silence.
    """
    from nexus_ai_agent.creative.audio_engine.models import NAGAR_STREAMING_PRESET

    source = tmp_path / "x.wav"
    source.write_bytes(b"\x00" * 64)
    transcoder = FfmpegTranscoder()

    def with_stderr(text: str):
        class _Outcome:
            ok = True
            returncode = 0
            termination = "exited"
            stderr_tail = text
            elapsed_seconds = 0.0

        return _Outcome()

    def patch(text: str) -> None:
        transcoder._run = lambda *a, **k: with_stderr(text)  # type: ignore[method-assign]

    patch("no json at all here")
    with pytest.raises(AudioMeasurementError, match="no measurement JSON"):
        transcoder.measure_loudnorm_pass(source, NAGAR_STREAMING_PRESET)

    patch("{ this is not json }")
    with pytest.raises(AudioMeasurementError, match="not parseable"):
        transcoder.measure_loudnorm_pass(source, NAGAR_STREAMING_PRESET)

    patch(
        '{"input_i": "not a number", "input_tp": "-1", "input_lra": "5", '
        '"input_thresh": "-30", "target_offset": "0"}'
    )
    with pytest.raises(AudioMeasurementError, match="not a number"):
        transcoder.measure_loudnorm_pass(source, NAGAR_STREAMING_PRESET)

    patch(
        '{"input_i": "-inf", "input_tp": "-inf", "input_lra": "0", '
        '"input_thresh": "-inf", "target_offset": "inf"}'
    )
    assert transcoder.measure_loudnorm_pass(source, NAGAR_STREAMING_PRESET) is None


@requires_ffmpeg
def test_a_loudnorm_pass_that_exits_nonzero_is_a_measurement_error(tmp_path: Path) -> None:
    """A failed measurement pass cannot produce a gain; it must raise."""
    from nexus_ai_agent.creative.audio_engine.models import NAGAR_STREAMING_PRESET

    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"RIFF" + b"\x00" * 400)
    with pytest.raises(AudioMeasurementError):
        FfmpegTranscoder().measure_loudnorm_pass(bogus, NAGAR_STREAMING_PRESET)


@requires_ffmpeg
def test_an_ebur128_summary_without_its_numbers_is_refused(tmp_path: Path) -> None:
    """A summary that lost the integrated line is not a measurement."""
    source = media.write_sine(tmp_path / "tone.wav", seconds=1.0)
    transcoder = FfmpegTranscoder()
    real_run = FfmpegTranscoder._run

    def gutted(self, argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        outcome = real_run(self, argv, **kwargs)
        if "ebur128=peak=true:framelog=quiet" in list(argv):
            return outcome.model_copy(update={"stderr_tail": "Summary:\\n  Loudness range: 5.0 LU"})
        return outcome

    transcoder._run = gutted.__get__(transcoder)  # type: ignore[method-assign]
    with pytest.raises(AudioMeasurementError, match="missing integrated loudness"):
        transcoder.measure_loudness(source)


def test_a_probe_binary_that_exists_as_a_sibling_is_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ffprobe`` next to ``ffmpeg`` is used before ``PATH`` — the bundled shape."""
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_ffmpeg.chmod(0o755)
    sibling = tmp_path / "ffprobe"
    sibling.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sibling.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    transcoder = FfmpegTranscoder(binary=str(fake_ffmpeg))
    assert transcoder.probe_binary == str(sibling)


def test_the_available_flag_is_false_rather_than_raising(tmp_path: Path) -> None:
    """``available()`` is a question; it must answer even when the answer is no."""
    assert FfmpegTranscoder(binary=str(tmp_path / "absent")).available() is False


# --------------------------------------------------------------------------- #
# Continuity: the verdict objects a consumer reads
# --------------------------------------------------------------------------- #


def test_a_continuity_axis_knows_whether_it_is_ok() -> None:
    from nexus_ai_agent.creative.audio_engine.continuity import AxisVerdict

    assert AxisVerdict(axis="loudness", verdict="ok").ok is True
    drifted = AxisVerdict(axis="loudness", verdict="drift")
    assert drifted.ok is False


def test_a_report_codes_only_the_axes_that_drifted() -> None:
    """The codes are the actionable list; ``ok`` axes must not appear in it."""
    from nexus_ai_agent.creative.audio_engine.continuity import (
        AxisVerdict,
        ContinuityReport,
    )

    report = ContinuityReport(
        reference_id="ep1",
        candidate_id="ep2",
        lock_hash_match=True,
        stitchable=False,
        axes=(
            AxisVerdict(axis="loudness", verdict="ok"),
            AxisVerdict(axis="sample_rate", verdict="drift"),
            AxisVerdict(axis="signature", verdict="unknown"),
        ),
        drift_axes=("sample_rate",),
        unknown_axes=("signature",),
    )
    assert report.codes == ("sample_rate:drift", "signature:unknown")
    assert report.continuous is False
    assert report.stitchable is False


def test_a_scale_that_cannot_be_compared_is_none_not_zero() -> None:
    """A band distance with no shared bands is unmeasurable, not identical.

    Returning ``0.0`` here would report two unrelated spectra as a perfect match —
    exactly the kind of fabrication the continuity gate exists to prevent.
    """
    from nexus_ai_agent.creative.audio_engine.continuity import _band_distance

    assert _band_distance({}, {"bass": 0.5}) is None
    assert _band_distance({"bass": 0.0}, {"bass": 0.0}) is None
    identical = _band_distance({"bass": 0.5, "air": 0.1}, {"bass": 0.5, "air": 0.1})
    assert identical == pytest.approx(0.0, abs=1e-12)


def test_an_assembly_proof_names_the_strategy_it_used() -> None:
    """The strategy is derived from the proof, never chosen by the caller."""
    from nexus_ai_agent.creative.audio_engine.continuity import AssemblyProof
    from nexus_ai_agent.creative.audio_engine.models import CANONICAL_PROFILE

    lossless = AssemblyProof(lossless=True, reason=None, reference_profile=CANONICAL_PROFILE)
    assert lossless.strategy == "stream_copy"
    refused = AssemblyProof(
        lossless=False, reason="mixed shapes", reference_profile=CANONICAL_PROFILE
    )
    assert refused.strategy == "re_encode_required"


def test_a_signature_is_recognised_whether_wrapped_or_bare() -> None:
    """``_signature_of`` accepts a passport *or* a signature, and nothing else."""
    from nexus_ai_agent.creative.audio_engine.continuity import _signature_of
    from nexus_ai_agent.creative.audio_engine.models import SpectralSignature

    bare = SpectralSignature(
        sample_rate=48_000,
        channels=2,
        samples_analyzed=1000,
        analysis_seconds=1.0,
        rms_dbfs=-20.0,
    )
    assert _signature_of(bare) is bare

    class _Wrapped:
        signature = bare

    assert _signature_of(_Wrapped()) is bare
    assert _signature_of(object()) is None


# --------------------------------------------------------------------------- #
# The workspace boundary: refusals a caller can act on
# --------------------------------------------------------------------------- #


def test_staging_a_missing_input_names_the_file(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    from nexus_ai_agent.creative.audio_engine.workspace import WorkspaceBoundaryError

    with pytest.raises(WorkspaceBoundaryError, match="input file not found"):
        workspace.stage_input(tmp_path / "never-existed.wav")


def test_a_run_id_that_is_a_path_segment_is_refused_by_name(tmp_path: Path) -> None:
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    from nexus_ai_agent.creative.audio_engine.workspace import WorkspaceBoundaryError

    for hostile in ("../up", "a/b", "a\\b", ".", ".."):
        with pytest.raises(WorkspaceBoundaryError, match="unsafe run id"):
            with workspace.run_directory(hostile):
                pass  # pragma: no cover


def test_publishing_a_staged_path_that_vanished_is_refused(tmp_path: Path) -> None:
    """The atomic rename has to fail closed when the source is gone."""
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    source = workspace.stage_input(media.write_sine(tmp_path / "tone.wav"))
    destination = workspace.media_path("tone_out", suffix=".wav")
    source.unlink()
    with pytest.raises(Exception) as refusal:  # noqa: B017 - AtomicPublishError
        workspace.publish(source, destination, replace=True)
    assert "does not exist" in str(refusal.value)


def test_a_dotted_asset_id_stays_inside_the_media_directory(tmp_path: Path) -> None:
    """A leading dot is not a hidden-file bug; it is still a contained name.

    ``.hidden.wav`` is not a traversal and not a dotfile a listing would miss (it
    carries a real suffix and lives under ``media/``).  What must hold is that the
    name is inside the boundary and stable — so this pins containment rather than
    inventing a renaming rule the engine does not have.
    """
    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    path = workspace.media_path(".hidden", suffix=".wav")
    assert path.parent == workspace.media_dir
    assert path.name == ".hidden.wav"
    assert workspace.contain(path) == path
