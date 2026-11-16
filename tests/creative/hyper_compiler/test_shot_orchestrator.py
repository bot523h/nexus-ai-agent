"""Orchestrator tests, including the headline 95% cache-reuse proof."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    FramePipelineSpec,
    MuxMode,
)
from nexus_ai_agent.creative.hyper_compiler.errors import HyperCompilerError
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    SEGMENT_HEADER_LEN,
    SegmentStatus,
    TimelineSegmentSpec,
)
from nexus_ai_agent.creative.hyper_compiler.shot_orchestrator import (
    HyperTimelineOrchestrator,
    default_pipe_spec,
)

Frame = NDArray[np.uint8]


def _ffmpeg_exe() -> str | None:
    """Locate a usable FFmpeg: PATH first, else the imageio-ffmpeg bundle."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        return cast(str, imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # pragma: no cover - environment without any FFmpeg
        return None


FFMPEG = _ffmpeg_exe()


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
    keys = {
        spec.segment_id: orch.planner.cas_key(
            spec,
            pipeline_digest=default_pipe_spec(spec).fingerprint(),
            mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
        )
        for spec in timeline
    }
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


def test_orchestrator_pipeline_change_re_renders_every_segment(tmp_path: Path) -> None:
    """P1-4: a pipeline-knob change invalidates segments even if the spec is equal."""
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(i) for i in range(5)]
    orch.execute(timeline, frame_factory=frame_factory())

    # Same timeline, brighter pipeline: every segment must be re-rendered.
    brighter = orch.execute(
        timeline,
        frame_factory=frame_factory(),
        pipe_spec_factory=lambda s: FramePipelineSpec(
            width=s.width, height=s.height, fps=s.fps, brightness=0.3
        ),
    )
    assert brighter.plan.cache_hits == 0
    assert brighter.receipt.rendered_segments == 5


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
    assert execution.output_size == out.stat().st_size


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
    assert orch.planner.read_segment_artifact(planned.cas_key)
    assert outcome.container_size == planned.artifact_path.stat().st_size - SEGMENT_HEADER_LEN


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


def test_orchestrator_cpu_mode_receipt_reports_cpu(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    execution = orch.execute([make_spec(0)], frame_factory=frame_factory())
    assert execution.receipt.mode == "deterministic_cpu_mux"
    assert execution.segments[0].mode == "deterministic_cpu_mux"


@pytest.mark.skipif(FFMPEG is None, reason="no FFmpeg available")
def test_orchestrator_ffmpeg_mode_really_runs(tmp_path: Path) -> None:
    """P1-1: FFMPEG_SUBPROCESS_PIPE must really drive the engine, not just label it."""
    assert FFMPEG is not None
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024, ffmpeg_bin=FFMPEG)
    execution = orch.execute(
        [make_spec(0)],
        frame_factory=frame_factory(),
        mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
    )
    assert execution.receipt.mode == "ffmpeg_subprocess_pipe"
    assert execution.segments[0].mode == "ffmpeg_subprocess_pipe"
    assert execution.receipt.rendered_segments == 1
    assert execution.output_size > 0
    # A second run reuses the cached artifact and the receipt still says ffmpeg.
    second = orch.execute(
        [make_spec(0)],
        frame_factory=frame_factory(),
        mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
    )
    assert second.plan.cache_hits == 1
    assert second.receipt.mode == "ffmpeg_subprocess_pipe"


