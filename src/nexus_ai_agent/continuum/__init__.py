"""Continuum: committed, verifiable project-state snapshot (anti-state-loss)."""

from __future__ import annotations

from nexus_ai_agent.continuum.snapshot import (
    SNAPSHOT_PATH,
    ContinuumSnapshot,
    read_snapshot,
    verify_snapshot,
    write_snapshot,
)

__all__ = [
    "ContinuumSnapshot",
    "SNAPSHOT_PATH",
    "read_snapshot",
    "verify_snapshot",
    "write_snapshot",
]
