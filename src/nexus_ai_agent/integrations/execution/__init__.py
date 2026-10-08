"""Execution Fabric: provider-neutral contract + the Nexus-fenced staging path.

See :mod:`nexus_ai_agent.integrations.execution.contract` for the boundary
rules. This package is deliberately provider-neutral; provider adapters live in
sibling packages (``integrations.hatchet``) and are optional.
"""

from __future__ import annotations

from .backends import ExecutionBackend, InProcessBackend, JobHandler
from .contract import (
    BackendKind,
    ExecutionAttempt,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
    OutcomeState,
    identity_matches,
)
from .reconcile import is_stale, reconcile_result, state_from_provider
from .staging import assert_staged, staging_dir_for

__all__ = [
    "BackendKind",
    "ExecutionAttempt",
    "ExecutionBackend",
    "ExecutionFailure",
    "ExecutionIdentity",
    "ExecutionRequest",
    "ExecutionResult",
    "InProcessBackend",
    "JobHandler",
    "OutcomeState",
    "assert_staged",
    "identity_matches",
    "is_stale",
    "reconcile_result",
    "staging_dir_for",
    "state_from_provider",
]
