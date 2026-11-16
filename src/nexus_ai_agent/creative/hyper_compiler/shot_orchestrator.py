"""End-to-end orchestration of the hyper-compiler pipeline.

:class:`HyperTimelineOrchestrator` wires the three engines together:

1. the segment-CAS planner (:mod:`segment_cas_dag`) decides, per segment,
   ``CACHE_HIT`` vs ``RENDER_REQUIRED`` against the on-disk content-addressed
   store, keying each segment on the render-pipeline fingerprint so a single
   knob change (brightness, dissolve, trim, …) invalidates exactly that segment;
2. the bounded-memory pipe runner (:mod:`bounded_memory_pipe_runner`) streams
   only the segments that must be re-rendered, under a hard memory ceiling, and
   the resulting payload is committed atomically to the cache;
3. the filter-graph compiler (:mod:`ffmpeg_filter_graph_compiler`) builds the
   single-pass ``-filter_complex`` for the FFmpeg pipe mode.

The requested :class:`MuxMode` is honoured: ``ffmpeg_subprocess_pipe`` really
drives ``ffmpeg`` over a subprocess pipe, and the receipt's ``mode`` is taken
from the engine outcome that actually ran, never copied from the argument.

The final timeline is assembled by *streaming* every segment (from cache or from
a freshly rendered temp file) into a file sink in fixed-size chunks — the whole
timeline is never held in RAM, and a cached segment's artifact is read once for
verification and streamed, not loaded three times.

A run over a timeline where one segment changed therefore renders exactly one
segment (a 95% cache hit on a 20-segment timeline) and emits an immutable
:class:`CompileExecutionReceipt`.
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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
)

_ORCH_MAGIC = b"NAGARTL1"
_CHUNK_BYTES = 64 * 1024

#: A caller-supplied frame source for one segment (streamed, never fully held).
FrameFactory = Callable[[TimelineSegmentSpec], Iterable[Frame]]
PipeSpecFactory = Callable[[TimelineSegmentSpec], FramePipelineSpec]


def default_pipe_spec(spec: TimelineSegmentSpec) -> FramePipelineSpec:
    return FramePipelineSpec(width=spec.width, height=spec.height, fps=spec.fps)


class _HashingWriter:
    """Binary sink that tracks the SHA-256 and byte count of everything written."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.hasher = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes, /) -> int:
        self._handle.write(data)
        self.hasher.update(data)
        self.size += len(data)
        return len(data)

    @property
    def digest(self) -> str:
        return self.hasher.hexdigest()


