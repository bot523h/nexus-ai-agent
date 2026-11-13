"""Stage-B B1: executable regressions for workspace and tool-parser audit debt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    AudioMeasurementError,
    AudioProbeError,
    FfmpegTranscoder,
    _parse_float,
    parse_duration_us,
    parse_ebur128_summary,
)
from nexus_ai_agent.creative.audio_engine.models import MediaProbe, ProcessOutcome
from nexus_ai_agent.creative.audio_engine.process import CancelToken
from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace, WorkspaceBoundaryError
from nexus_ai_agent.tools.filesystem_policy import ContentLimitExceeded, FilesystemBoundaryError


@pytest.mark.parametrize("limit", [True, False])
def test_stage_input_specific_content_limit_handler_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: bool
) -> None:
    workspace = RuntimeWorkspace(tmp_path / "work").prepare()
    source = tmp_path / "source.wav"
    source.write_bytes(b"immutable input")
    error = ContentLimitExceeded("grew") if limit else FilesystemBoundaryError("disk refusal")

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(workspace.filesystem, "write_stream", refuse)
    with pytest.raises(WorkspaceBoundaryError) as raised:
        workspace.stage_input(source)
    assert raised.value.__cause__ is error
    assert str(raised.value) == (
        "input grew while it was being staged: grew" if limit else "disk refusal"
    )
    assert source.read_bytes() == b"immutable input"
    assert list(workspace.inputs_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Duration: 00:01:02.123456, start: 0", 62_123_456),
        ("DURATION = 123:59:59.999999", 446_399_999_999),
        ("duration=1.125\n", 1_125_000),
        ("duration_us=125001\n", 125_001),
        ("TAG:duration=1.25e1\n", 12_500_000),
        ('{"format":{"duration":"2.75"}}', 2_750_000),
        ('{"streams":[{"duration":"3.25"}]}', 3_250_000),
        ('{"streams":[{"duration_ts":48001,"time_base":"1/48000"}]}', 1_000_021),
        ('{"streams":[{"duration_ts":1,"time_base":"0/48000","duration":2}]}', 2_000_000),
        ('{"streams":[{"duration_ts":"x","time_base":"1/0","duration":2}]}', 2_000_000),
        ('{"streams":[1,{"codec_type":"video","duration":9}],"duration":2}', 2_000_000),
        ('warning {bad}\n{"streams":null,"format":{"duration":1}} trailing', 1_000_000),
        ('{"duration":true}', None),
        ('{"duration":"nan"}', None),
        ('{"duration":-1}', None),
        ('{"format":null}', None),
        ('{"duration":{}}', None),
        ("Duration: N/A\nout_time=00:01:10.00", None),
        ("Duration: 00:99:00", None),
        ("duration=-1\nduration=nan", None),
        ("no duration", None),
    ],
)
def test_duration_parser_families(text: str, expected: int | None) -> None:
    assert parse_duration_us(text) == expected


@pytest.mark.parametrize("raw", ["nan", "-inf", "inf", "1e999", "garbage", ""])
def test_nonfinite_or_invalid_parser_values_are_not_measurements(raw: str) -> None:
    assert _parse_float(raw) is None


LEGACY_SUMMARY = """Summary:
  Integrated loudness:
    I: -24.0 LUFS
    Threshold: -34.0 LUFS
  Loudness range:
    LRA: 1.5 LU
    Threshold: -44.0 LUFS
  True peak:
    Peak: -2.0 dBFS
