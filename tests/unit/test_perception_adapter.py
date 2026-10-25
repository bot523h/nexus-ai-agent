"""The perception engine against a *real* FFmpeg, plus hostile/failing stand-ins.

Real-binary tests run once per available FFmpeg build (the distro build and the
``imageio-ffmpeg`` wheel CI uses) so a format drift between versions fails here.
Failure paths use tiny shell stand-ins that behave like FFmpeg's stderr, which is
the only way to exercise timeouts, floods and mid-analysis modification
deterministically.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from nexus_ai_agent.creative.packs.perception.models import (
    OPERATION_ASSESS_LOUDNESS,
    OPERATION_MARK_SCENE_CUTS,
    OPERATION_MARK_SILENCES,
    LoudnessEvidence,
    SceneCutEvidence,
    SilenceEvidence,
)
from nexus_ai_agent.creative.packs.perception.operations import register_perception_operations
from nexus_ai_agent.creative.perception import (
    AnalysisFailedError,
    AnalysisInputError,
    AnalysisTimeoutError,
    AnalysisUnavailableError,
    FfmpegAnalyzer,
)
from nexus_ai_agent.creative.perception import analyzer as analyzer_module
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    MediaRef,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)

SEC = 1_000_000


def _binaries() -> dict[str, str]:
    found: dict[str, str] = {}
    system = shutil.which("ffmpeg")
    if system:
        found["system"] = system
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        found["bundled"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - extra not installed
        pass
    return found


BINARIES = _binaries()
needs_ffmpeg = pytest.mark.skipif(not BINARIES, reason="no FFmpeg binary available")


def _ffmpeg(binary: str, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed test fixture generation
        [binary, "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        timeout=60,
    )


@pytest.fixture(scope="module", params=sorted(BINARIES) or ["none"])
def engine(request: pytest.FixtureRequest) -> str:
    if request.param == "none":
        pytest.skip("no FFmpeg binary available")
    return BINARIES[request.param]


@pytest.fixture(scope="module")
def media(engine: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("perception-media")
    out = {
        name: root / name
        for name in (
            "tone_gap.wav",
            "tone.wav",
            "mute.wav",
            "scenes.mp4",
            "still.mp4",
            "av.mp4",
            "trailing.wav",
        )
    }
    # 2s tone, 1.5s digital silence, 1.5s tone -> one silence ~[2.0, 3.5]
    _ffmpeg(
        engine,
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1.5",
        "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]", "-map", "[a]",
        str(out["tone_gap.wav"]),
    )  # fmt: skip
    _ffmpeg(engine, "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(out["tone.wav"]))
    _ffmpeg(engine, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2", str(out["mute.wav"]))
    # 1s red, 1s blue, 1s green -> hard cuts at 1s and 2s
    _ffmpeg(
        engine,
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=green:s=160x90:r=10:d=1",
        "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]",
        "-pix_fmt", "yuv420p", str(out["scenes.mp4"]),
    )  # fmt: skip
    _ffmpeg(
        engine,
        "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=10:d=3",
        "-pix_fmt", "yuv420p", str(out["still.mp4"]),
    )  # fmt: skip
    _ffmpeg(
        engine,
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-pix_fmt", "yuv420p", "-shortest", str(out["av.mp4"]),
    )  # fmt: skip
    # 1s tone then silence to the very end (no silence_end is ever printed)
    _ffmpeg(
        engine,
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
        "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]",
        str(out["trailing.wav"]),
    )  # fmt: skip
    return out


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# real FFmpeg
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_probe_reports_streams(engine: str, media: dict[str, Path]) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    video = analyzer.probe(media["scenes.mp4"])
    assert (video.has_video, video.has_audio, video.width, video.height) == (True, False, 160, 90)
    assert abs(video.duration_us - 3 * SEC) < 100_000
    both = analyzer.probe(media["av.mp4"])
    assert both.has_video and both.has_audio
    audio = analyzer.probe(media["tone.wav"])
    assert (audio.has_video, audio.has_audio, audio.sample_rate) == (False, True, 44100)


@needs_ffmpeg
def test_silence_is_measured_and_content_bound(engine: str, media: dict[str, Path]) -> None:
    evidence = FfmpegAnalyzer(binary=engine).silences(media["tone_gap.wav"])
    assert isinstance(evidence, SilenceEvidence)
    assert evidence.asset_sha256 == _sha(media["tone_gap.wav"])
    assert evidence.engine.startswith("ffmpeg ")
    assert abs(evidence.duration_us - 5 * SEC) < 100_000
    assert len(evidence.spans) == 1
    span = evidence.spans[0]
    assert abs(span.start_us - 2 * SEC) < 80_000
    assert abs(span.end_us - 3_500_000) < 80_000
    # the sealed evidence re-validates from its own JSON
    assert SilenceEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@needs_ffmpeg
def test_min_silence_filters_short_gaps(engine: str, media: dict[str, Path]) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    assert analyzer.silences(media["tone_gap.wav"], min_silence_s=2.0).spans == ()
    assert len(analyzer.silences(media["tone_gap.wav"], min_silence_s=1.0).spans) == 1


@needs_ffmpeg
def test_continuous_tone_has_no_silence(engine: str, media: dict[str, Path]) -> None:
    assert FfmpegAnalyzer(binary=engine).silences(media["tone.wav"]).spans == ()


@needs_ffmpeg
def test_silence_running_to_the_end_is_closed_at_the_duration(
    engine: str, media: dict[str, Path]
) -> None:
    evidence = FfmpegAnalyzer(binary=engine).silences(media["trailing.wav"])
    assert len(evidence.spans) == 1
    assert abs(evidence.spans[0].start_us - SEC) < 80_000
    assert evidence.spans[0].end_us == evidence.duration_us


@needs_ffmpeg
def test_scene_cuts_are_found_on_hard_cuts_only(engine: str, media: dict[str, Path]) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    evidence = analyzer.scene_cuts(media["scenes.mp4"])
    assert isinstance(evidence, SceneCutEvidence)
    assert len(evidence.cuts_us) == 2
    assert abs(evidence.cuts_us[0] - SEC) < 100_000
    assert abs(evidence.cuts_us[1] - 2 * SEC) < 100_000
    assert analyzer.scene_cuts(media["still.mp4"]).cuts_us == ()


@needs_ffmpeg
def test_loudness_of_a_known_sine(engine: str, media: dict[str, Path]) -> None:
    evidence = FfmpegAnalyzer(binary=engine).loudness(media["tone.wav"])
    assert isinstance(evidence, LoudnessEvidence)
    # a full-scale-ish sine from lavfi sits near -22 LUFS / -18 dBFS peak
    assert evidence.integrated_lufs is not None and -24.0 < evidence.integrated_lufs < -20.0
    assert evidence.true_peak_dbfs is not None and -20.0 < evidence.true_peak_dbfs < -16.0


@needs_ffmpeg
def test_digital_silence_has_no_measurable_loudness(engine: str, media: dict[str, Path]) -> None:
    evidence = FfmpegAnalyzer(binary=engine).loudness(media["mute.wav"])
    assert evidence.integrated_lufs is None
    assert evidence.true_peak_dbfs is None


@needs_ffmpeg
def test_missing_streams_are_typed_input_errors(engine: str, media: dict[str, Path]) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    with pytest.raises(AnalysisInputError, match="no video stream"):
        analyzer.scene_cuts(media["tone.wav"])
    with pytest.raises(AnalysisInputError, match="no audio stream"):
        analyzer.silences(media["scenes.mp4"])
    with pytest.raises(AnalysisInputError, match="no audio stream"):
        analyzer.loudness(media["scenes.mp4"])


@needs_ffmpeg
def test_bad_inputs_are_typed_errors(engine: str, media: dict[str, Path], tmp_path: Path) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    with pytest.raises(AnalysisInputError, match="not found"):
        analyzer.silences(tmp_path / "missing.wav")
    with pytest.raises(AnalysisInputError, match="not a regular file"):
        analyzer.silences(tmp_path)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"\x00\x01 not media at all \xff\xfe" * 8)  # binary, but no container
    with pytest.raises(AnalysisFailedError):
        analyzer.silences(junk)
    for bad in (-100.0, -9.0, 0.0):
        with pytest.raises(AnalysisInputError, match="noise_floor_db"):
            analyzer.silences(media["tone.wav"], noise_floor_db=bad)
    for bad in (0.0, 0.001, 601.0):
        with pytest.raises(AnalysisInputError, match="min_silence_s"):
            analyzer.silences(media["tone.wav"], min_silence_s=bad)
    for bad in (0.0, 1.0, -0.5):
        with pytest.raises(AnalysisInputError, match="threshold"):
            analyzer.scene_cuts(media["scenes.mp4"], threshold=bad)


@needs_ffmpeg
def test_a_dash_leading_filename_cannot_become_an_option(
    engine: str, media: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "-hostile.wav"
    shutil.copy(media["tone.wav"], hostile)
    monkeypatch.chdir(tmp_path)
    evidence = FfmpegAnalyzer(binary=engine).silences("./-hostile.wav")
    assert evidence.asset_sha256 == _sha(hostile)
    evidence = FfmpegAnalyzer(binary=engine).silences("-hostile.wav")  # relative, leading dash
    assert evidence.asset_sha256 == _sha(hostile)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("evil.m3u8", "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n"),
        ("local.m3u8", "#EXTM3U\n#EXTINF:1,\n/etc/passwd\n#EXT-X-ENDLIST\n"),
        ("list.ffconcat", "ffconcat version 1.0\nfile '/etc/passwd'\n"),
        ("call.sdp", "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=x\nm=audio 5004 RTP/AVP 0\n"),
        ("manifest.mpd", "<?xml version='1.0'?><MPD><Period/></MPD>"),
        ("empty.wav", ""),
    ],
)
def test_text_inputs_are_refused_before_any_process_runs(
    tmp_path: Path, name: str, text: str
) -> None:
    """HLS/concat/SDP/DASH can name other files or URLs (the CVE-2016-1897/1898 class):
    refused outright, with a binary that would fail the test if it were ever executed."""
    canary = tmp_path / "executed"
    binary = _fake(tmp_path, f"touch {canary}\nexit 0\n")
    target = tmp_path / name
    target.write_text(text)
    analyzer = FfmpegAnalyzer(binary=binary)
    for call in (
        analyzer.probe,
        analyzer.silences,
        analyzer.scene_cuts,
        analyzer.loudness,
    ):
        with pytest.raises(AnalysisInputError, match="text, not a media container"):
            call(target)
    assert not canary.exists(), "FFmpeg must never be started for a text input"


@needs_ffmpeg
def test_real_ffmpeg_never_sees_a_network_playlist(engine: str, tmp_path: Path) -> None:
    playlist = tmp_path / "evil.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:9/never.ts\n#EXT-X-ENDLIST\n")
    with pytest.raises(AnalysisInputError):
        FfmpegAnalyzer(binary=engine, timeout_s=20).silences(playlist)


@needs_ffmpeg
def test_binary_media_is_not_mistaken_for_text(engine: str, media: dict[str, Path]) -> None:
    for name in ("tone.wav", "scenes.mp4", "av.mp4"):
        head = media[name].read_bytes()[:1024]
        assert not analyzer_module._looks_textual(head), name


@needs_ffmpeg
def test_evidence_drives_the_command_bus_end_to_end(engine: str, media: dict[str, Path]) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    path = media["tone_gap.wav"]
    silence = analyzer.silences(path)
    loudness = analyzer.loudness(path)
    sha = silence.asset_sha256
    clip = Clip(
        clip_id="c1",
        media_ref=MediaRef(
            asset_id="a1", content_sha256=sha, media_kind="audio", duration_us=silence.duration_us
        ),
        source_range=TimeRangeUS(start_us=0, end_us=silence.duration_us),
        timeline_range=TimeRangeUS(start_us=10 * SEC, end_us=10 * SEC + silence.duration_us),
    )
    timeline = Timeline(
        timeline_id="t",
        duration_us=30 * SEC,
        tracks=[Track(track_id="a", name="A", kind="audio", clips=[clip])],
    )
    project = new_project("p", "P", timeline).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="a1",
                    media_kind="audio",
                    content_sha256=sha,
                    duration_us=silence.duration_us,
                )
            ]
        }
    )
    registry = build_wave1_registry()
    register_perception_operations(registry)
    bus = CommandBus(project, registry=registry)

    marked = bus.dispatch(
        TypedCommand(
            command_id="m1",
            operation=OPERATION_MARK_SILENCES,
            input={
                "track_id": "a",
                "clip_id": "c1",
                "edge": "both",
                "evidence": silence.model_dump(mode="json"),
            },
        )
    )
    assert marked.output["markers_added"] == 2
    times = sorted(m.timecode_us for m in bus.project.timeline.markers)
    assert abs(times[0] - 12 * SEC) < 80_000 and abs(times[1] - 13_500_000) < 80_000

    assessed = bus.dispatch(
        TypedCommand(
            command_id="l1",
            operation=OPERATION_ASSESS_LOUDNESS,
            input={"asset_id": "a1", "evidence": loudness.model_dump(mode="json")},
        )
    )
    assert assessed.output["verdict"] in {"too_quiet", "too_loud", "within_tolerance"}
    assert assessed.output["normalize_arguments"]["audio_asset_id"] == "a1"

    # evidence about *different bytes* is refused even though it is perfectly valid
    other = analyzer.scene_cuts(media["scenes.mp4"])
    with pytest.raises(Exception, match="different content"):
        bus.dispatch(
            TypedCommand(
                command_id="m2",
                operation=OPERATION_MARK_SCENE_CUTS,
                input={
                    "track_id": "a",
                    "clip_id": "c1",
                    "evidence": other.model_dump(mode="json"),
                },
            )
        )


@needs_ffmpeg
def test_engine_id_is_cached_and_versioned(engine: str) -> None:
    analyzer = FfmpegAnalyzer(binary=engine)
    first = analyzer.engine_id()
    assert first.startswith("ffmpeg ") and first != "ffmpeg unknown"
    assert analyzer.engine_id() is first


# ---------------------------------------------------------------------------
# stand-in binaries: deterministic failure modes
# ---------------------------------------------------------------------------

BANNER_AUDIO = (
    'echo "  Duration: 00:00:10.00, start: 0.000000, bitrate: 1 kb/s" >&2\n'
    'echo "  Stream #0:0: Audio: pcm_s16le, 44100 Hz, mono, s16, 705 kb/s" >&2\n'
)


def _fake(tmp_path: Path, body: str, name: str = "fake-ffmpeg") -> str:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


@pytest.fixture()
def media_file(tmp_path: Path) -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt \x00 original bytes")
    return path


def test_unresolvable_binary_is_unavailable(media_file: Path) -> None:
    with pytest.raises(AnalysisUnavailableError):
        FfmpegAnalyzer(binary="/definitely/not/here").silences(media_file)


def test_non_executable_binary_is_unavailable(tmp_path: Path, media_file: Path) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)
    with pytest.raises(AnalysisUnavailableError):
        FfmpegAnalyzer(binary=str(script)).silences(media_file)


def test_timeout_kills_the_process(tmp_path: Path, media_file: Path) -> None:
    marker = tmp_path / "still-running"
    binary = _fake(tmp_path, f"touch {marker}\nsleep 30\n")
    started = time.monotonic()
    with pytest.raises(AnalysisTimeoutError, match="killed"):
        FfmpegAnalyzer(binary=binary, timeout_s=0.5).silences(media_file)
    assert time.monotonic() - started < 10
    assert marker.exists()


def test_nonzero_exit_reports_the_stderr_tail(tmp_path: Path, media_file: Path) -> None:
    binary = _fake(tmp_path, 'echo "Invalid data found when processing input" >&2\nexit 3\n')
    with pytest.raises(AnalysisFailedError, match=r"status 3.*Invalid data"):
        FfmpegAnalyzer(binary=binary).silences(media_file)


def test_probe_output_without_a_duration_is_a_failure(tmp_path: Path, media_file: Path) -> None:
    binary = _fake(tmp_path, 'echo "nothing useful" >&2\n')
    with pytest.raises(AnalysisFailedError, match="cannot probe"):
        FfmpegAnalyzer(binary=binary).probe(media_file)


def test_a_flood_of_relevant_lines_is_refused(
    tmp_path: Path, media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyzer_module, "MAX_KEPT_LINES", 10)
    body = (
        'case "$*" in\n'
        "  *silencedetect*)\n"
        "    i=0; while [ $i -lt 200 ]; do\n"
        '      echo "[silencedetect @ 0x1] silence_start: $i" >&2; i=$((i+1)); done ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    with pytest.raises(AnalysisFailedError, match="more than 10 relevant lines"):
        FfmpegAnalyzer(binary=_fake(tmp_path, body)).silences(media_file)


def test_a_file_that_changes_during_analysis_yields_no_evidence(
    tmp_path: Path, media_file: Path
) -> None:
    body = (
        'prev=""; for a in "$@"; do\n'
        '  if [ "$prev" = "-i" ]; then input="$a"; fi; prev="$a"; done\n'
        'case "$*" in\n'
        '  *silencedetect*) echo "tampered" >> "$input" ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    with pytest.raises(AnalysisFailedError, match="changed while it was being analysed"):
        FfmpegAnalyzer(binary=_fake(tmp_path, body)).silences(media_file)


def test_media_longer_than_the_limit_is_refused_not_truncated(
    tmp_path: Path, media_file: Path
) -> None:
    binary = _fake(tmp_path, BANNER_AUDIO)  # reports 10 s
    with pytest.raises(AnalysisInputError, match="analysis limit"):
        FfmpegAnalyzer(binary=binary, max_media_s=5).silences(media_file)
    with pytest.raises(AnalysisInputError, match="analysis limit"):
        FfmpegAnalyzer(binary=binary, max_media_s=5).loudness(media_file)


def test_argv_contract(tmp_path: Path, media_file: Path) -> None:
    """Every call: no shell, an option-proof absolute input and a protocol whitelist."""
    log = tmp_path / "argv.log"
    body = (
        f'printf "%s\\n" "$@" > {log}.$$\n'
        'case "$*" in\n'
        '  *"-version"*) echo "ffmpeg version 9.9-test" ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    binary = _fake(tmp_path, body)
    analyzer = FfmpegAnalyzer(binary=binary)
    analyzer.silences(media_file)
    logs = sorted(tmp_path.glob("argv.log.*"))
    assert logs
    for entry in logs:
        argv = entry.read_text().splitlines()
        if "-version" in argv:
            continue
        assert argv[:3] == ["-hide_banner", "-nostdin", "-nostats"]
        assert argv[argv.index("-protocol_whitelist") + 1] == "file"
        assert argv.index("-protocol_whitelist") < argv.index("-i")
        allowed = argv[argv.index("-format_whitelist") + 1].split(",")
        assert argv.index("-format_whitelist") < argv.index("-i")
        assert {"wav", "mov", "matroska"} <= set(allowed)
        assert not {"hls", "applehttp", "concat", "sdp", "image2", "data", "dash"} & set(allowed)
        given = argv[argv.index("-i") + 1]
        assert os.path.isabs(given) and given == str(media_file.resolve())
    assert analyzer.engine_id() == "ffmpeg 9.9-test"


def test_unknown_version_output_degrades_to_a_labelled_engine(tmp_path: Path) -> None:
    analyzer = FfmpegAnalyzer(binary=_fake(tmp_path, 'echo "no version here"\n'))
    assert analyzer.engine_id() == "ffmpeg unknown"


def test_engine_query_failures_are_unavailable(tmp_path: Path) -> None:
    analyzer = FfmpegAnalyzer(binary=_fake(tmp_path, "sleep 30\n"), timeout_s=0.3)
    with pytest.raises(AnalysisUnavailableError, match="version"):
        analyzer.engine_id()


def test_a_same_size_same_mtime_swap_during_the_run_is_detected(
    tmp_path: Path, media_file: Path
) -> None:
    """Size+mtime can be forged with os.utime; the content hash cannot."""
    donor = tmp_path / "donor.wav"
    donor.write_bytes(bytes(b ^ 0x55 for b in media_file.read_bytes()))
    stamp = media_file.stat()
    body = (
        'prev=""; for a in "$@"; do\n'
        '  if [ "$prev" = "-i" ]; then input="$a"; fi; prev="$a"; done\n'
        'case "$*" in\n'
        f'  *silencedetect*) cat "{donor}" > "$input"; touch -r "{donor}" "$input" ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    os.utime(donor, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(AnalysisFailedError, match="changed while it was being analysed"):
        FfmpegAnalyzer(binary=_fake(tmp_path, body)).silences(media_file)


def test_a_file_replaced_by_rename_during_the_run_is_detected(
    tmp_path: Path, media_file: Path
) -> None:
    """A different inode at the same path (even with identical bytes) is not the file
    that was hashed; FFmpeg's input path is already the resolved file, so a symlink
    swap cannot redirect it, but a rename can replace it."""
    other = tmp_path / "other.wav"
    other.write_bytes(media_file.read_bytes())
    body = (
        'case "$*" in\n'
        f'  *silencedetect*) mv -f "{other}" "{media_file}" ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    with pytest.raises(AnalysisFailedError, match="changed while it was being analysed"):
        FfmpegAnalyzer(binary=_fake(tmp_path, body)).silences(media_file)


def test_evidence_names_the_rounded_parameters_ffmpeg_received(
    tmp_path: Path, media_file: Path
) -> None:
    log = tmp_path / "filter.log"
    body = (
        f'printf "%s\\n" "$@" > {log}.$$\n'
        'case "$*" in\n'
        '  *"-version"*) echo "ffmpeg version 9.9-test" ;;\n'
        "  *)\n" + BANNER_AUDIO + "    ;;\n"
        "esac\n"
    )
    analyzer = FfmpegAnalyzer(binary=_fake(tmp_path, body))
    evidence = analyzer.silences(media_file, noise_floor_db=-40.04, min_silence_s=0.50049)
    assert evidence.noise_floor_db == -40.0 and evidence.min_silence_us == 500_000
    filters = [
        line
        for entry in tmp_path.glob("filter.log.*")
        for line in entry.read_text().splitlines()
        if "silencedetect" in line
    ]
    assert filters == ["silencedetect=noise=-40.0dB:d=0.500"]
    with pytest.raises(AnalysisInputError, match="after rounding"):
        analyzer.silences(media_file, noise_floor_db=-99.96)
    with pytest.raises(AnalysisInputError, match="after rounding"):
        analyzer.scene_cuts(media_file, threshold=0.9996)


def test_too_many_spans_is_a_typed_engine_error_not_a_traceback(
    tmp_path: Path, media_file: Path
) -> None:
    lines = "".join(
        f'echo "[silencedetect @ 0x1] silence_start: {i}.0" >&2\n'
        f'echo "[silencedetect @ 0x1] silence_end: {i}.5 | silence_duration: 0.5" >&2\n'
        for i in range(0, 5001)
    )
    banner = BANNER_AUDIO.replace("00:00:10.00", "03:00:00.00")
    body = (
        'case "$*" in\n  *silencedetect*)\n' + lines + "    ;;\n  *)\n" + banner + "    ;;\nesac\n"
    )
    with pytest.raises(AnalysisFailedError, match="evidence limits"):
        FfmpegAnalyzer(binary=_fake(tmp_path, body), max_media_s=4 * 3600).silences(media_file)


@needs_ffmpeg
def test_real_ffmpeg_enforces_the_format_whitelist(
    engine: str, media: dict[str, Path], tmp_path: Path
) -> None:
    """Cause-specific proof: the *same* WAV opens when its demuxer is whitelisted and is
    refused when it is not, on both FFmpeg builds; a playlist is refused by the shipped list."""

    def run(path: Path, formats: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [
                engine, "-hide_banner", "-nostdin", "-protocol_whitelist", "file",
                "-format_whitelist", formats, "-i", str(path), "-t", "0.05", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip

    wav = media["tone.wav"]
    allowed = run(wav, "wav")
    refused = run(wav, "mov")
    assert allowed.returncode == 0 and "Input #0, wav" in allowed.stderr
    assert refused.returncode != 0 and "Input #0" not in refused.stderr
    # A probe-recognisable HLS playlist (TARGETDURATION) naming a local segment: FFmpeg
    # recognises the format, and only the whitelist stands between it and opening the file.
    segment = tmp_path / "seg.ts"
    segment.write_bytes(b"\x47" + b"\x00" * 187)
    playlist = tmp_path / "evil.m3u8"
    playlist.write_text(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{segment}\n#EXT-X-ENDLIST\n"
    )

    def verbose(formats: str | None) -> subprocess.CompletedProcess[str]:
        argv = [engine, "-hide_banner", "-nostdin", "-loglevel", "debug"]
        argv += ["-protocol_whitelist", "file"]
        if formats is not None:
            argv += ["-format_whitelist", formats]
        argv += ["-i", str(playlist), "-t", "0.05", "-f", "null", "-"]
        return subprocess.run(  # noqa: S603 - fixed argv
            argv, capture_output=True, text=True, timeout=20, check=False
        )

    control = verbose(None)  # no whitelist: the demuxer opens the named segment
    assert "Format hls probed" in control.stderr
    assert f"Opening '{segment}'" in control.stderr
    blocked = verbose(analyzer_module.FORMAT_WHITELIST)
    assert "Format hls probed" in blocked.stderr  # recognised, then refused by the whitelist
    assert "Format not on whitelist" in blocked.stderr
    assert f"Opening '{segment}'" not in blocked.stderr and blocked.returncode != 0
