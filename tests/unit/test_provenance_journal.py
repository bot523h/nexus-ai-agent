"""The durable journal: exactly-once appends, honest dedupe, crash survival."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, verify_chain


def _event_typed(job_id: str = "job-1", kind: EventKind = EventKind.JOB_ENQUEUED, **kw):
    return CausalEvent(
        kind=kind,
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="key-1",
        **kw,
    )


class TestAppendAndDedupe:
    def test_first_append_is_chained_from_genesis(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        result = journal.append(_event_typed())
        assert result.record is not None and not result.duplicate
        assert result.record.seq == 1
        assert result.record.prev_hash == "sha256:" + ("0" * 64)
        assert journal.head() == (1, result.record.record_hash)

    def test_transition_events_dedupe_exactly_once(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        first = journal.append(_event_typed(kind=EventKind.JOB_COMPLETED, attempt=1))
        retry_after_lost_ack = journal.append(_event_typed(kind=EventKind.JOB_COMPLETED, attempt=1))
        assert not first.duplicate
        assert retry_after_lost_ack.duplicate
        assert retry_after_lost_ack.record == first.record
        assert journal.count() == 1

    def test_different_attempts_never_collapse(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        journal.append(_event_typed(kind=EventKind.JOB_RESERVED, attempt=1))
        journal.append(_event_typed(kind=EventKind.JOB_RESERVED, attempt=2))
        assert journal.count() == 2

    def test_observations_never_dedupe(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        for _ in range(3):
            result = journal.append(_event_typed(kind=EventKind.JOB_ENQUEUE_DUPLICATE))
            assert not result.duplicate
        assert journal.count() == 3


class TestDurability:
    def test_records_survive_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "causal.sqlite3"
        journal = CausalJournal(path)
        journal.append(_event_typed())
        journal.append(_event_typed(kind=EventKind.JOB_RESERVED, attempt=1))
        reopened = CausalJournal(path)
        assert reopened.count() == 2
        assert reopened.records_for_job("job-1")[1].kind is EventKind.JOB_RESERVED

    def test_memory_journal_works(self) -> None:
        journal = CausalJournal(":memory:")
        journal.append(_event_typed())
        assert journal.count() == 1

    def test_backfilled_records_are_labeled(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        result = journal.append(_event_typed(), backfilled=True)
        payload = result.record.payload  # type: ignore[union-attr]
        assert payload["detail"]["backfilled"] is True
        assert payload["detail"]["reconstructed_from"] == "nexus_job_queue"


class TestConcurrency:
    def test_concurrent_appends_stay_serialized_and_chained(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        errors: list[Exception] = []

        def worker(worker_id: int) -> None:
            try:
                for step in range(5):
                    journal.append(
                        _event_typed(
                            job_id=f"job-{worker_id}-{step}",
                            kind=EventKind.JOB_ENQUEUED,
                        )
                    )
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        assert not errors
        assert journal.count() == 40
        verdict = verify_chain(journal.all_records())
        assert verdict.ok
        assert verdict.head_hash == journal.head()[1]


class TestCorruptionDetection:
    def test_editted_row_is_caught_by_chain_verification(self, tmp_path: Path) -> None:
        """A direct DB write (out-of-process attacker) breaks the chain."""
        path = tmp_path / "causal.sqlite3"
        journal = CausalJournal(path)
        journal.append(_event_typed())
        journal.append(_event_typed(kind=EventKind.JOB_RESERVED, attempt=1))

        connection = sqlite3.connect(path)
        row = connection.execute(
            "SELECT seq, record_json FROM nexus_causal_journal WHERE seq = 1"
        ).fetchone()
        tampered = row[1].replace("job_enqueued", "job_completed")
        connection.execute(
            "UPDATE nexus_causal_journal SET record_json = ? WHERE seq = 1", (tampered,)
        )
        connection.commit()
        connection.close()

        reopened = CausalJournal(path)
        verdict = verify_chain(reopened.all_records())
        assert not verdict.ok
        assert verdict.first_broken_seq == 1
