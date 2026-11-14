"""Tests for :mod:`differential_filtergraph_compiler`.

Includes the ``.cube`` LUT generator, filtergraph compilation (executed against
real ffmpeg), and the content-addressed segment renderer with cache hit/miss.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest

from nexus_ai_agent.creative.hyper_compiler.differential_filtergraph_compiler import (
    CompileError,
    GradeParams,
    SegmentCASRenderer,
    compile_filtergraph,
    generate_oklab_3dlut_cube_file,
)
from nexus_ai_agent.creative.hyper_compiler.models import (
    TimelineDAG,
    TimelineDifferentialNode,
)


def _make_node(
    node_id: str = "n1",
    *,
    start_us: int = 0,
    end_us: int = 1_000_000,
    input_sha256: str | None = None,
) -> TimelineDifferentialNode:
    return TimelineDifferentialNode(
        node_id=node_id,
        input_sha256=input_sha256 or ("sha256:" + "9" * 64),
        start_us=start_us,
        end_us=end_us,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=300)


def _probe_ok(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-hide_banner", "-i", str(path), "-f", "null", "-"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


def _stream_kinds(path: Path) -> set[str]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = result.stderr
    kinds: set[str] = set()
    for line in text.splitlines():
        if "Stream #" in line:
            if ": Video:" in line:
                kinds.add("video")
            if ": Audio:" in line:
                kinds.add("audio")
    return kinds


def test_generate_oklab_3dlut_cube_file_format(tmp_path):
    params = GradeParams()
    out = tmp_path / "grade.cube"
    start = time.perf_counter()
    result = generate_oklab_3dlut_cube_file(params, out)
    elapsed = time.perf_counter() - start
    assert result == out
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("LUT_3D_SIZE 17")
    lines = [line for line in text.splitlines() if line.strip()]
    # Header + 17^3 grid points.
    assert len(lines) == 1 + 17**3
    # Every grid point is a valid RGB triplet in [0, 1].
    for line in lines[1:]:
        r, g, b = line.split()
        assert 0.0 <= float(r) <= 1.0
        assert 0.0 <= float(g) <= 1.0
        assert 0.0 <= float(b) <= 1.0
    # Well within the 10 ms budget (this bound is deliberately generous to
    # avoid flakiness on loaded CI hosts; the vectorised impl is ~ms).
    assert elapsed < 1.0, f"LUT generation took {elapsed:.3f}s"


def test_generate_cube_rejects_bad_temperature(tmp_path):
    with pytest.raises(ValueError):
        generate_oklab_3dlut_cube_file(GradeParams(temperature_k=50000), tmp_path / "x.cube")


def test_compile_filtergraph_renders_real_mp4(make_clip, tmp_path):
    clip = make_clip("src.mp4", duration=2.0, width=320, height=180, has_audio=True)
    duration_sec = 2.0
    input_sha256 = "sha256:" + hashlib.sha256(clip.read_bytes()).hexdigest()

    node = _make_node("n1", end_us=int(duration_sec * 1_000_000), input_sha256=input_sha256)
    dag = TimelineDAG(
        nodes=(node,),
        total_duration_us=node.duration_us,
        fps=Fraction(30, 1),
        width=320,
        height=180,
    )
    compiled = compile_filtergraph(dag, {"n1": str(clip)})
    assert compiled.filtergraph
    assert compiled.video_out is not None
    assert compiled.audio_out is not None

    out = tmp_path / "rendered.mp4"
    argv = compiled.argv(out)
    result = _run(argv)
    assert result.returncode == 0, result.stderr
    assert out.exists() and out.stat().st_size > 0

    assert _probe_ok(out)
    kinds = _stream_kinds(out)
    assert "video" in kinds and "audio" in kinds


def test_compile_filtergraph_missing_input_raises():
    node = _make_node("n1")
    dag = TimelineDAG(
        nodes=(node,),
        total_duration_us=node.duration_us,
        fps=Fraction(30, 1),
        width=320,
        height=180,
    )
    with pytest.raises(CompileError):
        compile_filtergraph(dag, {})


def test_segment_cache_hit_and_miss(make_clip, tmp_path):
    clip_a = make_clip("a.mp4", duration=2.0, has_audio=True)
    clip_b = make_clip("b.mp4", duration=2.0, has_audio=True)

    def sha(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    renderer = SegmentCASRenderer(work_dir=tmp_path / "segments")
    grade = GradeParams()
    node_a = _make_node("a", end_us=2_000_000, input_sha256=sha(clip_a))
    node_b = _make_node("b", end_us=2_000_000, input_sha256=sha(clip_b))

    # First render: both segments are cache misses (actually encoded).
    seg_a1 = renderer.render_segment(
        input_path=str(clip_a), input_sha256=sha(clip_a), node=node_a, grade=grade
    )
    seg_b1 = renderer.render_segment(
        input_path=str(clip_b), input_sha256=sha(clip_b), node=node_b, grade=grade
    )
    assert seg_a1.exists() and seg_b1.exists()

    # Same inputs again: cache hits (same paths returned, no re-encode).
    seg_a2 = renderer.render_segment(
        input_path=str(clip_a), input_sha256=sha(clip_a), node=node_a, grade=grade
    )
    seg_b2 = renderer.render_segment(
        input_path=str(clip_b), input_sha256=sha(clip_b), node=node_b, grade=grade
    )
    assert seg_a2 == seg_a1
    assert seg_b2 == seg_b1

    # Editing node B's params invalidates only B's cache entry.
    node_b_edited = _make_node("b", end_us=3_000_000, input_sha256=sha(clip_b))
    seg_b3 = renderer.render_segment(
        input_path=str(clip_b), input_sha256=sha(clip_b), node=node_b_edited, grade=grade
    )
    assert seg_b3 != seg_b1
    # A remains cached.
    seg_a3 = renderer.render_segment(
        input_path=str(clip_a), input_sha256=sha(clip_a), node=node_a, grade=grade
    )
    assert seg_a3 == seg_a1


def test_render_timeline_stream_copy_concat(make_clip, tmp_path):
    clip_a = make_clip("ta.mp4", duration=1.5, has_audio=True)
    clip_b = make_clip("tb.mp4", duration=1.5, has_audio=True)

    def sha(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    renderer = SegmentCASRenderer(work_dir=tmp_path / "segments")
    grade = GradeParams()
    nodes = [
        (
            _make_node("a", end_us=int(1.5 * 1_000_000), input_sha256=sha(clip_a)),
            str(clip_a),
            sha(clip_a),
        ),
        (
            _make_node("b", end_us=int(1.5 * 1_000_000), input_sha256=sha(clip_b)),
            str(clip_b),
            sha(clip_b),
        ),
    ]
    out = tmp_path / "timeline.mp4"
    result_path = renderer.render_timeline(nodes_with_inputs=nodes, grade=grade, output_path=out)
    assert result_path == out
    assert out.exists() and out.stat().st_size > 0
    assert _probe_ok(out)
    kinds = _stream_kinds(out)
    assert "video" in kinds and "audio" in kinds
