"""Chronos-specific bridge registry for staged NEXUS execution-spine work."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from .nexus_spine_adapter import (
    ChronosSpineAttempt,
    ChronosSpineExecutionReceipt,
    FencingTokenReader,
    PreVerifyHook,
    execute_chronos_staged_attempt,
)

CHRONOS_STAGED_SPINE_MASTER: Final[str] = "chronos.staged_spine_master"


# This operation is deliberately separate from the dependency-light Studio
# ChronosPort allowlist: it requires an injected authoritative fencing reader
# and must never manufacture a local token authority.
CHRONOS_SPINE_OPERATIONS: Final[frozenset[str]] = frozenset({CHRONOS_STAGED_SPINE_MASTER})


def dispatch_chronos_spine_operation(
    operation_name: str,
    workspace_root: str | Path,
    attempt: ChronosSpineAttempt,
    *,
    active_fencing_token: FencingTokenReader,
    before_verify: PreVerifyHook | None = None,
) -> ChronosSpineExecutionReceipt:
    """Dispatch only the registered staged spine operation, failing closed."""
    if operation_name not in CHRONOS_SPINE_OPERATIONS:
        raise ValueError(f"unsupported Chronos spine operation: {operation_name}")
    return execute_chronos_staged_attempt(
        workspace_root,
        attempt,
        active_fencing_token=active_fencing_token,
        before_verify=before_verify,
    )


__all__ = [
    "CHRONOS_SPINE_OPERATIONS",
    "CHRONOS_STAGED_SPINE_MASTER",
    "dispatch_chronos_spine_operation",
]
