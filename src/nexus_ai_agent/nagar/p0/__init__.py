"""Nagar P0 deterministic Intent, NEL, Semantic Time, and causal contracts."""

from .causal import CausalGraph, CausalReceipt, GraphEdge, GraphNode, NodeKind
from .commit import P0CommitContext, P0CommitResult, commit_preview
from .intent import IntentResolution, IntentSource, IntentStatus, UserIntent, resolve_intent
from .nel import (
    CompileResult,
    NELAst,
    NELDiagnostic,
    TypedTrimIR,
    canonical_hash,
    canonicalize,
    compile_nel,
    parse,
)
from .runtime import P0PlanEnvelope, P0Preview, canonical_command_input, preview_nel
from .semantic_time import (
    EvidenceKind,
    EvidenceRef,
    SegmentResolution,
    SemanticSegment,
    invalidate_after_revision,
    microseconds_to_pts,
    pts_to_microseconds,
    pts_with_delta_to_microseconds,
    resolve_segment,
)

__all__ = [
    "CausalGraph",
    "CausalReceipt",
    "GraphEdge",
    "GraphNode",
    "NodeKind",
    "P0CommitContext",
    "P0CommitResult",
    "commit_preview",
    "IntentResolution",
    "IntentSource",
    "IntentStatus",
    "UserIntent",
    "resolve_intent",
    "CompileResult",
    "NELAst",
    "NELDiagnostic",
    "TypedTrimIR",
    "canonical_hash",
    "canonicalize",
    "compile_nel",
    "parse",
    "P0PlanEnvelope",
    "P0Preview",
    "canonical_command_input",
    "preview_nel",
    "EvidenceKind",
    "EvidenceRef",
    "SemanticSegment",
    "SegmentResolution",
    "invalidate_after_revision",
    "microseconds_to_pts",
    "pts_to_microseconds",
    "pts_with_delta_to_microseconds",
    "resolve_segment",
]
