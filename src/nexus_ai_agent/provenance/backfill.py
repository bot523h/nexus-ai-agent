"""Crash-window recovery: reconstruct missing ledger history, explicitly labeled.

The journal does not share the queue's transaction (by design — the ledger
must never gain authority over job outcomes, and a shared transaction would
let ledger failure veto execution). The cost is a real gap window: a crash
between the queue's durable commit and the journal commit leaves the record
unwritten.

This module closes that gap the only honest way: it re-derives the events the
authoritative row *proves* (creation + the terminal transition of the winning
attempt) and appends them flagged ``backfilled=True`` with
``reconstructed_from="nexus_job_queue"``. A backfilled record is durable
evidence about the fact — never a claim about having been written at event
time — and the passport downgrades any history containing one to
``VERIFIED_WITH_LIMITATIONS``.

Dedupe makes this idempotent: re-running backfill after another crash appends
nothing that already exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts
from nexus_ai_agent.provenance.observer import utc_now_iso
from nexus_ai_agent.provenance.passport import JobFactsProvider

_IN_FLIGHT = frozenset({"pending", "processing", "verifying"})


@dataclass(frozen=True)
class BackfillReport:
    """What recovery appended vs. what was already on the ledger."""

    jobs_examined: int
    appended: int
    already_present: int


def _events_for(facts: JobFacts) -> list[tuple[EventKind, int | None]]:
    """Transitions the durable row state *proves* (never assumed history).

    An unparseable row status corrupts the authority itself: nothing beyond
    the row's existence (the enqueue) can be proven, so nothing else is
    reconstructed — the gap stays visible instead of being guessed.
    """
    events: list[tuple[EventKind, int | None]] = [(EventKind.JOB_ENQUEUED, None)]
    if not facts.status_known:
        return events
    if facts.status in _IN_FLIGHT:
        # Mid-flight history is NOT reconstructable from one row — only the
        # creation is provable. The remaining gap stays visible (the passport
        # reports it) instead of being invented away.
        return events
    if facts.status == "completed":
        events.append((EventKind.JOB_RESERVED, facts.attempt))
        if facts.verification is not None:
            # The durable verification block proves the verification phase
            # ran; its labeled reconstruction belongs to the account.
            events.append((EventKind.JOB_VERIFICATION_STARTED, facts.attempt))
        events.append((EventKind.JOB_COMPLETED, facts.attempt))
        return events
    if facts.attempt >= 1:
        events.append((EventKind.JOB_RESERVED, facts.attempt))
    events.append((EventKind.JOB_FAILED, facts.attempt if facts.attempt >= 1 else None))
    return events


def _reconstructed_event(
    kind: EventKind, facts: JobFacts, attempt: int | None, now: str
) -> CausalEvent:
    terminal = kind in (EventKind.JOB_COMPLETED, EventKind.JOB_FAILED)
    return CausalEvent(
        kind=kind,
        job_id=facts.job_id,
        job_type=facts.job_type,
        idempotency_key=facts.idempotency_key,
        attempt=attempt,
        status=facts.status if terminal else None,
        error=facts.error if kind is EventKind.JOB_FAILED else None,
        payload_digest=facts.payload_digest,
        result_digest=facts.result_digest if kind is EventKind.JOB_COMPLETED else None,
        occurred_at=now,
    )


def backfill_journal(
    journal: CausalJournal,
    facts_provider: JobFactsProvider,
    job_ids: Iterable[str],
) -> BackfillReport:
    """Append the labeled reconstructions the authoritative rows prove.

    Idempotent: transition dedupe skips anything already recorded, so this is
    safe to run on every startup.
    """
    examined = 0
    appended = 0
    already = 0
    for job_id in job_ids:
        facts = facts_provider.get_job_facts(job_id)
        if facts is None:
            continue
        examined += 1
        now = utc_now_iso()
        for kind, attempt in _events_for(facts):
            result = journal.append(
                _reconstructed_event(kind, facts, attempt, now), backfilled=True
            )
            if result.duplicate:
                already += 1
            else:
                appended += 1
    return BackfillReport(jobs_examined=examined, appended=appended, already_present=already)


__all__ = ["BackfillReport", "backfill_journal"]
