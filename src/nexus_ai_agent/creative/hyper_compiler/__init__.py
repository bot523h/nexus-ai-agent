"""Hyper-Compiler & Segment-CAS streaming engine.

A single-pass FFmpeg filter-graph compiler, a content-addressed segment DAG that
re-renders only what changed, and a hard-bounded streaming frame runner — wired
together by :class:`HyperTimelineOrchestrator`.
"""

from __future__ import annotations

from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    DEFAULT_MAX_BUFFER_BYTES,
    BoundedMemoryPipeRunner,
    CompileExecutionReceipt,
    FramePipelineSpec,
    MuxMode,
    RingBufferFrameStore,
    StreamOutcome,
    apply_tone,
    dissolve,
)
from nexus_ai_agent.creative.hyper_compiler.errors import (
    HyperCompilerError,
    InvalidFilterGraphError,
    MemoryBudgetExceededError,
    PipeExecutionError,
    SegmentIntegrityError,
)
from nexus_ai_agent.creative.hyper_compiler.ffmpeg_filter_graph_compiler import (
    ColorGradeEqOp,
    CompiledFilterGraph,
    CrossfadeAudioOp,
    CrossfadeVideoOp,
    FFmpegFilterGraphCompiler,
    FilmGrainOp,
    FilterOp,
    GraphInput,
    LoudnessNormOp,
    LUT3DOp,
    ScalePadOp,
    StreamKind,
    TrimOp,
    VignetteOp,
)
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    PlannedSegment,
    SegmentCASPlanner,
    SegmentPlan,
    SegmentStatus,
    TimelineSegmentSpec,
    VerifiedSegment,
    canonical_spec_bytes,
    merkle_root,
    merkle_root_hex,
    segment_cas_key,
    timeline_merkle_root,
    validate_cas_key,
)
from nexus_ai_agent.creative.hyper_compiler.shot_orchestrator import (
    HyperTimelineOrchestrator,
    SegmentRender,
    TimelineExecution,
    default_pipe_spec,
)

__all__ = [
    "DEFAULT_MAX_BUFFER_BYTES",
    "BoundedMemoryPipeRunner",
    "ColorGradeEqOp",
    "CompileExecutionReceipt",
    "CompiledFilterGraph",
    "CrossfadeAudioOp",
    "CrossfadeVideoOp",
    "FFmpegFilterGraphCompiler",
    "FilmGrainOp",
    "FilterOp",
    "FramePipelineSpec",
    "GraphInput",
    "HyperCompilerError",
    "HyperTimelineOrchestrator",
    "InvalidFilterGraphError",
    "LUT3DOp",
    "LoudnessNormOp",
    "MemoryBudgetExceededError",
    "MuxMode",
    "PipeExecutionError",
    "PlannedSegment",
    "RingBufferFrameStore",
    "ScalePadOp",
    "SegmentCASPlanner",
    "SegmentIntegrityError",
    "SegmentPlan",
    "SegmentRender",
    "SegmentStatus",
    "StreamKind",
    "StreamOutcome",
    "TimelineExecution",
    "TimelineSegmentSpec",
    "TrimOp",
    "VerifiedSegment",
    "VignetteOp",
    "apply_tone",
    "canonical_spec_bytes",
    "default_pipe_spec",
    "dissolve",
    "merkle_root",
    "merkle_root_hex",
    "segment_cas_key",
    "timeline_merkle_root",
    "validate_cas_key",
]
