"""Integration tests for :mod:`bulletproof_normalizer` using real ffmpeg.

ffprobe is exercised through a monkeypatched probe so the suite is
self-contained in this sandbox (the implementation still shells out to real
``ffprobe``/``ffmpeg`` by name per the specification).  Clips render into
``/tmp``; nothing is written into the repository.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.hyper_compiler import bulletproof_normalizer as bn
from nexus_ai_agent.creative.hyper_compiler.bulletproof_normalizer import (
    NormalizationError,
    NormalizedMedia,
    probe_media,
)
from nexus_ai_agent.creative.hyper_compiler.models import HDR, SDR, MediaStreamProbe

_IDENTITY = ((1.0, 0.0), (0.0, 1.0))


def _ffprobe_json(video, audio=True, duration="2.0"):
    streams = [video]
    if audio:
        streams.append({"codec_type": "audio"})
    return {"streams": streams, "format": {"duration": duration}}


def _run_stub(stdout_json, returncode=0):
    import types

    cp = types.SimpleNamespace()
    cp.returncode = returncode
    cp.stdout = stdout_json
    cp.stderr = ""
    return cp


def test_probe_media_parses_ffprobe_json(make_clip, monkeypatch):
    path = make_clip("probe.mp4", duration=2.0, width=320, height=180, has_audio=True)
    video = {
        "codec_type": "video",
        "width": 320,
        "height": 180,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "display_aspect_ratio": "16:9",
        "color_space": "bt709",
        "metadata": {},
        "side_data_list": [],
    }
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: _run_stub(__import__("json").dumps(_ffprobe_json(video)))
    )
    probe = probe_media(path)
    assert probe.width == 320 and probe.height == 180
    assert probe.has_audio is True
    assert probe.color_space == SDR
    assert probe.rotation_degrees == 0.0
    assert probe.is_variable_frame_rate is False
    assert probe.sha256.startswith("sha256:")


def test_probe_media_rotation_from_metadata(make_clip, monkeypatch):
    path = make_clip("probe.mp4", duration=2.0, width=320, height=180)
    video = {
        "codec_type": "video",
        "width": 320,
        "height": 180,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "display_aspect_ratio": "16:9",
        "color_space": "bt709",
        "metadata": {"rotate": "90"},
        "side_data_list": [],
    }
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: _run_stub(__import__("json").dumps(_ffprobe_json(video)))
    )
    probe = probe_media(path)
    assert abs(probe.rotation_degrees - 90.0) < 1e-6
    assert probe.needs_rotation is True


def test_probe_media_rotation_from_side_data(make_clip, monkeypatch):
    path = make_clip("probe.mp4", duration=2.0, width=320, height=180)
    # 90° clockwise display matrix (row-major int32 little-endian, 16.16 fixed
    # point): [[0, -1, 0],[1, 0, 0],[0, 0, 1]].
    import struct

    matrix_90 = b"".join(struct.pack("<i", v) for v in (0, -65536, 0, 65536, 0, 0, 0, 0, 65536))
    video = {
        "codec_type": "video",
        "width": 320,
        "height": 180,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "display_aspect_ratio": "16:9",
        "color_space": "bt709",
        "metadata": {},
        "side_data_list": [{"side_data_type": "Display Matrix", "displaymatrix": matrix_90.hex()}],
    }
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: _run_stub(__import__("json").dumps(_ffprobe_json(video)))
    )
    probe = probe_media(path)
    assert abs(probe.rotation_degrees - 90.0) < 1e-6
    assert probe.needs_rotation is True


def test_probe_media_hdr(make_clip, monkeypatch):
    path = make_clip("probe.mp4", duration=2.0, width=320, height=180)
    video = {
        "codec_type": "video",
        "width": 320,
        "height": 180,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "display_aspect_ratio": "16:9",
        "color_space": "bt2020nc",
        "metadata": {},
        "side_data_list": [],
    }
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: _run_stub(__import__("json").dumps(_ffprobe_json(video)))
    )
    probe = probe_media(path)
    assert probe.color_space == HDR


def test_normalize_standard_clip(make_clip, tmp_path, monkeypatch):
    path = make_clip("standard.mp4", duration=2.0, width=320, height=180, has_audio=True)
    normal = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    probes = iter([normal, normal])
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(probes))
    out = tmp_path / "out.mp4"
    result = bn.normalize_media(path, out)
    assert isinstance(result, NormalizedMedia) and out.exists()
    assert result.probe.rotation_degrees == 0.0
    assert result.probe.has_audio is True
    assert "fps=" not in result.filtergraph  # CFR → no fps resample


def test_normalize_injects_silence_when_no_audio(make_clip, tmp_path, monkeypatch):
    path = make_clip("silent.mp4", duration=2.0, width=320, height=180, has_audio=False)
    normal = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        False,
        "sha256:" + "0" * 64,
    )
    probes = iter([normal, normal])
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(probes))
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    out = tmp_path / "out.mp4"
    bn.normalize_media(path, out)
    assert out.exists()
    import subprocess

    info = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    ).stderr
    assert "Audio:" in info, info


def test_normalize_zeroes_rotation_metadata(make_clip, tmp_path, monkeypatch):
    path = make_clip("rotated.mp4", duration=2.0, width=320, height=180, has_audio=True)
    rotated = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        ((0.0, -1.0), (1.0, 0.0)),
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    normal = MediaStreamProbe(
        180,
        320,
        Fraction(30, 1),
        Fraction(30, 1),
        9 / 16,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    probes = iter([rotated, normal])
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(probes))
    out = tmp_path / "out.mp4"
    result = bn.normalize_media(path, out)
    assert result.probe.rotation_degrees == 0.0
    assert result.probe.needs_rotation is False


def test_normalize_atomic_no_temp_leftovers(make_clip, tmp_path, monkeypatch):
    path = make_clip("atomic.mp4", duration=1.5, has_audio=True)
    normal = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(iter([normal, normal])))
    before = {p.name for p in tmp_path.iterdir()}
    bn.normalize_media(path, tmp_path / "atomic_out.mp4")
    after = {p.name for p in tmp_path.iterdir()}
    assert not any("hyper_norm" in name for name in after - before)


def test_normalize_missing_input_raises(tmp_path):
    with pytest.raises(NormalizationError):
        bn.normalize_media(tmp_path / "missing.mp4")


def test_normalize_vfr_branch_adds_fps_filter(make_clip, tmp_path, monkeypatch):
    path = make_clip("vfr.mp4", duration=1.5, has_audio=True)
    vfr = MediaStreamProbe(
        320,
        180,
        Fraction(60, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    normal = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    probes = iter([vfr, normal])
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(probes))
    result = bn.normalize_media(path, tmp_path / "vfr_out.mp4")
    assert "fps=" in result.filtergraph, result.filtergraph


def test_normalize_cfr_branch_omits_fps_filter(make_clip, tmp_path, monkeypatch):
    path = make_clip("cfr.mp4", duration=1.5, has_audio=True)
    normal = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(iter([normal, normal])))
    result = bn.normalize_media(path, tmp_path / "cfr_out.mp4")
    assert "fps=" not in result.filtergraph, result.filtergraph