"""


@pytest.mark.parametrize(
    "text",
    [
        LEGACY_SUMMARY,
        LEGACY_SUMMARY.replace("dBFS", "dBTP"),
        "summary:\nIntegrated Loudness = -24.0 LKFS\nLoudness Range = 1.5 LU\n"
        "Relative Threshold: -34 LUFS\nTrue-Peak: -2 dBTP",
        "Programme loudness: -2.40e1 LUFS\nLRA=1.5 LU\nTP=-2 dBTP\nThreshold=-34 LUFS",
        '{"ebur128":{"integrated_lufs":-24,"true_peak_dbtp":-2,'
        '"loudness_range_lu":1.5,"relative_threshold_lufs":-34}}',
        "lavfi.r128.I=-22\nlavfi.r128.I=-24\nlavfi.r128.LRA=1.5\n"
        "lavfi.r128.I_threshold=-34\nlavfi.r128.true_peak=0.7943282347242815",
    ],
)
def test_ebur128_summary_families_keep_units_and_gate_semantics(text: str) -> None:
    measurement = parse_ebur128_summary(text)
    assert measurement.integrated_lufs == -24.0
    assert measurement.true_peak_dbtp == pytest.approx(-2.0, abs=1e-12)
    assert measurement.loudness_range_lu == 1.5
    assert measurement.relative_threshold_lufs == -34.0


def test_summary_uses_last_programme_and_negative_infinity_means_silence() -> None:
    measured = parse_ebur128_summary(LEGACY_SUMMARY.replace("-24.0", "-12.0") + LEGACY_SUMMARY)
    assert measured.integrated_lufs == -24.0
    silent = parse_ebur128_summary("lavfi.r128.I=-inf\nlavfi.r128.true_peak=0")
    assert silent.integrated_lufs is None and silent.true_peak_dbtp is None


@pytest.mark.parametrize(
    "text",
    [
        "no summary",
        "Summary:\n LRA: 1 LU",
        "I:-24 LUFS",
        "Peak:-2 dBFS",
        '{"integrated_lufs":"nan","true_peak_dbtp":-1}',
        '{"integrated_lufs":-24,"true_peak_dbtp":"inf"}',
        '{"integrated_lufs":true,"true_peak_dbtp":-1}',
        '{"input_i":-24,"input_tp":-1}',  # loudnorm is not the independent scanner
    ],
)
def test_unmeasurable_summary_refuses_instead_of_silent_none(text: str) -> None:
    with pytest.raises(AudioMeasurementError):
        parse_ebur128_summary(text)


def test_probe_prefers_optional_json_and_never_hides_its_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    engine = FfmpegTranscoder(probe_binary="/optional/ffprobe")
    expected = MediaProbe(size_bytes=6, sha256="sha256:" + "0" * 64, source="ffprobe_json")
    cancel = CancelToken()
    seen = []

    def machine(path, *, cancel=None):
        seen.append((path, cancel))
        return expected

    monkeypatch.setattr(engine, "probe_json", machine)
    assert engine.probe(source, cancel=cancel) is expected
    assert seen == [(source, cancel)]

    def malformed(*args, **kwargs):
        raise AudioProbeError("missing machine evidence")

    monkeypatch.setattr(engine, "probe_json", malformed)
    with pytest.raises(AudioProbeError, match="machine evidence"):
        engine.probe(source)


def test_probe_without_ffprobe_uses_version_independent_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    engine = FfmpegTranscoder()
    engine._probe_binary = ""
    banner = "duration=2.25\nStream #0:0: Audio: pcm_s16le, 48000 Hz, 2 channels, s16"
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *a, **k: ProcessOutcome(
            argv=("ffmpeg",),
            returncode=1,
            termination="exited",
            elapsed_seconds=0,
            stderr_tail=banner,
        ),
    )
    probe = engine.probe(source)
    assert probe.container_duration_us == 2_250_000
    assert (probe.channels, probe.sample_rate) == (2, 48_000)
    assert probe.source == "banner"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"duration_ts": None, "duration": "1.25"}, 1_250_000),
        ({"duration_ts": None}, None),
        ({"duration_ts": 0, "format": {"duration": "nan"}}, None),
        ({"duration_ts": 0, "format": {"duration": -1}}, None),
        ({"duration_ts": 0, "format": {"duration": True}}, None),
        ({"streams": [1]}, None),
    ],
)
def test_json_duration_variants_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict, expected: int | None
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    engine = FfmpegTranscoder(probe_binary="/optional/ffprobe")
    stream = dict(
        codec_type="audio",
        codec_name="flac",
        sample_rate="48000",
        channels=2,
        sample_fmt="s32",
        time_base="1/48000",
        duration_ts=0,
    )
    stream.update({k: v for k, v in overrides.items() if k not in ("format", "streams")})
    if stream["duration_ts"] is None:
        del stream["duration_ts"]
    document = {
        "streams": overrides.get("streams", [stream]),
        "format": overrides.get("format", {}),
    }
    monkeypatch.setattr(
        engine,
        "_run",
        lambda *a, **k: ProcessOutcome(
            argv=("ffprobe",),
            returncode=0,
            termination="exited",
            elapsed_seconds=0,
            stdout_tail=json.dumps(document),
        ),
    )
    if expected is None:
        with pytest.raises(AudioProbeError):
            engine.probe(source)
    else:
        assert engine.probe(source).container_duration_us == expected


def test_continuity_assembly_names_do_not_hide_an_under_95_percent_module() -> None:
    from nexus_ai_agent.creative.audio_engine.continuity import assembly_names

    assert assembly_names((Path("private/shot-a.flac"), "other/shot-b.flac")) == (
        "shot-a.flac",
        "shot-b.flac",
    )
    assert assembly_names(()) == ()
