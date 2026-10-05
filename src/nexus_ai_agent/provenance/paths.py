"""Durable-path conventions + the degraded-open factory (single source of truth).

Every composition root (bot process, CLI) addresses the causal journal through
this helper so all writers and readers land on the same sidecar — the same
discipline ``worker.job_queue_db_path`` enforces for the queue itself.

The open helper is the architectural answer to "evidence failure must degrade
evidence, never kill the execution plane" (D-0024): a corrupt, locked or
unavailable sidecar downgrades to ``None`` (logged), the queue runs with no
observer, and the passport plane honestly reports the missing evidence —
startup never dies for the ledger's sake.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from nexus_ai_agent.provenance.journal import CausalJournal

logger = logging.getLogger(__name__)


def causal_journal_db_path(job_queue_db_path: Path | str) -> Path:
    """The causal-journal sidecar that belongs to one job-queue sidecar.

    ``<db>.jobs.sqlite3`` → ``<db>.jobs.sqlite3.causal.sqlite3``: the ledger
    is stored NEXT TO the authority it observes, never inside it.
    """
    return Path(f"{job_queue_db_path}.causal.sqlite3")


def try_open_causal_journal(
    db_path: Path | str, *, connect_timeout: float = 30.0
) -> CausalJournal | None:
    """Open the causal journal; degrade to ``None`` (logged) when unavailable.

    Detection + logging + explicit degradation + execution continues — the
    one policy point for journal-availability failures at composition roots.
    Returns a live journal only when the sidecar opens and its schema is
    ensured; a corrupt file, an unwritable location or a lock that outlives
    ``connect_timeout`` all land here.
    """
    try:
        return CausalJournal(db_path, connect_timeout=connect_timeout)
    except (sqlite3.Error, OSError):
        logger.warning(
            "causal_journal_unavailable path=%s "
            "(evidence degraded to absent; execution continues without recording)",
            db_path,
            exc_info=True,
        )
        return None


__all__ = ["causal_journal_db_path", "try_open_causal_journal"]
