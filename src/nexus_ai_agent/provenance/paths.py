"""Durable-path conventions for the provenance plane (single source of truth).

Every composition root (bot process, CLI) addresses the causal journal through
this helper so all writers and readers land on the same sidecar — the same
discipline ``worker.job_queue_db_path`` enforces for the queue itself.
"""

from __future__ import annotations

from pathlib import Path


def causal_journal_db_path(job_queue_db_path: Path | str) -> Path:
    """The causal-journal sidecar that belongs to one job-queue sidecar.

    ``<db>.jobs.sqlite3`` → ``<db>.jobs.sqlite3.causal.sqlite3``: the ledger
    is stored NEXT TO the authority it observes, never inside it.
    """
    return Path(f"{job_queue_db_path}.causal.sqlite3")


__all__ = ["causal_journal_db_path"]
