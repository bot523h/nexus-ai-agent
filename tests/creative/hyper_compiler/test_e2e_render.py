"""End-to-end: real clips through normaliser → analyser → compiler → MP4.

Everything renders into ``/tmp``; the repository is untouched.
"""

from __future__ import annotations

import hashlib
import subprocess
from fractions import Fraction
from pathlib import Path

from nexus_ai_agent.creative.hyper_compiler import (
    bulletproof_normalizer as bn,
)
from nexus_ai_agent.creative.hyper_compiler import (
    differential_filtergraph_compiler as dc,
)
from nexus_ai_agent.creative.hyper_compiler import (
    optical_spectral_analyzer as osa,
)
from nexus_ai_agent.creative.hyper_compiler.differential_filtergraph_compiler import (
    GradeParams,
    SegmentCASRenderer,
)
from nexus_ai_agent.creative.hyper_compiler.models import (
    SDR,
    MediaStreamProbe,
    TimelineDAG,
    TimelineDifferentialNode,
)

_IDENTITY = ((1.0, 0.0), (0.0, 1.0))


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


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


def test_full_pipeline_renders_mp4(make_clip, tmp_path, monkeypatch):
    # 1. Normalise a mobile-sourced clip (no audio → silence injected).
    raw = make_clip("mobile.mp4", duration=2.0, width=320, height=180, has_audio=False)
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
    normal_out = MediaStreamProbe(
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
    probes = iter([normal, normal_out])
    monkeypatch.setattr(bn, "probe_media", lambda p, **kw: next(probes))
    monkeypatch.setattr(bn, "_read_duration", lambda p: 2.0)
    norm = bn.normalize_media(raw, tmp_path / "normalized.mp4")
    assert norm.probe.has_audio is True
    assert norm.probe.rotation_degrees == 0.0

    # 2. Analyse the proxy stream (shot boundaries + audio).
    frames = osa.proxy_frames(str(norm.output_path), width=160, height=90)
    boundaries = osa.detect_shot_boundaries(frames, threshold=0.05)
    audio_segments = osa.analyze_audio_silence_speech(str(norm.output_path))
    assert isinstance(boundaries, list)
    assert isinstance(audio_segments, list)

    # 3. Compile + render a one-node timeline.
    node = TimelineDifferentialNode(
        node_id="n1",
        input_sha256=norm.sha256,
        start_us=0,
        end_us=norm.duration_us,
        speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
        oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    dag = TimelineDAG(
        nodes=(node,),
        total_duration_us=node.duration_us,
        fps=Fraction(30, 1),
        width=norm.probe.width,
        height=norm.probe.height,
    )
    compiled = dc.compile_filtergraph(dag, {"n1": norm.output_path}, grade=GradeParams())
    out = tmp_path / "final.mp4"
    subprocess.run(compiled.argv(out), shell=False, capture_output=True, text=True, timeout=300)
    assert out.exists() and out.stat().st_size > 0
    kinds = _stream_kinds(out)
    assert "video" in kinds and "audio" in kinds


def test_partial_edit_only_rerenders_changed_segment(make_clip, tmp_path):
    clip_a = make_clip("e2e_a.mp4", duration=1.5, has_audio=True)
    clip_b = make_clip("e2e_b.mp4", duration=2.0, has_audio=True)

    def node(node_id, end_us, input_sha256):
        return TimelineDifferentialNode(
            node_id=node_id,
            input_sha256=input_sha256,
            start_us=0,
            end_us=end_us,
            speed_curve=((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0)),
            oklab_matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )

    renderer = SegmentCASRenderer(work_dir=tmp_path / "segments")
    grade = GradeParams()

    def timeline(clips):
        nodes_with_inputs = []
        total = 0
        for clip in clips:
            n = node("n" + str(len(nodes_with_inputs)), int(1.5 * 1_000_000), _sha(clip))
            nodes_with_inputs.append((n, str(clip), _sha(clip)))
            total += n.duration_us
        return nodes_with_inputs, total

    # First timeline: A then B.
    nodes_v1, _ = timeline([clip_a, clip_b])
    out_v1 = tmp_path / "v1.mp4"
    renderer.render_timeline(nodes_with_inputs=nodes_v1, grade=grade, output_path=out_v1)
    seg_a_v1 = renderer.segment_key(_sha(clip_a), nodes_v1[0][0])
    seg_b_v1 = renderer.segment_key(_sha(clip_b), nodes_v1[1][0])
    assert seg_a_v1 != seg_b_v1

    # Second timeline: A unchanged, B re-edited (longer) → only B's key changes.
    clip_b2 = make_clip("e2e_b2.mp4", duration=2.5, has_audio=True)
    nodes_v2, _ = timeline([clip_a, clip_b2])
    seg_a_v2 = renderer.segment_key(_sha(clip_a), nodes_v2[0][0])
    seg_b_v2 = renderer.segment_key(_sha(clip_b2), nodes_v2[1][0])
    assert seg_a_v2 == seg_a_v1  # unchanged segment → cache hit
    assert seg_b_v2 != seg_b_v1  # changed segment → re-render

    out_v2 = tmp_path / "v2.mp4"
    renderer.render_timeline(nodes_with_inputs=nodes_v2, grade=grade, output_path=out_v2)
    assert out_v1.exists() and out_v2.exists()
    for p in (out_v1, out_v2):
        assert _stream_kinds(p) >= {"video", "audio"}
