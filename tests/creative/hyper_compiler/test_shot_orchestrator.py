"""Orchestrator tests, including the headline 95% cache-reuse proof."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    FramePipelineSpec,
    MuxMode,
)
from nexus_ai_agent.creative.hyper_compiler.errors import HyperCompilerError
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    SegmentStatus,
    TimelineSegmentSpec,
)
from nexus_ai_agent.creative.hyper_compiler.shot_orchestrator import (
    HyperTimelineOrchestrator,
    default_pipe_spec,
)

Frame = NDArray[np.uint8]


def make_spec(index: int, *, dna: str = "dna", value: int = 100) -> TimelineSegmentSpec:
    return TimelineSegmentSpec(
        segment_id=f"seg{index}",
        start_frame=index * 10,
        end_frame=(index + 1) * 10,
        fps=24.0,
        width=8,
        height=8,
        shot_dna_digest=f"{dna}-{value}",
        camera_digest="cam",
        lighting_digest="lit",
        audio_digest="aud",
        audio_filter_digest="afx",
    )


def frame_factory(value: int = 100) -> Callable[[TimelineSegmentSpec], Iterator[Frame]]:
    def factory(_spec: TimelineSegmentSpec) -> Iterator[Frame]:
        for _ in range(3):
            yield np.full((8, 8, 3), value, dtype=np.uint8)

    return factory


def test_default_pipe_spec_mirrors_geometry() -> None:
    pipe = default_pipe_spec(make_spec(0))
    assert pipe.width == 8 and pipe.height == 8 and pipe.fps == 24.0


def test_orchestrator_plan_delegates_to_planner(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path)
    plan = orch.plan([make_spec(0), make_spec(1)])
    assert plan.total_leaves == 2
    assert plan.cache_hits == 0


def test_orchestrator_first_run_renders_every_segment(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    execution = orch.execute([make_spec(i) for i in range(20)], frame_factory=frame_factory())
    assert execution.receipt.rendered_segments == 20
    assert execution.receipt.cache_hits == 0
    assert all(not seg.from_cache for seg in execution.segments)
    assert execution.receipt.timeline_merkle_root == execution.plan.merkle_root


def test_orchestrator_output_is_deterministic(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(4)]
    first = orch.execute(timeline, frame_factory=frame_factory())
    second = orch.execute(timeline, frame_factory=frame_factory())
    assert first.output_digest == second.output_digest
    assert first.output_size == second.output_size


def test_orchestrator_95_percent_cache_recovery(tmp_path: Path) -> None:
    """The headline proof: change 1 of 20 segments, re-render exactly 1."""
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(20)]

    first = orch.execute(timeline, frame_factory=frame_factory())
    assert first.plan.render_required == 20
    assert first.plan.cache_hits == 0

    changed = list(timeline)
    changed[7] = make_spec(7, dna="changed", value=200)
    second = orch.execute(changed, frame_factory=frame_factory())

    assert second.plan.cache_hits == 19
    assert second.plan.render_required == 1
    assert second.receipt.cache_hit_ratio == 0.95
    assert second.receipt.cache_hits == 19
    assert second.receipt.rendered_segments == 1
    rendered = [seg for seg in second.segments if not seg.from_cache]
    assert [seg.segment_id for seg in rendered] == ["seg7"]


def test_orchestrator_unchanged_segments_are_not_rewritten(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(5)]
    orch.execute(timeline, frame_factory=frame_factory())
    keys = {spec.segment_id: orch.planner.cas_key(spec) for spec in timeline}
    before = {key: (tmp_path / f"{key}.nagarseg").stat().st_mtime_ns for key in keys.values()}

    changed = list(timeline)
    changed[2] = make_spec(2, dna="changed", value=200)
    second = orch.execute(changed, frame_factory=frame_factory())

    # The four unchanged segments keep byte-identical cached artifacts...
    for segment_id in ("seg0", "seg1", "seg3", "seg4"):
        key = keys[segment_id]
        assert (tmp_path / f"{key}.nagarseg").stat().st_mtime_ns == before[key]
    # ...and only the changed segment was re-rendered under a fresh key.
    rendered = [seg for seg in second.segments if not seg.from_cache]
    assert [seg.segment_id for seg in rendered] == ["seg2"]


def test_orchestrator_cache_hit_payloads_are_byte_identical(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(3)]
    first = orch.execute(timeline, frame_factory=frame_factory())
    second = orch.execute(timeline, frame_factory=frame_factory())
    for a, b in zip(first.segments, second.segments, strict=True):
        assert a.payload_digest == b.payload_digest
        assert a.payload_size == b.payload_size
        assert b.from_cache is True


def test_orchestrator_requires_frames_for_uncached_segment(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path)
    with pytest.raises(HyperCompilerError):
        orch.execute([make_spec(0)])


def test_orchestrator_writes_output_file(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path / "cache", max_buffer_bytes=1024 * 1024)
    out = tmp_path / "final.nagartl"
    execution = orch.execute([make_spec(0)], frame_factory=frame_factory(), output_path=out)
    assert execution.output_path == out
    assert out.read_bytes()
    assert execution.output_digest == hashlib.sha256(out.read_bytes()).hexdigest()


def test_orchestrator_execute_delta_is_equivalent(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(3)]
    orch.execute(timeline, frame_factory=frame_factory())
    delta = orch.execute_delta(timeline, frame_factory=frame_factory())
    assert delta.plan.cache_hits == 3


def test_orchestrator_custom_pipe_spec_factory(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0), make_spec(1)]
    orch.execute(
        timeline,
        frame_factory=frame_factory(),
        pipe_spec_factory=lambda _spec: FramePipelineSpec(
            width=8, height=8, fps=24.0, brightness=0.1
        ),
    )
    assert len(list(tmp_path.glob("*.nagarseg"))) == 2


def test_orchestrator_render_segment_directly(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    planned = orch.plan([make_spec(0)]).segments[0]
    assert planned.status is SegmentStatus.RENDER_REQUIRED
    outcome = orch.render_segment(planned, frame_factory()(make_spec(0)))
    assert outcome.frames_processed == 3
    assert orch.planner.verify_segment_artifact(planned.cas_key) == outcome.container


def test_orchestrator_verify_timeline(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(3)]
    execution = orch.execute(timeline, frame_factory=frame_factory())
    assert orch.verify_timeline(timeline) == execution.plan.merkle_root


def test_orchestrator_verify_timeline_skips_uncached(tmp_path: Path) -> None:
    # Nothing is cached yet, so every segment is RENDER_REQUIRED: verify must
    # skip them (nothing on disk to check) and still return the Merkle root.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(3)]
    assert orch.verify_timeline(timeline) == orch.plan(timeline).merkle_root


def test_orchestrator_receipt_reports_mode(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    execution = orch.execute(
        [make_spec(0)],
        frame_factory=frame_factory(),
        mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
    )
    assert execution.receipt.mode == "ffmpeg_subprocess_pipe"


def test_orchestrator_peak_memory_stays_bounded(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=192 * 2)
    execution = orch.execute([make_spec(0)], frame_factory=frame_factory())
    assert execution.receipt.peak_buffer_bytes <= 192 * 2


def test_orchestrator_hmac_key_is_honoured(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, hmac_key=b"secret")
    assert orch.planner.cas_key(make_spec(0)) == orch.plan([make_spec(0)]).segments[0].cas_key


def test_orchestrator_cache_hit_segment_reports_zero_peak(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0)]
    orch.execute(timeline, frame_factory=frame_factory())
    second = orch.execute(timeline, frame_factory=frame_factory())
    assert second.receipt.peak_buffer_bytes == 0
