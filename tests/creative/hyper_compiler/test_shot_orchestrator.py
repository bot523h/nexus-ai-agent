"""Orchestrator tests, including the headline 95% cache-reuse proof."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler import shot_orchestrator as so
from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    FramePipelineSpec,
    MuxMode,
)
from nexus_ai_agent.creative.hyper_compiler.errors import (
    HyperCompilerError,
    SegmentTamperError,
)
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    SEGMENT_HEADER_LEN,
    PlannedSegment,
    SegmentStatus,
    TimelineSegmentSpec,
)
from nexus_ai_agent.creative.hyper_compiler.shot_orchestrator import (
    HyperTimelineOrchestrator,
    _resolve_receipt_mode,
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

    # The reused-cache output is bit-identical to a full from-scratch render of
    # the same changed timeline in a fresh cache: 19 cached segments + 1 render
    # must produce exactly the bytes of 20 renders.
    scratch = HyperTimelineOrchestrator(tmp_path / "scratch", max_buffer_bytes=1024 * 1024)
    full = scratch.execute(changed, frame_factory=frame_factory())
    assert full.receipt.rendered_segments == 20
    assert second.output_digest == full.output_digest
    assert second.output_size == full.output_size


def test_orchestrator_rejects_forged_cache_and_rerenders(tmp_path: Path) -> None:
    # F1 end-to-end: if a cached artifact's manifest is tampered (forged engine
    # mode), the orchestrator must not trust it — it re-renders and emits an
    # honest receipt rather than minting a false cache hit.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0)]
    first = orch.execute(timeline, frame_factory=frame_factory())
    assert first.receipt.cache_hits == 0

    spec = timeline[0]
    key = orch.planner.cas_key(
        spec,
        pipeline_digest=default_pipe_spec(spec).fingerprint(),
        mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
    )
    manifest_path = orch.planner.manifest_path(key)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["mode"] = "ffmpeg_subprocess_pipe"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = orch.execute(timeline, frame_factory=frame_factory())
    assert second.plan.cache_hits == 0
    assert second.plan.render_required == 1
    assert second.receipt.rendered_segments == 1


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


def test_orchestrator_rejects_mixed_engine_modes() -> None:
    # Defence in depth: a run that somehow mixes engines is refused rather than
    # emitting a receipt that lies about how the bytes were produced.  (The mode
    # is part of the cache key, so this is not reachable through the public API;
    # the guard is exercised directly.)
    with pytest.raises(HyperCompilerError, match="mixed engine modes"):
        _resolve_receipt_mode(
            {"deterministic_cpu_mux", "ffmpeg_subprocess_pipe"}, MuxMode.DETERMINISTIC_CPU_MUX
        )


def test_orchestrator_rejects_unknown_manifest_mode() -> None:
    # A tampered manifest that names a mode the enum does not know must fail
    # closed rather than be trusted for the receipt.
    with pytest.raises(HyperCompilerError, match="unknown engine mode"):
        _resolve_receipt_mode({"not-a-real-engine"}, MuxMode.DETERMINISTIC_CPU_MUX)


def test_orchestrator_empty_timeline_receipt_mode(tmp_path: Path) -> None:
    # With no segments the receipt falls back to the requested mode.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    execution = orch.execute([], frame_factory=frame_factory())
    assert execution.receipt.mode == "deterministic_cpu_mux"
    assert execution.receipt.total_segments == 0


# ── F1: render_segment is bound to the planned segment ──────────────────────
def test_orchestrator_render_segment_rejects_mode_mismatch(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    planned = orch.plan([make_spec(0)]).segments[0]
    with pytest.raises(SegmentTamperError, match="mode"):
        orch.render_segment(
            planned,
            frame_factory()(make_spec(0)),
            mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
        )
    # Nothing was written under the planned key.
    assert not orch.planner.artifact_path(planned.cas_key).exists()


def test_orchestrator_render_segment_rejects_pipeline_mismatch(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    planned = orch.plan([make_spec(0)]).segments[0]
    brighter = FramePipelineSpec(width=8, height=8, fps=24.0, brightness=0.5)
    with pytest.raises(SegmentTamperError, match="pipeline"):
        orch.render_segment(planned, frame_factory()(make_spec(0)), pipe_spec=brighter)
    assert not orch.planner.artifact_path(planned.cas_key).exists()


def test_orchestrator_render_segment_rejects_key_mismatch(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    planned = orch.plan([make_spec(0)]).segments[0]
    forged = PlannedSegment(
        planned.spec,
        "f" * 64,
        planned.status,
        planned.artifact_path,
        planned.pipeline_digest,
        mode=planned.mode,
    )
    with pytest.raises(SegmentTamperError, match="key"):
        orch.render_segment(forged, frame_factory()(make_spec(0)))
    assert not orch.planner.artifact_path(forged.cas_key).exists()


# ── F2: the orchestrator forwards the watchdog timeout to the runner ────────
def test_orchestrator_forwards_timeout_to_runner(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, timeout_seconds=1.25)
    assert orch.runner.timeout_seconds == 1.25


def test_orchestrator_rejects_invalid_timeout(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.hyper_compiler.errors import PipeExecutionError

    with pytest.raises(PipeExecutionError):
        HyperTimelineOrchestrator(tmp_path, timeout_seconds=0.0)


# ── F4: execute() makes the output rename durable ───────────────────────────
def test_orchestrator_execute_fsyncs_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
        _fsync_directory,
    )

    synced: list[Path] = []

    def spy(path: Path) -> None:
        synced.append(Path(path))
        _fsync_directory(path)

    monkeypatch.setattr(so, "_fsync_directory", spy)
    orch = HyperTimelineOrchestrator(tmp_path / "cache", max_buffer_bytes=1024 * 1024)
    out = tmp_path / "nested" / "final.nagartl"
    orch.execute([make_spec(0)], frame_factory=frame_factory(), output_path=out)
    assert out.parent in synced


def test_orchestrator_execute_propagates_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real disk error while making the rename durable must not be swallowed:
    # the caller must never believe a non-durable write succeeded.
    def boom(path: Path) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(so, "_fsync_directory", boom)
    orch = HyperTimelineOrchestrator(tmp_path / "cache", max_buffer_bytes=1024 * 1024)
    out = tmp_path / "final.nagartl"
    with pytest.raises(OSError):
        orch.execute([make_spec(0)], frame_factory=frame_factory(), output_path=out)
    assert list(tmp_path.glob("*.tmp")) == []


# ── F1 (direct path): a non-pure pipe_spec_factory cannot poison the CAS ─────
def test_orchestrator_snapshots_pipe_spec_once(tmp_path: Path) -> None:
    # The factory must be called exactly once per segment for the whole run:
    # plan() and execute() must NOT each call it (that double call is what let a
    # non-pure factory commit bytes under a key that disagreed with the plan).
    calls: list[str] = []

    def counting_factory(s: TimelineSegmentSpec) -> FramePipelineSpec:
        calls.append(s.segment_id)
        return FramePipelineSpec(width=s.width, height=s.height, fps=s.fps, brightness=0.25)

    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0), make_spec(1)]
    orch.execute(timeline, frame_factory=frame_factory(), pipe_spec_factory=counting_factory)
    assert calls == ["seg0", "seg1"]


def test_orchestrator_unstable_pipe_spec_factory_is_rejected(tmp_path: Path) -> None:
    # F1: a factory that returns a *different* pipeline on the second call for
    # the same segment would make the rendered bytes disagree with the planned
    # key.  Snapshotting removes the second call, so the run is still bound to
    # the plan-time pipeline and the committed artifact verifies against it.
    seen: dict[str, int] = {}

    def unstable_factory(s: TimelineSegmentSpec) -> FramePipelineSpec:
        n = seen.get(s.segment_id, 0)
        seen[s.segment_id] = n + 1
        brightness = 0.1 if n == 0 else 0.9  # second call would differ
        return FramePipelineSpec(width=s.width, height=s.height, fps=s.fps, brightness=brightness)

    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    timeline = [make_spec(0)]
    execution = orch.execute(
        timeline, frame_factory=frame_factory(), pipe_spec_factory=unstable_factory
    )
    # Exactly one factory call per segment, and the artifact verifies against the
    # planned (brightness=0.1) pipeline — not the 0.9 the second call would give.
    assert seen == {"seg0": 1}
    planned = execution.plan.segments[0]
    expected = FramePipelineSpec(width=8, height=8, fps=24.0, brightness=0.1).fingerprint()
    assert planned.pipeline_digest == expected
    verified = orch.planner.verify_segment_artifact(
        planned.cas_key,
        expected_mode=planned.mode,
        expected_pipeline_digest=planned.pipeline_digest,
    )
    assert verified.mode == planned.mode


def test_orchestrator_execute_fails_closed_on_provenance_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F1: even if a planned segment's key were forged to disagree with the
    # snapshot pipeline, execute() must refuse *before* rendering or writing to
    # the cache — never commit bytes under a mismatched key.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    real_plan = orch._plan_from_specs

    def forging_plan(timeline: object, specs: object, mode: object) -> object:
        plan = real_plan(timeline, specs, mode)  # type: ignore[arg-type]
        forged = []
        for seg in plan.segments:
            forged.append(
                PlannedSegment(
                    seg.spec,
                    "f" * 64,  # a key that does not match the snapshot pipeline
                    seg.status,
                    seg.artifact_path,
                    seg.pipeline_digest,
                    mode=seg.mode,
                )
            )
        from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import SegmentPlan

        return SegmentPlan(
            segments=tuple(forged),
            merkle_root=plan.merkle_root,
            total_leaves=plan.total_leaves,
            cache_hits=plan.cache_hits,
            render_required=plan.render_required,
        )

    monkeypatch.setattr(orch, "_plan_from_specs", forging_plan)
    with pytest.raises(SegmentTamperError, match="key"):
        orch.execute([make_spec(0)], frame_factory=frame_factory())
    # Nothing was committed to the cache under the forged key.
    assert list((tmp_path).rglob("*.nagarseg")) == []


# ── F4: the temp file lives on the destination filesystem (no EXDEV) ─────────
def test_orchestrator_output_replace_is_same_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4: the timeline temp file must be created in the *same directory* as the
    # final target, so the atomic ``os.replace`` cannot fail with EXDEV across
    # filesystems.  We observe the src/dst of the timeline rename directly.
    real_replace = os.replace
    pairs: list[tuple[Path, Path]] = []

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        pairs.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    orch = HyperTimelineOrchestrator(tmp_path / "cache", max_buffer_bytes=1024 * 1024)
    out = tmp_path / "nested" / "final.nagartl"
    orch.execute([make_spec(0)], frame_factory=frame_factory(), output_path=out)
    timeline_pairs = [p for p in pairs if p[0].name.endswith(".nagartl.tmp")]
    assert timeline_pairs, "the timeline was not renamed atomically"
    src, dst = timeline_pairs[-1]
    assert src.parent == dst.parent == out.parent
    assert out.exists()


def test_orchestrator_output_temp_cleaned_on_render_failure(
    tmp_path: Path,
) -> None:
    # F4: if the render fails before os.replace, the same-filesystem temp file
    # must be removed so no ``.nagartl.tmp`` is left behind next to the target.
    orch = HyperTimelineOrchestrator(tmp_path / "cache", max_buffer_bytes=1024 * 1024)
    out = tmp_path / "final.nagartl"

    def boom(_spec: TimelineSegmentSpec) -> Iterator[Frame]:
        raise HyperCompilerError("frame source exploded")

    with pytest.raises(HyperCompilerError):
        orch.execute([make_spec(0)], frame_factory=boom, output_path=out)
    assert list(tmp_path.glob("*.nagartl.tmp")) == []
    assert not out.exists()


def test_orchestrator_assert_provenance_rejects_mode_mismatch(tmp_path: Path) -> None:
    # Directly exercise the execute()-path guard's fail-closed branches.
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    spec0 = make_spec(0)
    pipe = default_pipe_spec(spec0)
    planned = PlannedSegment(
        spec0,
        orch.planner.cas_key(spec0, pipeline_digest=pipe.fingerprint(), mux_mode="cpu"),
        SegmentStatus.RENDER_REQUIRED,
        Path("/x"),
        pipe.fingerprint(),
        mode="cpu",
    )
    with pytest.raises(SegmentTamperError, match="mode"):
        orch._assert_provenance(planned, pipe, MuxMode.FFMPEG_SUBPROCESS_PIPE)


def test_orchestrator_assert_provenance_rejects_pipeline_mismatch(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    spec0 = make_spec(0)
    pipe = default_pipe_spec(spec0)
    planned = PlannedSegment(
        spec0,
        orch.planner.cas_key(
            spec0, pipeline_digest="deadbeef", mux_mode=MuxMode.DETERMINISTIC_CPU_MUX.value
        ),
        SegmentStatus.RENDER_REQUIRED,
        Path("/x"),
        "deadbeef",  # a digest that disagrees with the snapshot pipeline
        mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
    )
    with pytest.raises(SegmentTamperError, match="pipeline"):
        orch._assert_provenance(planned, pipe, MuxMode.DETERMINISTIC_CPU_MUX)


def test_orchestrator_plan_from_specs_rejects_length_mismatch(tmp_path: Path) -> None:
    orch = HyperTimelineOrchestrator(tmp_path, max_buffer_bytes=1024 * 1024)
    with pytest.raises(HyperCompilerError, match="snapshot length"):
        orch._plan_from_specs(
            [make_spec(0), make_spec(1)],
            [default_pipe_spec(make_spec(0))],
            MuxMode.DETERMINISTIC_CPU_MUX,
        )
