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

Critical honesty rule (task-231 closure): if the journal already carries a
terminal transition for an attempt (``JOB_COMPLETED`` / ``JOB_FAILED``) but
lacks the matching ``JOB_RESERVED``, backfill MUST NOT invent that reservation.
Incomplete history stays incomplete; false continuity is forbidden.

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
_TERMINAL_KINDS = frozenset({EventKind.JOB_COMPLETED, EventKind.JOB_FAILED})


@dataclass(frozen=True)
class BackfillReport:
    """What recovery appended vs. what was already on the ledger."""

    jobs_examined: int
    appended: int
    already_present: int


def _recorded_kinds(journal: CausalJournal, job_id: str) -> set[tuple[EventKind, int | None]]:
    """Kinds already durable for *job_id* (attempt-aware)."""
    found: set[tuple[EventKind, int | None]] = set()
    for record in journal.records_for_job(job_id):
        payload = record.payload
        kind_raw = payload.get("kind")
        if not kind_raw:
            continue
        try:
            kind = EventKind(kind_raw)
        except ValueError:
            continue
        attempt = payload.get("attempt")
        found.add((kind, int(attempt) if attempt is not None else None))
    return found


def _events_for(
    facts: JobFacts,
    *,
    existing: set[tuple[EventKind, int | None]],
) -> list[tuple[EventKind, int | None]]:
    """Transitions the durable row state *proves* (never assumed history).

    When a terminal record already exists for an attempt without a matching
    reservation, the reservation is **not** synthesized: inventing continuity
    after the fact is the false-coherence path this function forbids.
    """
    events: list[tuple[EventKind, int | None]] = [(EventKind.JOB_ENQUEUED, None)]
    if not facts.status_known:
        return events
    if facts.status in _IN_FLIGHT:
        return events

    attempt = facts.attempt if facts.attempt >= 1 else None
    terminal_present = any((kind, attempt) in existing for kind in _TERMINAL_KINDS)
    reserved_present = (EventKind.JOB_RESERVED, attempt) in existing

    if facts.status == "completed":
        if attempt is not None and not (terminal_present and not reserved_present):
            events.append((EventKind.JOB_RESERVED, attempt))
        if facts.verification is not None:
            events.append((EventKind.JOB_VERIFICATION_STARTED, attempt))
        events.append((EventKind.JOB_COMPLETED, attempt))
        return events

    if attempt is not None and not (terminal_present and not reserved_present):
        events.append((EventKind.JOB_RESERVED, attempt))
    events.append((EventKind.JOB_FAILED, attempt if attempt is not None else None))
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
    safe to run on every startup. Never invents a reservation for an attempt
    whose terminal transition is already on the ledger without one.
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
        existing = _recorded_kinds(journal, job_id)
        for kind, attempt in _events_for(facts, existing=existing):
            result = journal.append(
                _reconstructed_event(kind, facts, attempt, now), backfilled=True
            )
            if result.duplicate:
                already += 1
            else:
                appended += 1
    return BackfillReport(jobs_examined=examined, appended=appended, already_present=already)


__all__ = ["BackfillReport", "backfill_journal"]