def test_orchestrator_ffmpeg_mode_fails_closed_without_binary(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(
        tmp_path, max_buffer_bytes=1024 * 1024, ffmpeg_bin="/nonexistent/ffmpeg-xyz"
    )
    from nexus_ai_agent.creative.hyper_compiler.errors import PipeExecutionError

    with pytest.raises(PipeExecutionError):
        orch.execute(
            [make_spec(0)],
            frame_factory=frame_factory(),
            mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
        )


def test_orchestrator_peak_memory_stays_bounded(tmp_path: Path) -> None:
    # 8x8x3 = 192 B/frame; the CPU working set for this geometry is 832 B, so a
    # 4096 B budget still bounds the reported peak.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=4096)
    execution = orch.execute([make_spec(0)], frame_factory=frame_factory())
    assert execution.receipt.peak_buffer_bytes <= 4096


def test_orchestrator_hmac_key_is_honoured(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, hmac_key=b"secret")
    spec = make_spec(0)
    digest = default_pipe_spec(spec).fingerprint()
    assert (
        orch.planner.cas_key(
            spec, pipeline_digest=digest, mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value
        )
        == orch.plan([spec]).segments[0].cas_key
    )


def test_orchestrator_cache_hit_segment_reports_zero_peak(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0)]
    orch.execute(timeline, frame_factory=frame_factory())
    second = orch.execute(timeline, frame_factory=frame_factory())
    assert second.receipt.peak_buffer_bytes == 0


@pytest.mark.skipif(FFMPEG is None, reason="no FFmpeg available")
def test_orchestrator_switching_engine_is_a_cache_miss(tmp_path: Path) -> None:
    """P1-A: the same segment rendered by one engine is a CACHE_MISS for the other."""
    assert FFMPEG is not None
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024, ffmpeg_bin=FFMPEG)
    timeline = [make_spec(0)]

    cpu_run = orch.execute(timeline, frame_factory=frame_factory())
    assert cpu_run.plan.render_required == 1
    assert cpu_run.plan.segments[0].mode == "deterministic_cpu_mux"

    # Same timeline, different engine: the CPU artifact must NOT satisfy it.
    ffmpeg_run = orch.execute(
        timeline,
        frame_factory=frame_factory(),
        mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
    )
    assert ffmpeg_run.plan.cache_hits == 0
    assert ffmpeg_run.plan.render_required == 1
    assert ffmpeg_run.receipt.mode == "ffmpeg_subprocess_pipe"
    assert ffmpeg_run.plan.segments[0].cas_key != cpu_run.plan.segments[0].cas_key


def test_orchestrator_cpu_cache_hit_reports_cpu_mode(tmp_path: Path) -> None:
    # P1-A: a cache hit reports the mode recorded in the manifest, not a default.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0)]
    orch.execute(timeline, frame_factory=frame_factory())
    second = orch.execute(timeline, frame_factory=frame_factory())
    assert second.plan.cache_hits == 1
    assert second.plan.segments[0].mode == "deterministic_cpu_mux"
    assert second.segments[0].mode == "deterministic_cpu_mux"


def test_orchestrator_rejects_mixed_engine_modes(tmp_path: Path) -> None:
    # If a manifest's recorded mode disagrees with another segment's, the run is
    # refused rather than emitting a receipt that lies about the engine.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    spec0 = make_spec(0)
    key0 = orch.planner.cas_key(
        spec0,
        pipeline_digest=default_pipe_spec(spec0).fingerprint(),
        mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
    )
    # A cached artifact whose manifest claims the FFmpeg engine.
    orch.planner.write_segment_artifact(key0, b"body", mode="ffmpeg_subprocess_pipe")
    # seg1 is uncached and will be rendered by the CPU engine.
    with pytest.raises(HyperCompilerError, match="mixed engine modes"):
        orch.execute([spec0, make_spec(1)], frame_factory=frame_factory())


def test_orchestrator_rejects_unknown_manifest_mode(tmp_path: Path) -> None:
    # A tampered manifest with a bogus mode must fail closed, not raise a bare
    # ValueError when the receipt tries to parse it.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    spec0 = make_spec(0)
    key0 = orch.planner.cas_key(
        spec0,
        pipeline_digest=default_pipe_spec(spec0).fingerprint(),
        mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
    )
    orch.planner.write_segment_artifact(key0, b"body", mode="not-a-real-engine")
    with pytest.raises(HyperCompilerError, match="unknown engine mode"):
        orch.execute([spec0], frame_factory=frame_factory())


def test_orchestrator_empty_timeline_receipt_mode(tmp_path: Path) -> None:
    # With no segments the receipt falls back to the requested mode.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    execution = orch.execute([], frame_factory=frame_factory())
    assert execution.receipt.mode == "deterministic_cpu_mux"
    assert execution.receipt.total_segments == 0
