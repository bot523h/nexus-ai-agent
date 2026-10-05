"""Regression: backfill must not invent JOB_RESERVED after a bare terminal.

Finding B (PR #153 closure): if the journal already holds JOB_COMPLETED(attempt=N)
without JOB_RESERVED(attempt=N), reconstruction must leave the gap visible — never
synthesize the missing reservation to invent causal continuity.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.provenance.backfill import backfill_journal
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts
from nexus_ai_agent.provenance.observer import utc_now_iso


class _FactsMap:
    def __init__(self, facts: JobFacts) -> None:
        self._facts = {facts.job_id: facts}

    def get_job_facts(self, job_id: str) -> JobFacts | None:
        return self._facts.get(job_id)


def _completed_facts(job_id: str = "job-incomplete-middle") -> JobFacts:
    return JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="ik-1",
        status="completed",
        attempt=1,
        error=None,
        created_at=None,
        started_at=None,
        finished_at=None,
        payload={},
        payload_digest="sha256:" + ("a" * 64),
        result={"ok": True},
        result_digest="sha256:" + ("b" * 64),
        verification=None,
        status_known=True,
    )


def test_completed_without_reserved_does_not_synthesize_reservation(tmp_path: Path) -> None:
    """Case A: COMPLETED exists, RESERVED missing → no synthetic RESERVED."""
    journal = CausalJournal(tmp_path / "causal.sqlite3")
    facts = _completed_facts()
    now = utc_now_iso()
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=facts.job_id,
            job_type=facts.job_type,
            idempotency_key=facts.idempotency_key,
            attempt=None,
            payload_digest=facts.payload_digest,
            occurred_at=now,
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id=facts.job_id,
            job_type=facts.job_type,
            idempotency_key=facts.idempotency_key,
            attempt=1,
            status="completed",
            payload_digest=facts.payload_digest,
            result_digest=facts.result_digest,
            occurred_at=now,
        )
    )
    before_kinds = {
        (r.payload["kind"], r.payload.get("attempt")) for r in journal.records_for_job(facts.job_id)
    }
    assert ("job_reserved", 1) not in before_kinds

    report = backfill_journal(journal, _FactsMap(facts), [facts.job_id])
    after = journal.records_for_job(facts.job_id)
    kinds = [(r.payload["kind"], r.payload.get("attempt")) for r in after]
    assert ("job_reserved", 1) not in kinds, "must not invent reservation after bare completion"
    report2 = backfill_journal(journal, _FactsMap(facts), [facts.job_id])
    assert report2.appended == 0
    assert report.jobs_examined == 1


def test_empty_journal_still_reconstructs_full_terminal_chain(tmp_path: Path) -> None:
    """Case B: ordinary crash window — neither RESERVED nor COMPLETED on journal."""
    journal = CausalJournal(tmp_path / "causal.sqlite3")
    facts = _completed_facts("job-crash-window")
    report = backfill_journal(journal, _FactsMap(facts), [facts.job_id])
    assert report.appended >= 2
    kinds = {
        (r.payload["kind"], r.payload.get("attempt")) for r in journal.records_for_job(facts.job_id)
    }
    assert ("job_enqueued", None) in kinds
    assert ("job_reserved", 1) in kinds
    assert ("job_completed", 1) in kinds
    for record in journal.records_for_job(facts.job_id):
        detail = record.payload.get("detail") or {}
        assert detail.get("backfilled") is True
        assert detail.get("reconstructed_from") == "nexus_job_queue"
