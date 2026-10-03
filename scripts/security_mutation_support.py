"""Thin re-export of canonical security mutation support in nexus_ai_agent.continuum."""

from __future__ import annotations

from nexus_ai_agent.continuum.security_mutation_support import (
    ROOT,
    Mutation,
    MutationOutcome,
    MutationStatus,
    SourceEdit,
    classify_mutation_outcome,
    extract_error_nodeids,
    extract_failed_nodeids,
    legacy_weak_substring_killed,
    run_mutation_suite,
)

__all__ = [
    "ROOT",
    "Mutation",
    "MutationOutcome",
    "MutationStatus",
    "SourceEdit",
    "classify_mutation_outcome",
    "extract_error_nodeids",
    "extract_failed_nodeids",
    "legacy_weak_substring_killed",
    "run_mutation_suite",
]
