"""The queue→ledger bridge: record durable transitions, never break a job.

Authority law (the reason this module is tiny): the observer has **no**
execution authority. It runs strictly *after* a durable queue commit, receives
facts (never decisions), and its failure degrades the ledger — never the job.
The queue adapter owns the degradation policy
(``causal_ledger_observe_failed`` in its log); this module stays honest and
lets storage errors propagate to that single policy point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CausalObserver(Protocol):
    """What the queue adapter requires of a causal observer (structural)."""

    def observe(self, event: CausalEvent) -> None: ...


class QueueLedgerObserver:
    """Appends queue lifecycle events to a :class:`CausalJournal`."""

    def __init__(self, journal: CausalJournal) -> None:
        self._journal = journal

    def observe(self, event: CausalEvent) -> None:
        """Append one durable-transition event (sync; queue calls via thread).

        The append result is a journal concern: consumers needing the exact
        ``AppendResult`` (duplication visibility, the record itself) address
        the journal property directly. The observer's own contract to the
        queue is fire-and-forget evidence recording.
        """
        self._journal.append(event)

    @property
    def journal(self) -> CausalJournal:
        return self._journal


__all__ = ["CausalObserver", "QueueLedgerObserver", "utc_now_iso"]
