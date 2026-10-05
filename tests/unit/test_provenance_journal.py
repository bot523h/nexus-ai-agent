"""The durable journal: exactly-once appends, honest dedupe, crash survival."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_ai_agent.provenance.journal import (
    CausalConflictError,
    CausalJournal,
)
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


class TestEvidenceConflicts:
    def test_identical_evidence_redelivery_is_an_honest_duplicate(self, tmp_path: Path) -> None:
        """Same key + same claims (different timestamp) = retry, absorbed."""
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        first = journal.append(
            _event_typed(
                kind=EventKind.JOB_COMPLETED,
                attempt=1,
                status="completed",
                result_digest="sha256:" + ("a" * 64),
                occurred_at="2026-10-05T10:00:00+00:00",
            )
        )
        retry = journal.append(
            _event_typed(
                kind=EventKind.JOB_COMPLETED,
                attempt=1,
                status="completed",
                result_digest="sha256:" + ("a" * 64),
                occurred_at="2026-10-05T10:00:05+00:00",
            )
        )
        assert not first.duplicate
        assert retry.duplicate
        assert retry.record == first.record
        assert journal.count() == 1  # no duplicate row, no conflict row
        assert verify_chain(journal.all_records()).ok

    def test_conflicting_redelivery_is_quarantined_never_absorbed(self, tmp_path: Path) -> None:
        """Same key + DIFFERENT claims: kept truth stands + durable conflict."""
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        first = journal.append(
            _event_typed(
                kind=EventKind.JOB_COMPLETED,
                attempt=1,
                status="completed",
                result_digest="sha256:" + ("a" * 64),
            )
        )
        with pytest.raises(CausalConflictError) as excinfo:
            journal.append(
                _event_typed(
                    kind=EventKind.JOB_COMPLETED,
                    attempt=1,
                    status="completed",
                    result_digest="sha256:" + ("b" * 64),  # different truth
                )
            )
        assert excinfo.value.kept_seq == first.record.seq
        records = journal.all_records()
        kinds = [record.kind for record in records]
        assert kinds == [EventKind.JOB_COMPLETED, EventKind.EVENT_CONFLICT]
        # the FIRST-recorded truth is untouched
        assert records[0].record_hash == first.record.record_hash
        conflict_payload = records[1].payload
        assert conflict_payload["detail"]["kept_seq"] == first.record.seq
        assert conflict_payload["detail"]["rejected_claims"]["result_digest"] == (
            "sha256:" + ("b" * 64)
        )
        assert verify_chain(records).ok

    def test_conflicting_redelivery_on_other_claim_fields(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        journal.append(
            _event_typed(
                kind=EventKind.JOB_FAILED,
                attempt=1,
                status="failed_terminal",
                error="render exploded",
            )
        )
        with pytest.raises(CausalConflictError):
            journal.append(
                _event_typed(
                    kind=EventKind.JOB_FAILED,
                    attempt=1,
                    status="failed_terminal",
                    error="a DIFFERENT error for the same transition",
                )
            )
        assert journal.records_for_job("job-1")[-1].kind is EventKind.EVENT_CONFLICT


def _same_event_worker(path: str, result_path: str) -> None:
    """One real OS process redelivering the SAME transition event repeatedly."""
    import traceback

    errors = []
    try:
        journal = CausalJournal(path)
        for _ in range(10):
            journal.append(
                _event_typed(
                    job_id="same-event",
                    kind=EventKind.JOB_COMPLETED,
                    attempt=1,
                    status="completed",
                    result_digest="sha256:" + ("c" * 64),
                )
            )
    except Exception:
        errors.append(traceback.format_exc())
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump({"errors": errors}, handle)


class TestCrossProcessDedupe:
    def test_same_event_from_two_processes_yields_exactly_one_record(self, tmp_path: Path) -> None:
        """Cross-process dedupe: concurrent redelivery of one transition
        (the bot-process + CLI-drain reality over one sidecar) collapses to
        exactly one record — no second effect, no lost evidence."""
        journal_path = tmp_path / "dedupe.sqlite3"
        ctx = multiprocessing.get_context("fork")
        results = [tmp_path / f"dedupe-{w}.json" for w in range(2)]
        processes = [
            ctx.Process(target=_same_event_worker, args=(str(journal_path), str(results[w])))
            for w in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0
        for result in results:
            assert json.loads(result.read_text(encoding="utf-8"))["errors"] == []

        journal = CausalJournal(journal_path)
        records = journal.records_for_job("same-event")
        completions = [r for r in records if r.kind is EventKind.JOB_COMPLETED]
        assert len(completions) == 1, "exactly-once across processes"
        conflicts = [r for r in records if r.kind is EventKind.EVENT_CONFLICT]
        assert conflicts == [], "identical redelivery is an honest retry, not a conflict"
        assert verify_chain(records).ok


def _storm_worker(path: str, worker_id: int, per_worker: int, result_path: str) -> None:
    """One real OS process appending its own events to the shared journal."""
    import traceback

    errors = []
    appended = 0
    try:
        journal = CausalJournal(path)
        for step in range(per_worker):
            journal.append(
                _event_typed(
                    job_id=f"storm-{worker_id}-{step}",
                    kind=EventKind.JOB_ENQUEUED,
                )
            )
            appended += 1
    except Exception:
        errors.append(traceback.format_exc())
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump({"worker": worker_id, "appended": appended, "errors": errors}, handle)


class TestCrossProcessSerialization:
    def test_real_processes_never_lose_or_corrupt_records(self, tmp_path: Path) -> None:
        """4 real OS processes × 12 appends against one SQLite sidecar.

        Proves at a real process boundary (not threads, not mocks): sequence
        integrity, prev-hash correctness, no lost event under contention, and
        that the chain still verifies — then that a fresh append works after
        the contention storm (recovery).
        """
        journal_path = tmp_path / "storm.sqlite3"
        workers = 4
        per_worker = 12
        ctx = multiprocessing.get_context("fork")
        results = [tmp_path / f"result-{w}.json" for w in range(workers)]
        processes = [
            ctx.Process(
                target=_storm_worker,
                args=(str(journal_path), w, per_worker, str(results[w])),
            )
            for w in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=120)
            assert process.exitcode == 0, f"worker died with {process.exitcode}"
        for result in results:
            payload = json.loads(result.read_text(encoding="utf-8"))
            assert payload["errors"] == [], payload["errors"]
            assert payload["appended"] == per_worker

        journal = CausalJournal(journal_path)
        assert journal.count() == workers * per_worker
        verdict = verify_chain(journal.all_records())
        assert verdict.ok, verdict.reason
        head = journal.head()
        assert head is not None and head[0] == workers * per_worker

        # recovery after contention: the journal accepts new work cleanly
        after = journal.append(_event_typed(job_id="storm-after", kind=EventKind.JOB_ENQUEUED))
        assert not after.duplicate and after.record.seq == workers * per_worker + 1
        assert verify_chain(journal.all_records()).ok
