"""Passport fail-closed: missing JOB_RESERVED cannot yield VERIFIED.

Constructs the incomplete causal account:

  JOB_ENQUEUED + JOB_COMPLETED(attempt=N)
  (no JOB_RESERVED(attempt=N))

against an authoritative completed row and asserts the Artifact Passport
never upgrades that history to VERIFIED — the missing transition remains
explicit and certification stays fail-closed.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts
from nexus_ai_agent.provenance.observer import utc_now_iso
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus


class _FactsMap:
    def __init__(self, facts: JobFacts) -> None:
        self._facts = {facts.job_id: facts}

    def get_job_facts(self, job_id: str) -> JobFacts | None:
        return self._facts.get(job_id)


def _completed_facts(job_id: str = "job-missing-reserved") -> JobFacts:
    return JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="ik-missing-res",
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


def test_missing_reservation_cannot_reach_verified(tmp_path: Path) -> None:
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
    kinds = {(r.kind, r.payload.get("attempt")) for r in journal.records_for_job(facts.job_id)}
    assert (EventKind.JOB_RESERVED, 1) not in kinds

    passport = PassportBuilder(journal, _FactsMap(facts)).build(facts.job_id)

    assert passport.status is not PassportStatus.VERIFIED
    assert passport.status in {
        PassportStatus.INCOMPLETE,
        PassportStatus.VERIFIED_WITH_LIMITATIONS,
        PassportStatus.COMPROMISED,
    }
    codes = {f.code for f in passport.findings}
    assert "missing_transition_record" in codes
    assert any("job_reserved" in f.detail for f in passport.findings)


def test_backfill_does_not_upgrade_bare_terminal_to_verified(tmp_path: Path) -> None:
    """Re-running backfill must not invent RESERVED and unlock VERIFIED."""
    from nexus_ai_agent.provenance.backfill import backfill_journal

    journal = CausalJournal(tmp_path / "causal.sqlite3")
    facts = _completed_facts("job-backfill-cannot-heal")
    now = utc_now_iso()
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=facts.job_id,
            job_type=facts.job_type,
            idempotency_key=facts.idempotency_key,
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
    provider = _FactsMap(facts)
    backfill_journal(journal, provider, [facts.job_id])
    kinds = {(r.kind, r.payload.get("attempt")) for r in journal.records_for_job(facts.job_id)}
    assert (EventKind.JOB_RESERVED, 1) not in kinds

    passport = PassportBuilder(journal, provider).build(facts.job_id)
    assert passport.status is not PassportStatus.VERIFIED
    assert "missing_transition_record" in {f.code for f in passport.findings}
