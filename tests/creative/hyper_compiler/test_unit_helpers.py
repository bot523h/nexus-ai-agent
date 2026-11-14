"""Unit tests for pure helper functions across the hyper-compiler modules.

These tests exercise the mathematical and parsing helpers directly (no ffmpeg
required for most), covering the branches the integration tests miss so line
and branch coverage stay above 95%.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from unittest import mock

import numpy as np
import pytest

from nexus_ai_agent.creative.hyper_compiler import (
    bulletproof_normalizer as bn,
)
from nexus_ai_agent.creative.hyper_compiler import (
    differential_filtergraph_compiler as dc,
)
from nexus_ai_agent.creative.hyper_compiler import (
    optical_spectral_analyzer as osa,
)
from nexus_ai_agent.creative.hyper_compiler.bulletproof_normalizer import (
    NormalizationError,
    probe_media,
)
from nexus_ai_agent.creative.hyper_compiler.differential_filtergraph_compiler import (
    CompileError,
    GradeParams,
    SegmentCASRenderer,
)
from nexus_ai_agent.creative.hyper_compiler.models import (
    HDR,
    SDR,
    MediaStreamProbe,
    TimelineDAG,
    TimelineDifferentialNode,
    canonical_params_hash,
)

_IDENTITY = ((1.0, 0.0), (0.0, 1.0))


# --- bulletproof_normalizer parsers -------------------------------------------


def test_parse_rate_variants():
    assert bn._parse_rate("30/1") == Fraction(30, 1)
    assert bn._parse_rate("60000/1001") == Fraction(60000, 1001)
    assert bn._parse_rate("30") == Fraction(30, 1)
    for bad in (None, "", "0/0", "0", "N/A", "none", "not-a-rate", "/"):
        assert bn._parse_rate(bad) == Fraction(0, 1)
    # den == 0 is coerced to 1 by ``int(den) or 1``.
    assert bn._parse_rate("1/0") == Fraction(1, 1)


def test_rotation_from_matrix():
    assert bn._rotation_from_matrix(_IDENTITY) == 0.0
    m90 = ((0.0, -1.0), (1.0, 0.0))
    assert abs(bn._rotation_from_matrix(m90) - 90.0) < 1e-6
    m180 = ((-1.0, 0.0), (0.0, -1.0))
    assert abs(bn._rotation_from_matrix(m180) - 180.0) < 1e-6
    m270 = ((0.0, 1.0), (-1.0, 0.0))
    assert abs(bn._rotation_from_matrix(m270) - 270.0) < 1e-6


def test_nearest_supported_rotation():
    assert bn._nearest_supported_rotation(0.0) is None
    assert bn._nearest_supported_rotation(360.0) is None
    assert bn._nearest_supported_rotation(359.9) is None
    assert bn._nearest_supported_rotation(90.0) == 90.0
    assert bn._nearest_supported_rotation(180.0) == 180.0
    assert bn._nearest_supported_rotation(270.0) == 270.0
    # Near-but-not-supported angles fall through.
    assert bn._nearest_supported_rotation(45.0) is None
    assert bn._nearest_supported_rotation(100.0) is None


def test_color_space():
    assert bn._color_space({"color_space": "bt709"}) == SDR
    assert bn._color_space({"color_space_name": "bt709"}) == SDR
    assert bn._color_space({"color_space": "bt2020nc"}) == HDR
    assert bn._color_space({"color_transfer": "smpte2084"}) == HDR
    assert bn._color_space({"color_primaries": "smpte2084"}) == HDR
    assert bn._color_space({"color_transfer": "hlg"}) == HDR  # HLG marker
    assert bn._color_space({}) == SDR


def test_parse_aspect_ratio():
    assert bn._parse_aspect_ratio("16:9", 320, 180) == pytest.approx(16 / 9)
    assert bn._parse_aspect_ratio("1.7778", 320, 180) == pytest.approx(1.7778)
    assert bn._parse_aspect_ratio(None, 320, 180) == pytest.approx(16 / 9)
    assert bn._parse_aspect_ratio("garbage", 320, 180) == pytest.approx(16 / 9)
    assert bn._parse_aspect_ratio(None, 0, 0) == 1.0
    assert bn._parse_aspect_ratio(None, 0, 180) == 1.0


def test_transpose_filter():
    assert bn._transpose_filter(90.0) == "transpose=1"
    assert bn._transpose_filter(180.0) == "hflip,vflip"
    assert bn._transpose_filter(270.0) == "transpose=2"
    with pytest.raises(NormalizationError):
        bn._transpose_filter(45.0)


def test_fps_target():
    probe = MediaStreamProbe(
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
    assert bn._fps_target(probe) == 30.0  # avg used
    probe = MediaStreamProbe(
        320,
        180,
        Fraction(60, 1),
        Fraction(0, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    assert bn._fps_target(probe) == 60.0  # falls back to r_frame_rate
    probe = MediaStreamProbe(
        320,
        180,
        Fraction(0, 1),
        Fraction(0, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    assert bn._fps_target(probe) == 30.0  # default


def test_is_variable_frame_rate():
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
    assert bn._is_variable_frame_rate(vfr) is True
    cfr = MediaStreamProbe(
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
    assert bn._is_variable_frame_rate(cfr) is False
    no_avg = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(0, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    assert bn._is_variable_frame_rate(no_avg) is False


# --- probe_media error paths ----------------------------------------------


def _stub(cmd_stdout_json: str, returncode: int = 0):
    import types

    cp = types.SimpleNamespace()
    cp.returncode = returncode
    cp.stdout = cmd_stdout_json
    cp.stderr = ""
    return cp


def test_probe_media_ffprobe_missing(monkeypatch):
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("no ffprobe"))
    )
    with pytest.raises(NormalizationError):
        probe_media("x.mp4")


def test_probe_media_ffprobe_failed(monkeypatch):
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("{}", returncode=1))
    with pytest.raises(NormalizationError):
        probe_media("x.mp4")


def test_probe_media_bad_json(monkeypatch):
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("{not json", returncode=0))
    with pytest.raises(NormalizationError):
        probe_media("x.mp4")


def test_probe_media_no_video(monkeypatch):
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub('{"streams": [], "format": {}}'))
    with pytest.raises(NormalizationError):
        probe_media("x.mp4")


def test_probe_metadata_rotation_bad_value(tmp_path, monkeypatch):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"")
    video = {
        "codec_type": "video",
        "width": 320,
        "height": 180,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "display_aspect_ratio": "16:9",
        "color_space": "bt709",
        "metadata": {"rotate": "not-a-number"},
        "side_data_list": [],
    }
    monkeypatch.setattr(
        bn,
        "_run",
        lambda cmd, **kw: _stub(
            __import__("json").dumps({"streams": [video], "format": {"duration": "2.0"}})
        ),
    )
    probe = probe_media(p)
    assert probe.rotation_degrees == 0.0


# --------------------------------------------------------------------------- # models


def test_is_so2():
    from nexus_ai_agent.creative.hyper_compiler.models import _is_so2

    assert _is_so2(_IDENTITY) is True
    assert _is_so2(((0.0, -1.0), (1.0, 0.0))) is True  # 90 deg
    assert _is_so2(((2.0, 0.0), (0.0, 2.0))) is False  # not unit
    assert _is_so2(((1.0, 0.0), (0.0, 2.0))) is False  # not orthogonal


def test_validate_sha256():
    from nexus_ai_agent.creative.hyper_compiler.models import _validate_sha256

    assert _validate_sha256("sha256:" + "0" * 64).startswith("sha256:")
    for bad in ("sha256:short", "not-sha", "sha256:" + "g" * 64, 123, ""):
        with pytest.raises(ValueError):
            _validate_sha256(bad)


def test_media_stream_probe_validation():
    with pytest.raises(ValueError):
        MediaStreamProbe(
            0,
            180,
            Fraction(30, 1),
            Fraction(30, 1),
            16 / 9,
            _IDENTITY,
            SDR,
            True,
            "sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError):
        MediaStreamProbe(
            320,
            180,
            Fraction(-1, 1),
            Fraction(30, 1),
            16 / 9,
            _IDENTITY,
            SDR,
            True,
            "sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError):
        MediaStreamProbe(
            320,
            180,
            Fraction(30, 1),
            Fraction(30, 1),
            0.0,
            _IDENTITY,
            SDR,
            True,
            "sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError):
        MediaStreamProbe(
            320,
            180,
            Fraction(30, 1),
            Fraction(30, 1),
            16 / 9,
            ((2.0, 0.0), (0.0, 2.0)),
            SDR,
            True,
            "sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError):
        MediaStreamProbe(
            320, 180, Fraction(30, 1), Fraction(30, 1), 16 / 9, _IDENTITY, SDR, True, "not-a-sha"
        )


def test_media_stream_probe_is_variable_frame_rate():
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
    assert vfr.is_variable_frame_rate is True
    cfr = MediaStreamProbe(
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
    assert cfr.is_variable_frame_rate is False
    no_avg = MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(0, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        True,
        "sha256:" + "0" * 64,
    )
    assert no_avg.is_variable_frame_rate is False


def test_media_stream_probe_needs_rotation():
    static = MediaStreamProbe(
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
    assert static.needs_rotation is False
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
    assert rotated.needs_rotation is True


def test_timeline_node_validation():
    base = dict(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    with pytest.raises(ValueError):
        TimelineDifferentialNode(**{**base, "end_us": 0})  # end <= start
    with pytest.raises(ValueError):
        TimelineDifferentialNode(**{**base, "input_sha256": "bad"})
    with pytest.raises(ValueError):
        TimelineDifferentialNode(**{**base, "oklab_matrix": ((1.0, 0), (0, 1))})  # not 3x3


def test_timeline_node_speed_at():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    assert node.speed_at(0.0) == 1.0
    assert node.speed_at(1.0) == 1.0
    assert node.speed_at(0.5) == 1.0
    assert node.speed_at(-5.0) == node.speed_at(0.0)  # clamped
    assert node.speed_at(5.0) == node.speed_at(1.0)  # clamped
    # dx ~ 0 -> 1.0
    flat = TimelineDifferentialNode(
        node_id="n2",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    assert flat.speed_at(0.5) == 1.0


def test_timeline_node_phase_integral():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    assert node.phase_integral(0.0) == 0.0
    assert node.phase_integral(-1.0) == 0.0
    assert node.duration_us == 1_000_000


def test_timeline_dag_validation():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    with pytest.raises(ValueError):
        TimelineDAG((node,), 0, Fraction(30, 1), 320, 180)
    with pytest.raises(ValueError):
        TimelineDAG((node,), 1_000_000, Fraction(0, 1), 320, 180)
    with pytest.raises(ValueError):
        TimelineDAG((node,), 1_000_000, Fraction(30, 1), 0, 180)


def test_canonical_params_hash():
    h1 = canonical_params_hash({"a": 1, "b": [1, 2]})
    h2 = canonical_params_hash({"b": [1, 2], "a": 1})  # key order shouldn't matter
    assert h1 == h2
    assert h1.startswith("sha256:")
    with pytest.raises((ValueError, TypeError)):
        canonical_params_hash(float("nan"))


# --- optical_spectral_analyzer --------------------------------------------


def test_srgb_to_linear():
    # low branch (<= 0.04045)
    assert osa.srgb_to_linear(np.array(0.0)) == 0.0
    assert osa.srgb_to_linear(np.array(0.04)) == pytest.approx(0.04 / 12.92)
    # high branch
    assert osa.srgb_to_linear(np.array(1.0)) == 1.0
    arr = np.array([0.0, 0.5, 1.0])
    out = osa.srgb_to_linear(arr)
    assert out.shape == arr.shape


def test_rgb_to_oklab():
    black = osa.rgb_to_oklab(np.zeros((1, 1, 3)))
    assert black.shape == (1, 1, 3)
    white = osa.rgb_to_oklab(np.ones((1, 1, 3)))
    assert white.shape == (1, 1, 3)


def test_unit_vector():
    assert np.allclose(osa._unit_vector(np.array([0.0, 0.0])), 0.0)
    assert np.allclose(osa._unit_vector(np.array([1.0, 2.0])), np.array([1 / 3, 2 / 3]))


def test_oklab_histogram_and_jsd():
    oklab = np.full((4, 4, 3), 0.5)
    h = osa.oklab_histogram(oklab)
    assert h.shape == (8 * 8 * 8,)
    assert abs(h.sum() - 1.0) < 1e-9
    p = np.ones(8) / 8
    q = np.ones(8) / 8
    assert abs(osa.jensen_shannon_divergence(p, q)) < 1e-9
    # identical distributions -> 0
    assert abs(osa.jensen_shannon_divergence(h, h)) < 1e-9


def test_difference_matrix():
    assert osa._difference_matrix(2, 1).shape == (0, 1)  # size <= order
    d1 = osa._difference_matrix(1, 4)
    assert d1.shape == (3, 4)
    assert d1[0, 0] == 1.0 and d1[0, 1] == -1.0


def test_smooth_trajectory():
    pts = np.array([[0.0], [1.0], [2.0], [3.0]])
    out = osa.smooth_trajectory(pts)
    assert out.shape == pts.shape
    # lambda <= 0 skipped
    out0 = osa.smooth_trajectory(pts, lambda_1=0.0, lambda_2=0.0, lambda_3=0.0)
    assert np.allclose(out0, pts)
    # empty -> copy
    empty = np.zeros((0, 2))
    assert np.allclose(osa.smooth_trajectory(empty), empty)
    # ndim != 2 -> ValueError
    with pytest.raises(ValueError):
        osa.smooth_trajectory(np.zeros(5))


def test_analyze_audio_empty():
    with mock.patch.object(osa, "_extract_audio_mono", return_value=np.zeros(0)):
        assert osa.analyze_audio_silence_speech("x.mp4") == []


def test_analyze_audio_silence_only():
    # All-silence audio at 16 kHz, 0.1 s -> one window of silence.
    samples = np.zeros(1600, dtype=np.float64)
    with mock.patch.object(osa, "_extract_audio_mono", return_value=samples):
        segs = osa.analyze_audio_silence_speech("x.mp4", silence_dbfs=-40.0, window_sec=0.025)
    assert len(segs) == 1
    assert segs[0].is_speech is False


def test_analyze_audio_speech_only():
    samples = np.full(3200, 0.5, dtype=np.float64)  # loud -> speech
    with mock.patch.object(osa, "_extract_audio_mono", return_value=samples):
        segs = osa.analyze_audio_silence_speech("x.mp4", window_sec=0.025)
    assert len(segs) == 1
    assert segs[0].is_speech is True


# --- differential_filtergraph_compiler -------------------------------------


def test_grade_params_validation():
    assert GradeParams().temperature_k == 6500.0
    with pytest.raises(ValueError):
        GradeParams(temperature_k=500)
    with pytest.raises(ValueError):
        GradeParams(temperature_k=50000)
    with pytest.raises(ValueError):
        GradeParams(contrast=0.0)
    with pytest.raises(ValueError):
        GradeParams(contrast=-1.0)


def test_generate_oklab_3dlut_cube_file(tmp_path):
    out = tmp_path / "lut.cube"
    dc.generate_oklab_3dlut_cube_file(GradeParams(), out)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("LUT_3D_SIZE 17")
    lines = text.splitlines()
    assert len(lines) == 1 + 17**3  # header + 4913 triplets


def test_escape_drawtext():
    assert bn is not None  # ensure import exercised
    escaped = dc._escape_drawtext("a:b'c\\d,%")
    assert ":" not in escaped.replace("\\:", "")
    assert "\\:" in escaped


def test_speed_ramp_lines():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    lines, out_label = dc._speed_ramp_lines(0, "[vbase0]", 0, 1_000_000, node)
    assert out_label == "vcat0"
    assert any("concat" in line for line in lines)
    assert all("trim" in line for line in lines[:8])


def test_compile_error_no_input():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    dag = TimelineDAG((node,), 1_000_000, Fraction(30, 1), 320, 180)
    with pytest.raises(CompileError):
        dc.compile_filtergraph(dag, {})  # no main input for n1


def test_segment_key_determinism():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    r1 = SegmentCASRenderer.__new__(SegmentCASRenderer)
    k1 = r1.segment_key("sha256:aaa", node)
    k2 = r1.segment_key("sha256:aaa", node)
    assert k1 == k2
    assert k1.startswith("sha256:")
    node2 = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=2_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    assert r1.segment_key("sha256:aaa", node2) != k1


def test_oklab_matrices_invertible():
    assert np.allclose(osa._M2 @ np.linalg.inv(osa._M2), np.eye(3), atol=1e-9)
    assert np.allclose(osa._M1 @ np.linalg.inv(osa._M1), np.eye(3), atol=1e-9)


# --- bulletproof_normalizer display-matrix parsing ------------------------


def test_parse_displaymatrix_bad_inputs():
    # non-hex characters -> None
    assert bn._parse_displaymatrix("zz" * 36) is None
    # too short -> None
    assert bn._parse_displaymatrix("deadbeef") is None
    # not a string or list -> None
    assert bn._parse_displaymatrix(123) is None
    # list with non-numeric entry -> ValueError inside -> None
    assert bn._parse_displaymatrix(["a", "b", "c", "d", "e", "f", "g", "h", "i"]) is None
    # valid list of 9 ints -> matrix
    m = bn._parse_displaymatrix([65536, 0, 0, 0, 65536, 0, 0, 0, 65536])
    assert m == _IDENTITY


def test_rotation_matrix_from_side_data_variants():
    identity = ((1.0, 0.0), (0.0, 1.0))
    # not a list at all -> identity (early return)
    assert bn._rotation_matrix_from_side_data(None) == identity
    assert bn._rotation_matrix_from_side_data("not-a-list") == identity
    # list containing a non-dict -> skipped
    assert bn._rotation_matrix_from_side_data([42, {}]) == identity
    # dict with wrong side_data_type -> skipped
    assert (
        bn._rotation_matrix_from_side_data(
            [{"side_data_type": "Something Else", "data": "deadbeef" * 9}]
        )
        == identity
    )
    # dict with raw None -> skipped
    assert (
        bn._rotation_matrix_from_side_data(
            [{"side_data_type": "Display Matrix", "displaymatrix": None}]
        )
        == identity
    )
    # valid -> matrix
    matrix_90 = b"".join(
        __import__("struct").pack("<i", v) for v in (0, -65536, 0, 65536, 0, 0, 0, 0, 65536)
    )
    m = bn._rotation_matrix_from_side_data(
        [{"side_data_type": "Display Matrix", "displaymatrix": matrix_90.hex()}]
    )
    assert abs(bn._rotation_from_matrix(m) - 90.0) < 1e-6


def test_probe_media_timeout(monkeypatch, tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"")
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 30))
    )
    with pytest.raises(NormalizationError):
        probe_media(p)


def test_read_duration_error_paths(monkeypatch):
    # non-zero returncode -> 0.0
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("{}", returncode=1))
    assert bn._read_duration("x.mp4") == 0.0
    # timeout -> 0.0
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 30))
    )
    assert bn._read_duration("x.mp4") == 0.0
    # bad JSON -> 0.0
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("{bad", returncode=0))
    assert bn._read_duration("x.mp4") == 0.0


# --- normalize_media branches ---------------------------------------------


def _normal_probe(has_audio=True):
    return MediaStreamProbe(
        320,
        180,
        Fraction(30, 1),
        Fraction(30, 1),
        16 / 9,
        _IDENTITY,
        SDR,
        has_audio,
        "sha256:" + "0" * 64,
    )


def test_normalize_output_path_defaults(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=True)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("", returncode=0))
    result = bn.normalize_media(path)  # output_path None
    assert result.output_path.endswith(".normalized.mp4")


def test_normalize_silence_zero_duration(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=False)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 0.0)
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("", returncode=0))
    result = bn.normalize_media(path, tmp_path / "out.mp4")
    assert result.output_path == str(tmp_path / "out.mp4")
    assert "anullsrc" in result.filtergraph


def test_normalize_ffmpeg_timeout(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=True)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    monkeypatch.setattr(
        bn, "_run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 600))
    )
    with pytest.raises(NormalizationError):
        bn.normalize_media(path, tmp_path / "out.mp4")


def test_normalize_ffmpeg_oserror(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=True)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(NormalizationError):
        bn.normalize_media(path, tmp_path / "out.mp4")


def test_normalize_ffmpeg_failed(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=True)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("", returncode=1))
    with pytest.raises(NormalizationError):
        bn.normalize_media(path, tmp_path / "out.mp4")


def test_normalize_os_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"")
    normal = _normal_probe(has_audio=True)
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: normal)
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    monkeypatch.setattr(bn, "_run", lambda cmd, **kw: _stub("", returncode=0))
    monkeypatch.setattr(bn.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        bn.normalize_media(path, tmp_path / "out.mp4")


# --- optical_spectral_analyzer remaining branches -------------------------


def test_detect_shot_boundaries_few_frames():
    assert osa.detect_shot_boundaries([]) == []
    single = np.zeros((osa.PROXY_HEIGHT, osa.PROXY_WIDTH, 3))
    assert osa.detect_shot_boundaries([single]) == []


def test_extract_audio_mono_no_chunks(monkeypatch):
    class FakeProc:
        stdout = None
        stderr = None

        def __init__(self, *a, **k):
            self.stdout = FakeIO()
            self.stderr = FakeIO()

        def wait(self, timeout=60):
            pass

    class FakeIO:
        def read(self, n):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    out = osa._extract_audio_mono("x.mp4")
    assert out.size == 0


def test_analyze_audio_silence_to_speech_transition():
    # two windows: first silence, second loud -> one transition segment pair.
    samples = np.concatenate([np.zeros(1600), np.full(1600, 0.5)])
    with mock.patch.object(osa, "_extract_audio_mono", return_value=samples):
        segs = osa.analyze_audio_silence_speech("x.mp4", window_sec=0.025)
    assert len(segs) == 2
    assert segs[0].is_speech is False
    assert segs[1].is_speech is True


# --- differential_filtergraph_compiler branches ----------------------------


def test_grade_oklab_film_strength():
    from nexus_ai_agent.creative.hyper_compiler.differential_filtergraph_compiler import (
        _grade_oklab,
    )

    oklab = np.full((2, 2, 3), 0.7)
    base = _grade_oklab(oklab, GradeParams(film_strength=0.0))
    graded = _grade_oklab(oklab, GradeParams(film_strength=0.5))
    assert not np.allclose(base, graded)


def test_compile_lut_path_provided(tmp_path):
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    dag = TimelineDAG((node,), 1_000_000, Fraction(30, 1), 320, 180)
    lut = tmp_path / "custom.cube"
    lut.write_text("LUT_3D_SIZE 17\n")
    compiled = dc.compile_filtergraph(dag, {"n1": "in.mp4"}, lut_path=lut)
    assert compiled.filtergraph


def test_compile_zoom_box_and_layers(tmp_path):
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
        zoom_box=(0.1, 0.1, 0.5, 0.5),
        image_layers=[str(tmp_path / "layer.png")],
        audio_layers=[str(tmp_path / "layer.wav")],
    )
    (tmp_path / "layer.png").write_bytes(b"")
    (tmp_path / "layer.wav").write_bytes(b"")
    dag = TimelineDAG((node,), 1_000_000, Fraction(30, 1), 320, 180)
    compiled = dc.compile_filtergraph(dag, {"n1": "in.mp4"})
    assert "crop" in compiled.filtergraph
    assert "overlay" in compiled.filtergraph
    assert "amix" in compiled.filtergraph


def test_compile_multi_node_concat():
    node_a = TimelineDifferentialNode(
        node_id="a",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    node_b = TimelineDifferentialNode(
        node_id="b",
        input_sha256="sha256:" + "0" * 64,
        start_us=1_000_000,
        end_us=2_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    dag = TimelineDAG((node_a, node_b), 2_000_000, Fraction(30, 1), 320, 180)
    compiled = dc.compile_filtergraph(dag, {"a": "a.mp4", "b": "b.mp4"})
    assert "concat" in compiled.filtergraph
    assert compiled.video_out == "vout"
    assert compiled.audio_out == "aout"


def test_compile_empty_dag_audio_none():
    dag = TimelineDAG((), 1_000_000, Fraction(30, 1), 320, 180)
    compiled = dc.compile_filtergraph(dag, {})
    assert compiled.video_out is None
    assert compiled.audio_out is None


# --- SegmentCASRenderer caching & failures ----------------------------------


def _make_segment_node(duration_us=1_000_000):
    return TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "0" * 64,
        start_us=0,
        end_us=duration_us,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0, 0), (0, 1, 0), (0, 0, 1)),
    )


def test_render_segment_cache_hit(tmp_path):
    renderer = SegmentCASRenderer(work_dir=tmp_path / "seg")
    node = _make_segment_node()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake-video")
    # monkeypatch compile_filtergraph + subprocess to fake a successful render
    fake_path = tmp_path / "seg" / "segment.mp4"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"fake-segment")
    with (
        mock.patch.object(dc, "compile_filtergraph") as cf,
        mock.patch.object(dc.subprocess, "run") as run,
    ):
        cf.return_value = dc.CompiledFiltergraph(
            filtergraph="",
            inputs=(str(clip),),
            video_out="vout",
            audio_out="aout",
            output_args=(),
            duration_us=1_000_000,
        )
        run.return_value = mock.Mock(returncode=0)
        first = renderer.render_segment(
            input_path=str(clip),
            input_sha256="sha256:" + "0" * 64,
            node=node,
            grade=GradeParams(),
        )
        second = renderer.render_segment(
            input_path=str(clip),
            input_sha256="sha256:" + "0" * 64,
            node=node,
            grade=GradeParams(),
        )
    assert first == second  # cache hit, same path returned
    assert cf.call_count == 1  # compiled only once


def test_render_segment_timeout(tmp_path, monkeypatch):
    renderer = SegmentCASRenderer(work_dir=tmp_path / "seg")
    node = _make_segment_node()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with (
        mock.patch.object(dc, "compile_filtergraph") as cf,
        mock.patch.object(dc.subprocess, "run") as run,
    ):
        cf.return_value = dc.CompiledFiltergraph(
            filtergraph="",
            inputs=(str(clip),),
            video_out="vout",
            audio_out="aout",
            output_args=(),
            duration_us=1_000_000,
        )
        run.side_effect = subprocess.TimeoutExpired("ffmpeg", 600)
        with pytest.raises(CompileError):
            renderer.render_segment(
                input_path=str(clip),
                input_sha256="sha256:" + "0" * 64,
                node=node,
                grade=GradeParams(),
            )


def test_render_segment_oserror(tmp_path):
    renderer = SegmentCASRenderer(work_dir=tmp_path / "seg")
    node = _make_segment_node()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with (
        mock.patch.object(dc, "compile_filtergraph") as cf,
        mock.patch.object(dc.subprocess, "run") as run,
    ):
        cf.return_value = dc.CompiledFiltergraph(
            filtergraph="",
            inputs=(str(clip),),
            video_out="vout",
            audio_out="aout",
            output_args=(),
            duration_us=1_000_000,
        )
        run.side_effect = OSError("boom")
        with pytest.raises(CompileError):
            renderer.render_segment(
                input_path=str(clip),
                input_sha256="sha256:" + "0" * 64,
                node=node,
                grade=GradeParams(),
            )


def test_render_segment_failed_returncode(tmp_path):
    renderer = SegmentCASRenderer(work_dir=tmp_path / "seg")
    node = _make_segment_node()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with (
        mock.patch.object(dc, "compile_filtergraph") as cf,
        mock.patch.object(dc.subprocess, "run") as run,
    ):
        cf.return_value = dc.CompiledFiltergraph(
            filtergraph="",
            inputs=(str(clip),),
            video_out="vout",
            audio_out="aout",
            output_args=(),
            duration_us=1_000_000,
        )
        run.return_value = mock.Mock(returncode=1, stderr="boom", stdout="")
        with pytest.raises(CompileError):
            renderer.render_segment(
                input_path=str(clip),
                input_sha256="sha256:" + "0" * 64,
                node=node,
                grade=GradeParams(),
            )


def test_render_timeline_concat_failure(tmp_path):
    renderer = SegmentCASRenderer(work_dir=tmp_path / "seg")
    node = _make_segment_node()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    seg_path = tmp_path / "seg" / "segment.mp4"
    seg_path.parent.mkdir(parents=True, exist_ok=True)
    seg_path.write_bytes(b"fake-segment")
    with (
        mock.patch.object(renderer, "render_segment", return_value=seg_path),
        mock.patch.object(dc.subprocess, "run") as run,
    ):
        run.return_value = mock.Mock(returncode=1, stderr="concat boom", stdout="")
        with pytest.raises(CompileError):
            renderer.render_timeline(
                nodes_with_inputs=[(node, str(clip), "sha256:" + "0" * 64)],
                grade=GradeParams(),
                output_path=tmp_path / "out.mp4",
            )
