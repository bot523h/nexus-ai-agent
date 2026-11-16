"""End-to-end orchestration of the hyper-compiler pipeline.

:class:`HyperTimelineOrchestrator` wires the three engines together:

1. the segment-CAS planner (:mod:`segment_cas_dag`) decides, per segment,
   ``CACHE_HIT`` vs ``RENDER_REQUIRED`` against the on-disk content-addressed
   store;
2. the bounded-memory pipe runner (:mod:`bounded_memory_pipe_runner`) streams
   only the segments that must be re-rendered, under a hard memory ceiling, and
   the resulting payload is committed atomically to the cache;
3. the filter-graph compiler (:mod:`ffmpeg_filter_graph_compiler`) builds the
   single-pass ``-filter_complex`` for the FFmpeg pipe mode.

A run over a timeline where one segment changed therefore renders exactly one
segment (a 95% cache hit on a 20-segment timeline) and emits an immutable
:class:`CompileExecutionReceipt`.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    DEFAULT_MAX_BUFFER_BYTES,
    BoundedMemoryPipeRunner,
    CompileExecutionReceipt,
    Frame,
    FramePipelineSpec,
    MuxMode,
    StreamOutcome,
)
from nexus_ai_agent.creative.hyper_compiler.errors import HyperCompilerError
from nexus_ai_agent.creative.hyper_compiler.ffmpeg_filter_graph_compiler import (
    FFmpegFilterGraphCompiler,
)
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    PlannedSegment,
    SegmentCASPlanner,
    SegmentPlan,
    SegmentStatus,
    TimelineSegmentSpec,
    atomic_write_bytes,
)

_ORCH_MAGIC = b"NAGARTL1"

#: A caller-supplied frame source for one segment (streamed, never fully held).
FrameFactory = Callable[[TimelineSegmentSpec], Iterable[Frame]]
PipeSpecFactory = Callable[[TimelineSegmentSpec], FramePipelineSpec]


def default_pipe_spec(spec: TimelineSegmentSpec) -> FramePipelineSpec:
    return FramePipelineSpec(width=spec.width, height=spec.height, fps=spec.fps)


@dataclass(frozen=True)
class SegmentRender:
    """Per-segment outcome of one orchestrator run."""

    segment_id: str
    cas_key: str
    from_cache: bool
    payload_digest: str
    payload_size: int


@dataclass(frozen=True)
class TimelineExecution:
    """The full result of executing a timeline (plan + receipt + artifacts)."""

    plan: SegmentPlan
    receipt: CompileExecutionReceipt
    output_digest: str
    output_size: int
    segments: tuple[SegmentRender, ...]
    output_path: Path | None


class HyperTimelineOrchestrator:
    """Run and verify a timeline through the segment-CAS hyper-compiler."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        hmac_key: bytes | None = None,
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.planner = SegmentCASPlanner(self.cache_dir, hmac_key=hmac_key)
        self.compiler = FFmpegFilterGraphCompiler()
        self.runner = BoundedMemoryPipeRunner(
            max_buffer_bytes=max_buffer_bytes, ffmpeg_bin=ffmpeg_bin
        )

    def plan(self, timeline: Sequence[TimelineSegmentSpec]) -> SegmentPlan:
        return self.planner.plan(timeline)

    def render_segment(
        self,
        planned: PlannedSegment,
        frames: Iterable[Frame],
        *,
        pipe_spec: FramePipelineSpec | None = None,
    ) -> StreamOutcome:
        """Render one segment's frames under the memory ceiling and cache it."""
        spec = pipe_spec or default_pipe_spec(planned.spec)
        outcome = self.runner.stream_many([frames], spec)
        self.planner.write_segment_artifact(planned.cas_key, outcome.container)
        return outcome

    def _payload_for(
        self,
        planned: PlannedSegment,
        frame_factory: FrameFactory | None,
        pipe_spec_factory: PipeSpecFactory | None,
    ) -> tuple[bytes, bool, int]:
        """Return ``(payload, from_cache, peak_buffer_bytes)`` for one segment."""
        if planned.status is SegmentStatus.CACHE_HIT:
            return self.planner.verify_segment_artifact(planned.cas_key), True, 0
        if frame_factory is None:
            raise HyperCompilerError(
                f"segment {planned.spec.segment_id} needs a render but no frame_factory was given"
            )
        pipe_spec = (
            pipe_spec_factory(planned.spec)
            if pipe_spec_factory is not None
            else default_pipe_spec(planned.spec)
        )
        outcome = self.render_segment(planned, frame_factory(planned.spec), pipe_spec=pipe_spec)
        return outcome.container, False, outcome.peak_buffer_bytes

    def execute(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        frame_factory: FrameFactory | None = None,
        pipe_spec_factory: PipeSpecFactory | None = None,
        output_path: Path | None = None,
        mode: MuxMode = MuxMode.DETERMINISTIC_CPU_MUX,
    ) -> TimelineExecution:
        """Plan, render the diff, stream to ``output_path`` and receipt the run."""
        plan = self.plan(timeline)
        segments: list[SegmentRender] = []
        peak = 0
        out_hasher = hashlib.sha256()
        out_hasher.update(_ORCH_MAGIC)
        out_hasher.update(bytes.fromhex(plan.merkle_root))
        out_hasher.update(struct.pack(">Q", plan.total_leaves))

        body = bytearray()
        body.extend(_ORCH_MAGIC)
        body.extend(bytes.fromhex(plan.merkle_root))
        body.extend(struct.pack(">Q", plan.total_leaves))

        for planned in plan.segments:
            payload, from_cache, seg_peak = self._payload_for(
                planned, frame_factory, pipe_spec_factory
            )
            peak = max(peak, seg_peak)
            digest = hashlib.sha256(payload).digest()
            out_hasher.update(planned.cas_key.encode("ascii"))
            out_hasher.update(digest)
            record = (
                planned.cas_key.encode("ascii") + struct.pack(">Q", len(payload)) + digest + payload
            )
            body.extend(record)
            segments.append(
                SegmentRender(
                    segment_id=planned.spec.segment_id,
                    cas_key=planned.cas_key,
                    from_cache=from_cache,
                    payload_digest=digest.hex(),
                    payload_size=len(payload),
                )
            )

        container = bytes(body)
        output_digest = hashlib.sha256(container).hexdigest()
        if output_path is not None:
            atomic_write_bytes(Path(output_path), container)

        receipt = self.runner.build_receipt(
            timeline_merkle_root=plan.merkle_root,
            total_segments=plan.total_leaves,
            cache_hits=plan.cache_hits,
            rendered_segments=plan.render_required,
            peak_buffer_bytes=peak,
            output_digest=output_digest,
            mode=mode,
        )
        return TimelineExecution(
            plan=plan,
            receipt=receipt,
            output_digest=output_digest,
            output_size=len(container),
            segments=tuple(segments),
            output_path=Path(output_path) if output_path is not None else None,
        )

    def execute_delta(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        frame_factory: FrameFactory | None = None,
        pipe_spec_factory: PipeSpecFactory | None = None,
        output_path: Path | None = None,
    ) -> TimelineExecution:
        """Re-run a timeline, re-rendering only the segments that changed."""
        return self.execute(
            timeline,
            frame_factory=frame_factory,
            pipe_spec_factory=pipe_spec_factory,
            output_path=output_path,
        )

    def verify_timeline(self, timeline: Sequence[TimelineSegmentSpec]) -> str:
        """Verify every cached artifact and return the timeline Merkle root."""
        plan = self.plan(timeline)
        for planned in plan.segments:
            if planned.status is SegmentStatus.CACHE_HIT:
                self.planner.verify_segment_artifact(planned.cas_key)
        return plan.merkle_root


__all__ = [
    "FrameFactory",
    "HyperTimelineOrchestrator",
    "PipeSpecFactory",
    "SegmentRender",
    "TimelineExecution",
    "default_pipe_spec",
]