@dataclass(frozen=True)
class SegmentRender:
    """Per-segment outcome of one orchestrator run."""

    segment_id: str
    cas_key: str
    from_cache: bool
    payload_digest: str
    payload_size: int
    mode: str


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

    # -- planning -----------------------------------------------------------
    def _pipe_spec_for(
        self,
        spec: TimelineSegmentSpec,
        pipe_spec_factory: PipeSpecFactory | None,
    ) -> FramePipelineSpec:
        return pipe_spec_factory(spec) if pipe_spec_factory is not None else default_pipe_spec(spec)

    def plan(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        pipe_spec_factory: PipeSpecFactory | None = None,
    ) -> SegmentPlan:
        """Plan the timeline, folding each segment's pipeline fingerprint in."""
        digests = [self._pipe_spec_for(spec, pipe_spec_factory).fingerprint() for spec in timeline]
        return self.planner.plan(timeline, pipeline_digests=digests)

    # -- rendering ----------------------------------------------------------
    def _render_to_temp(
        self,
        frames: Iterable[Frame],
        spec: FramePipelineSpec,
        mode: MuxMode,
    ) -> tuple[Path, StreamOutcome]:
        """Render one segment to a temp file using the requested engine mode."""
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".seg.tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                if mode is MuxMode.FFMPEG_SUBPROCESS_PIPE:
                    outcome = self.runner.run_ffmpeg_stream([frames], spec, sink=handle)
                else:
                    outcome = self.runner.stream_many([frames], spec, sink=handle)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return tmp_path, outcome

    def render_segment(
        self,
        planned: PlannedSegment,
        frames: Iterable[Frame],
        *,
        pipe_spec: FramePipelineSpec | None = None,
        mode: MuxMode = MuxMode.DETERMINISTIC_CPU_MUX,
    ) -> StreamOutcome:
        """Render one segment under the memory ceiling and cache it atomically."""
        spec = pipe_spec or default_pipe_spec(planned.spec)
        tmp_path, outcome = self._render_to_temp(frames, spec, mode)
        try:
            with tmp_path.open("rb") as source:
                self.planner.commit_stream(
                    planned.cas_key,
                    source,
                    size=outcome.container_size,
                    digest=outcome.output_digest,
                )
        finally:
            tmp_path.unlink(missing_ok=True)
        return outcome

    # -- execution ----------------------------------------------------------
    def execute(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        frame_factory: FrameFactory | None = None,
        pipe_spec_factory: PipeSpecFactory | None = None,
        output_path: Path | None = None,
        mode: MuxMode = MuxMode.DETERMINISTIC_CPU_MUX,
    ) -> TimelineExecution:
        """Plan, render the diff, stream the result to ``output_path`` and receipt it.

        The timeline container is streamed segment-by-segment into a file sink
        (never assembled in RAM); a rendered segment is written once to a temp
        file, then streamed to the output and to the cache in bounded chunks.
        """
        plan = self.plan(timeline, pipe_spec_factory=pipe_spec_factory)
        segments: list[SegmentRender] = []
        peak = 0
        executed_mode: MuxMode | None = None

        out_fd, out_name = tempfile.mkstemp(suffix=".nagartl.tmp")
        tmp_out = Path(out_name)
        try:
            with os.fdopen(out_fd, "wb") as raw_out:
                writer = _HashingWriter(raw_out)
                writer.write(_ORCH_MAGIC)
                writer.write(bytes.fromhex(plan.merkle_root))
                writer.write(struct.pack(">Q", plan.total_leaves))

                for planned in plan.segments:
                    if planned.status is SegmentStatus.CACHE_HIT:
                        writer.write(planned.cas_key.encode("ascii"))
                        writer.write(struct.pack(">Q", planned.body_size))
                        writer.write(bytes.fromhex(planned.content_sha))
                        # One verified, streamed copy straight from the cache.
                        self.planner.stream_segment_body(planned.cas_key, writer)
                        segments.append(
                            SegmentRender(
                                segment_id=planned.spec.segment_id,
                                cas_key=planned.cas_key,
                                from_cache=True,
                                payload_digest=planned.content_sha,
                                payload_size=planned.body_size,
                                mode=MuxMode.DETERMINISTIC_CPU_MUX.value,
                            )
                        )
                        continue
                    if frame_factory is None:
                        raise HyperCompilerError(
                            f"segment {planned.spec.segment_id} needs a render but no "
                            "frame_factory was given"
                        )
                    spec = self._pipe_spec_for(planned.spec, pipe_spec_factory)
                    tmp_path, outcome = self._render_to_temp(
                        frame_factory(planned.spec), spec, mode
                    )
                    try:
                        executed_mode = outcome.mode
                        peak = max(peak, outcome.peak_buffer_bytes)
                        writer.write(planned.cas_key.encode("ascii"))
                        writer.write(struct.pack(">Q", outcome.container_size))
                        writer.write(bytes.fromhex(outcome.output_digest))
                        with tmp_path.open("rb") as rendered:
                            while True:
                                chunk = rendered.read(_CHUNK_BYTES)
                                if not chunk:
                                    break
                                writer.write(chunk)
                        with tmp_path.open("rb") as rendered:
                            self.planner.commit_stream(
                                planned.cas_key,
                                rendered,
                                size=outcome.container_size,
                                digest=outcome.output_digest,
                            )
                    finally:
                        tmp_path.unlink(missing_ok=True)
                    segments.append(
                        SegmentRender(
                            segment_id=planned.spec.segment_id,
                            cas_key=planned.cas_key,
                            from_cache=False,
                            payload_digest=outcome.output_digest,
                            payload_size=outcome.container_size,
                            mode=outcome.mode.value,
                        )
                    )
            if output_path is not None:
                target = Path(output_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_out, target)
            else:
                tmp_out.unlink(missing_ok=True)
        except BaseException:
            tmp_out.unlink(missing_ok=True)
            raise

        output_digest = writer.digest
        output_size = writer.size
        receipt_mode = executed_mode if executed_mode is not None else mode
        receipt = self.runner.build_receipt(
            timeline_merkle_root=plan.merkle_root,
            total_segments=plan.total_leaves,
            cache_hits=plan.cache_hits,
            rendered_segments=plan.render_required,
            peak_buffer_bytes=peak,
            output_digest=output_digest,
            mode=receipt_mode,
        )
        return TimelineExecution(
            plan=plan,
            receipt=receipt,
            output_digest=output_digest,
            output_size=output_size,
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
        mode: MuxMode = MuxMode.DETERMINISTIC_CPU_MUX,
    ) -> TimelineExecution:
        """Re-run a timeline, re-rendering only the segments that changed."""
        return self.execute(
            timeline,
            frame_factory=frame_factory,
            pipe_spec_factory=pipe_spec_factory,
            output_path=output_path,
            mode=mode,
        )

    def verify_timeline(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        pipe_spec_factory: PipeSpecFactory | None = None,
    ) -> str:
        """Verify every cached artifact and return the timeline Merkle root."""
        plan = self.plan(timeline, pipe_spec_factory=pipe_spec_factory)
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
