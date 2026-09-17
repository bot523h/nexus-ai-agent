"""Cross-turn state continuity for NEXUS AI Agent.

The project's own failure mode is not lost code — every commit lives on the
remote — it is lost *context*: a new session starts from a fresh checkout and
has to rediscover which phase is done, which commit was last good, and what is
still blocked.  This package persists that context **inside the repository**
(``.nexus/continuum.json``), so it travels with the code and is reviewable in
a diff like anything else.

See :mod:`nexus_ai_agent.continuum.snapshot` for the format and the checks.
"""

from __future__ import annotations

from nexus_ai_agent.continuum.snapshot import (
    SCHEMA_VERSION,
    ContinuumError,
    ContinuumSnapshot,
    GitState,
    PendingItem,
    PhaseRecord,
    VerifyReport,
    commits_ahead,
    current_git_state,
    default_snapshot_path,
    is_ancestor,
    load,
    repo_root,
    save,
    verify,
)

__all__ = [
    "SCHEMA_VERSION",
    "ContinuumError",
    "ContinuumSnapshot",
    "GitState",
    "PendingItem",
    "PhaseRecord",
    "VerifyReport",
    "commits_ahead",
    "current_git_state",
    "default_snapshot_path",
    "is_ancestor",
    "load",
    "repo_root",
    "save",
    "verify",
]
