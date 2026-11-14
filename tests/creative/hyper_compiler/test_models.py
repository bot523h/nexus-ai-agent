"""Unit tests for :mod:`nexus_ai_agent.creative.hyper_compiler.models`.

Pure value types — no subprocesses, no I/O, no ffmpeg required.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.hyper_compiler.models import (
    HDR,
    SDR,
    MediaStreamProbe,
    TimelineDAG,
    TimelineDifferentialNode,
    canonical_params_hash,
)

_IDENTITY = ((1.0, 0.0), (0.0, 1.0))


def _make_probe(**overrides):
    data = dict(
        width=1920,
        height=1080,
        r_frame_rate=Fraction(30, 1),
        avg_frame_rate=Fraction(30, 1),
        aspect_ratio=16 / 9,
        rotation=_IDENTITY,
        color_space=SDR,
        has_audio=True,
        sha256="sha256:" + "0" * 64,
    )
    data.update(overrides)
    return MediaStreamProbe(**data)


def test_probe_defaults_and_properties():
    probe = _make_probe()
    assert probe.width == 1920
    assert probe.height == 1080
    assert probe.rotation_degrees == 0.0
    assert probe.is_variable_frame_rate is False
    assert probe.needs_rotation is False
    assert probe.color_space == SDR
    assert probe.has_audio is True
    assert probe.sha256.startswith("sha256:")


def test_probe_rotation_90_from_matrix():
    # 90° clockwise: [[cos, -sin],[sin, cos]] = [[0,-1],[1,0]]
    matrix = ((0.0, -1.0), (1.0, 0.0))
    probe = _make_probe(rotation=matrix)
    assert abs(probe.rotation_degrees - 90.0) < 1e-6
    assert probe.needs_rotation is True


def test_probe_rotation_180_and_270():
    probe_180 = _make_probe(rotation=((-1.0, 0.0), (0.0, -1.0)))
    assert abs(probe_180.rotation_degrees - 180.0) < 1e-6
    probe_270 = _make_probe(rotation=((0.0, 1.0), (-1.0, 0.0)))
    assert abs(probe_270.rotation_degrees - 270.0) < 1e-6


def test_probe_vfr_detection():
    probe = _make_probe(r_frame_rate=Fraction(60, 1), avg_frame_rate=Fraction(30, 1))
    assert probe.is_variable_frame_rate is True
    probe_cfr = _make_probe(r_frame_rate=Fraction(30, 1), avg_frame_rate=Fraction(30, 1))
    assert probe_cfr.is_variable_frame_rate is False
    # Zero average frame rate → not VFR (fail-safe).
    probe_zero = _make_probe(r_frame_rate=Fraction(30, 1), avg_frame_rate=Fraction(0, 1))
    assert probe_zero.is_variable_frame_rate is False


def test_probe_hdr():
    probe = _make_probe(color_space=HDR)
    assert probe.color_space == HDR


def test_probe_validation():
    with pytest.raises(ValueError):
        _make_probe(width=0)
    with pytest.raises(ValueError):
        _make_probe(aspect_ratio=0.0)
    with pytest.raises(ValueError):
        _make_probe(rotation=((2.0, 0.0), (0.0, 2.0)))  # not SO(2)
    with pytest.raises(ValueError):
        _make_probe(sha256="not-a-hash")
    with pytest.raises(ValueError):
        _make_probe(sha256="sha256:" + "0" * 63)


def test_node_speed_at_and_phase_integral():
    # Identity linear bezier P0=(0,0) P3=(1,1) → constant speed 1.
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "1" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    assert abs(node.speed_at(0.0) - 1.0) < 1e-6
    assert abs(node.speed_at(0.5) - 1.0) < 1e-6
    assert abs(node.phase_integral(1.0) - 1.0) < 1e-6
    assert node.duration_us == 1_000_000

    # Clamped out-of-range t.
    assert abs(node.speed_at(-0.5) - 1.0) < 1e-6
    assert abs(node.speed_at(2.0) - 1.0) < 1e-6


def test_node_validation():
    with pytest.raises(ValueError):
        TimelineDifferentialNode(
            node_id="n1",
            input_sha256="sha256:" + "1" * 64,
            start_us=100,
            end_us=50,
            speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
            oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )
    with pytest.raises(ValueError):
        TimelineDifferentialNode(
            node_id="n1",
            input_sha256="sha256:" + "1" * 64,
            start_us=0,
            end_us=100,
            speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
            oklab_matrix=(
                (
                    1.0,
                    0.0,
                ),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            ),  # not 3x3
        )
    with pytest.raises(ValueError):
        TimelineDifferentialNode(
            node_id="n1",
            input_sha256="bad-hash",
            start_us=0,
            end_us=100,
            speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
            oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )


def test_dag_params_hash_stable():
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "1" * 64,
        start_us=0,
        end_us=1_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    dag = TimelineDAG(
        nodes=(node,),
        total_duration_us=1_000_000,
        fps=Fraction(30, 1),
        width=1920,
        height=1080,
    )
    h1 = dag.params_hash
    h2 = dag.params_hash
    assert h1 == h2
    assert h1.startswith("sha256:")

    # Different params → different hash.
    node2 = TimelineDifferentialNode(
        node_id="n1",
        input_sha256="sha256:" + "1" * 64,
        start_us=0,
        end_us=2_000_000,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    dag2 = TimelineDAG(
        nodes=(node2,),
        total_duration_us=2_000_000,
        fps=Fraction(30, 1),
        width=1920,
        height=1080,
    )
    assert dag2.params_hash != h1


def test_canonical_params_hash():
    h1 = canonical_params_hash({"a": 1, "b": [2, 3]})
    h2 = canonical_params_hash({"b": [2, 3], "a": 1})
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == 71  # "sha256:" + 64 hex
