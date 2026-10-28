"""Nagar P0 deterministic Intent, NEL, Semantic Time, and causal contracts."""

from .causal import CausalGraph, CausalReceipt, GraphEdge, GraphNode, NodeKind
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
from .runtime import P0Preview, canonical_command_input, preview_nel
from .semantic_time import (
    EvidenceKind,
    SegmentResolution,
    SemanticSegment,
    invalidate_after_revision,
    resolve_segment,
)

__all__ = [
    "CausalGraph",
    "CausalReceipt",
    "GraphEdge",
    "GraphNode",
    "NodeKind",
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
    "P0Preview",
    "canonical_command_input",
    "preview_nel",
    "EvidenceKind",
    "SemanticSegment",
    "SegmentResolution",
    "invalidate_after_revision",
    "resolve_segment",
]
