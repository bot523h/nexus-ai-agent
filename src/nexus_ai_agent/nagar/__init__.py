"""Nagar Operation Truth — machine-verifiable recomputation of what exists.

This package is the *engine*, not a document.  It reads live sources
(product catalogue, runtime registry, executable surface, proof evidence),
recomputes every number and every status at call time, and projects the
result.  Neither ``OPERATION_TRUTH.json`` nor any Markdown page is an input:
the stored projection is an *output* that the gate compares against a fresh
recomputation, and generated Markdown carries a machine-readable footprint of
the recomputation that produced it.

Design rule: Markdown and JSON never establish truth for each other.  A claim
is only as good as the chain

    TDD + Runtime Registry + Executable Surface + Proof Registry
        -> recompute
        -> projection (JSON, generated docs)

See ``docs/audits/OPERATION_TRUTH_2026-09-25.md`` for the audit that
introduced this engine and ``docs/OPERATION_WAVE_PLAN.md`` for the wave plan
over the computed missing set.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.sources import SourceError
from nexus_ai_agent.nagar.truth import (
    EVIDENCE_TOKENS,
    LADDER,
    PIPELINE_STAGES,
    Finding,
    build_projection,
    compare,
    invariants,
    load_projection,
    write_projection,
)

__all__ = [
    "EVIDENCE_TOKENS",
    "LADDER",
    "PIPELINE_STAGES",
    "Finding",
    "SourceError",
    "build_projection",
    "compare",
    "invariants",
    "load_projection",
    "write_projection",
]
