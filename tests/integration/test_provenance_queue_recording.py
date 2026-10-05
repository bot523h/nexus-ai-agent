"""The queue→ledger contract: every durable transition, recorded, fail-safe.

These are the failure proofs the Foundation-Convergence contract demands at
the real queue/storage boundary — not boolean unit branches:

* duplicate enqueue under one idempotency key → no second logical job, and
  the duplicate IS an observation record (the ACK-loss/duplicate story);
* payload conflict → first dispatch wins, durably observable;
* execution crash → typed durable failure AND a causal JOB_FAILED record;
* a broken ledger NEVER breaks a job — degradation is logged and visible to
  the passport as missing history;
* takeover/fencing: a stale attempt cannot journal a completion; recovery
  (``resume_pending``) leaves an explicit takeover record;
* backfill reconstructs crash-window holes with explicit labels — and the
  passport downgrades, never pretends.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.provenance import (
    CausalEvent,
    CausalJournal,
    EventKind,
    PassportBuilder,
    PassportStatus,
    QueueLedgerObserver,
    verify_chain,
)
from nexus_ai_agent.provenance.backfill import backfill_journal


def _ok_verifier(payload: dict, result: dict) -> VerificationOutcome:
    return VerificationOutcome(
        ok=True,
        reason_code=None,
        summary={
            "status": "verified",
            "logical_identity": {"project_id": "shot-k1", "output_asset_id": "a"},
            "spec_identity": {"operation": "timeline.trim"},
            "physical_identity": {
                "path": result["artifact_path"],
                "sha256": result["sha256"],
                "size_bytes": result["size_bytes"],
            },
            "probe": None,
        },
    )


def _ok_result(tmp_path: Path) -> dict[str, object]:
    artifact = tmp_path / "out.mp4"
    artifact.write_bytes(b"\x00\x01\x02fake-mp4-bytes")
    return {
        "success": True,
        "operation": "timeline.trim",
        "artifact_path": str(artifact),
        "artifact_kind": "video",
        "sha256": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size_bytes": artifact.stat().st_size,
    }


def _payload(key: str = "k1", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "idempotency_key": key,
        "operation": "timeline.trim",
        "user_id": 7,
        "chat_id": 9,
    }
    base.update(over)
    return base


async def _wait_terminal(queue: InProcessJobQueue, job_id: str) -> JobStatus:
    for _ in range(200):
        status = await queue.get_status(job_id)
        if status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
        }:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError("job never reached a terminal state")


async def _wait_record(
    journal: CausalJournal,
    job_id: str,
    kind: EventKind,
    *,
    attempt: int | None = None,
    timeout: float = 10.0,
):
    """Wait until the journal carries the transition record for a committed row.

    The observer appends strictly AFTER the durable commit, so a row can be
    observed terminal a few microseconds before its record lands (the record
    is evidence, not the truth — the row is). Tests must poll, never race.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for record in journal.records_for_job(job_id):
            if record.kind is not kind:
                continue
            if attempt is not None and record.payload.get("attempt") != attempt:
                continue
            return record
        await asyncio.sleep(0.02)
    raise AssertionError(f"record {kind.value} for {job_id} never landed")


class TestLifecycleRecording:
    async def test_full_success_lifecycle_is_recorded_in_order(self, tmp_path: Path) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)

        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=1)

        kinds = [record.kind for record in journal.records_for_job(job_id)]
        assert kinds == [
            EventKind.JOB_ENQUEUED,
            EventKind.JOB_RESERVED,
            EventKind.JOB_VERIFICATION_STARTED,
            EventKind.JOB_COMPLETED,
        ]
        verdict = verify_chain(journal.all_records())
        assert verdict.ok
        reserved = journal.records_for_job(job_id)[1]
        assert reserved.payload["attempt"] == 1
        completed = journal.records_for_job(job_id)[3]
        assert completed.payload["detail"]["artifact_sha256"].startswith("sha256:")
        assert completed.payload["attempt"] == 1

    async def test_duplicate_enqueue_records_observation_never_second_job(
        self, tmp_path: Path
    ) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)

        first = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        second = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        conflict = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="k1",
            payload=_payload(smuggled=True),
        )
        assert first == second == conflict
        assert await _wait_terminal(queue, first) is JobStatus.COMPLETED
        await _wait_record(journal, first, EventKind.JOB_COMPLETED, attempt=1)

        duplicates = [
            record
            for record in journal.records_for_job(first)
            if record.kind is EventKind.JOB_ENQUEUE_DUPLICATE
        ]
        assert len(duplicates) == 2
        assert duplicates[0].payload["detail"]["payload_conflict"] is False
        assert duplicates[1].payload["detail"]["payload_conflict"] is True
        assert duplicates[1].payload["detail"]["resolution"] == "first_dispatch_wins"
        # exactly one logical effect
        completions = [
            record
            for record in journal.records_for_job(first)
            if record.kind is EventKind.JOB_COMPLETED
        ]
        assert len(completions) == 1


class TestFailureRecording:
    async def test_execution_crash_is_durable_failure_and_causal_record(
        self, tmp_path: Path
    ) -> None:
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def boom(payload: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("render exploded")

        queue.register_handler("creative_render", boom)

        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        status = await _wait_terminal(queue, job_id)
        assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
        await _wait_record(journal, job_id, EventKind.JOB_FAILED, attempt=1)

        failed = journal.records_for_job(job_id)[-1]
        assert failed.kind is EventKind.JOB_FAILED
        assert failed.payload["attempt"] == 1
        assert "render exploded" in str(failed.payload["error"])

        # The failure's causal account verifies completely (no artifact expected).
        passport = PassportBuilder(journal, queue).build(job_id)
        assert passport.status is PassportStatus.VERIFIED

    async def test_typed_failure_record_carries_result_digest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.provenance.models import digest_of

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def refusing(payload: dict[str, object]) -> dict[str, object]:
            return {"success": False, "error_code": "creative.unsupported_operation"}

        queue.register_handler("creative_render", refusing)

        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) in {
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
        }
        await _wait_record(journal, job_id, EventKind.JOB_FAILED, attempt=1)
        failed = journal.records_for_job(job_id)[-1]
        assert failed.kind is EventKind.JOB_FAILED
        assert failed.payload["result_digest"] == digest_of(
            {"success": False, "error_code": "creative.unsupported_operation"}
        )


class TestFailSafety:
    async def test_broken_ledger_never_breaks_a_job_and_is_visible(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        class ExplodingObserver:
            def observe(self, event: object) -> None:
                raise OSError("ledger storage lost")

        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", causal_observer=ExplodingObserver())

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)

        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        with caplog.at_level("WARNING"):
            assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        assert any("causal_ledger_observe_failed" in record.message for record in caplog.records)
        # The degradation is visible to the provenance plane: the row is
        # COMPLETED, the journal has nothing — the passport refuses to
        # certify an unrecorded history.
        passport = PassportBuilder(CausalJournal(":memory:"), queue).build(job_id)
        assert passport.status is PassportStatus.INCOMPLETE
        assert any(finding.code == "missing_transition_record" for finding in passport.findings)


class TestTakeoverAndFencing:
    async def test_takeover_is_recorded_and_stale_attempt_cannot_complete(
        self, tmp_path: Path
    ) -> None:
        journal_path = tmp_path / "causal.sqlite3"
        journal = CausalJournal(journal_path)
        release_first = asyncio.Event()
        first_started = asyncio.Event()

        def make_queue() -> InProcessJobQueue:
            queue = InProcessJobQueue(
                tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
            )

            async def slow_handler(payload: dict[str, object]) -> dict[str, object]:
                first_started.set()
                await release_first.wait()
                return _ok_result(tmp_path)

            queue.register_handler("creative_render", slow_handler)
            queue.register_artifact_verifier("creative_render", _ok_verifier)
            return queue

        owner = make_queue()
        job_id = await owner.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        await asyncio.wait_for(first_started.wait(), timeout=5)
        assert await owner.get_status(job_id) is JobStatus.PROCESSING

        # Recovery (a new "process" over the same sidecar) takes the row over.
        successor = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def fast_handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        successor.register_handler("creative_render", fast_handler)
        successor.register_artifact_verifier("creative_render", _ok_verifier)
        taken = await successor.resume_pending()
        assert taken == [job_id]
        assert await _wait_terminal(successor, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=2)

        # The stale owner finally finishes — its CAS must reject (fencing).
        # A slow stale owner can never change the journal: every transition
        # it attempts loses the fencing CAS and records nothing. Await the
        # task itself — deterministic and bounded, no polling ambiguity.
        release_first.set()
        stale_task = owner._tasks.get(job_id)
        if stale_task is not None:
            await asyncio.wait_for(asyncio.shield(stale_task), timeout=10)

        kinds = [record.kind for record in journal.records_for_job(job_id)]
        assert kinds == [
            EventKind.JOB_ENQUEUED,
            EventKind.JOB_RESERVED,  # attempt 1 (the stale owner)
            EventKind.JOB_TAKEOVER,  # recovery, explicitly recorded
            EventKind.JOB_RESERVED,  # attempt 2 (the successor)
            EventKind.JOB_VERIFICATION_STARTED,
            EventKind.JOB_COMPLETED,  # attempt 2 ONLY
        ]
        completions = [
            record
            for record in journal.records_for_job(job_id)
            if record.kind is EventKind.JOB_COMPLETED
        ]
        assert len(completions) == 1
        assert completions[0].payload["attempt"] == 2

        passport = PassportBuilder(journal, successor).build(job_id)
        assert passport.status is PassportStatus.VERIFIED
        note_codes = {finding.code for finding in passport.findings}
        assert "earlier_attempts_unrecorded" in note_codes


class TestBackfill:
    async def test_crash_window_backfill_is_labeled_and_downgrades(self, tmp_path: Path) -> None:
        # The job ran while the ledger was down (crash window): durable row,
        # empty journal.
        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        assert journal.count() == 0

        first = backfill_journal(journal, queue, queue.job_ids())
        assert first.jobs_examined == 1
        assert first.appended == 4  # enqueued + reserved(1) + verifying(1) + completed(1)
        second = backfill_journal(journal, queue, queue.job_ids())
        assert second.appended == 0
        assert second.already_present == 4  # idempotent under lost ACKs

        assert verify_chain(journal.all_records()).ok
        passport = PassportBuilder(journal, queue).build(job_id)
        # Honest downgrade: reconstructed history never certifies as VERIFIED.
        assert passport.status is PassportStatus.VERIFIED_WITH_LIMITATIONS
        assert all(
            record.payload["detail"]["backfilled"] is True
            for record in journal.records_for_job(job_id)
        )


class TestTakeoverTruthfulness:
    async def test_pending_row_that_was_never_owned_records_no_takeover(
        self, tmp_path: Path
    ) -> None:
        """B: re-listing a never-reserved pending row is scheduling, NOT an
        ownership transfer — the journal must not fabricate a takeover."""
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        creator = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        creator.register_handler("creative_render", handler)
        creator.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await creator.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        # Cancel the task BEFORE it ever runs: the durable row stays
        # pending with attempt=0 — a row no execution ever owned.
        creator._tasks[job_id].cancel()
        await asyncio.sleep(0)
        assert await creator.get_status(job_id) is JobStatus.PENDING
        assert all(
            record.kind is not EventKind.JOB_TAKEOVER for record in journal.records_for_job(job_id)
        )

        # Restart semantics: a fresh process resumes the pending row.
        successor = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )
        successor.register_handler("creative_render", handler)
        successor.register_artifact_verifier("creative_render", _ok_verifier)
        taken = await successor.resume_pending()
        assert taken == [job_id]
        assert await _wait_terminal(successor, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=1)

        kinds = [record.kind for record in journal.records_for_job(job_id)]
        assert kinds == [
            EventKind.JOB_ENQUEUED,
            EventKind.JOB_RESERVED,  # the FIRST reservation ever minted
            EventKind.JOB_VERIFICATION_STARTED,
            EventKind.JOB_COMPLETED,
        ]
        assert not any(
            record.kind is EventKind.JOB_TAKEOVER for record in journal.records_for_job(job_id)
        )
        passport = PassportBuilder(journal, successor).build(job_id)
        assert passport.status is PassportStatus.VERIFIED

    async def test_shutdown_of_inflight_records_reopen_not_takeover(self, tmp_path: Path) -> None:
        """Cancellation already reopened the row (JOB_REOPENED); shutdown's
        re-list of the now-pending row must not add a fictitious takeover,
        and the fencing token stays in the durable record."""
        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )
        started = asyncio.Event()

        async def blocking(payload: dict[str, object]) -> dict[str, object]:
            started.set()
            await asyncio.Event().wait()  # cancelled by shutdown

        queue.register_handler("creative_render", blocking)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.05)  # let the reservation record land
        await queue.shutdown()

        kinds = [record.kind for record in journal.records_for_job(job_id)]
        assert kinds == [
            EventKind.JOB_ENQUEUED,
            EventKind.JOB_RESERVED,
            EventKind.JOB_REOPENED,  # the honest cancellation record
        ]
        assert not any(kind is EventKind.JOB_TAKEOVER for kind in kinds)


class TestCorruptAuthorityRow:
    async def test_unparseable_status_degrades_evidence_without_crashing(
        self, tmp_path: Path
    ) -> None:
        """A: unknown status spelling → get_job_facts degrades explicitly
        (status_known=False), the passport says COMPROMISED (an impossible
        persisted state), and backfill reconstructs nothing it cannot prove."""
        import sqlite3 as _sqlite3

        from nexus_ai_agent.provenance import PassportStatus

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=1)

        connection = _sqlite3.connect(tmp_path / "jobs.sqlite3")
        connection.execute(
            "UPDATE nexus_job_queue SET status = 'garbage_status' WHERE id = ?",
            (job_id,),
        )
        connection.commit()
        connection.close()

        facts = queue.get_job_facts(job_id)
        assert facts is not None
        assert facts.status_known is False
        assert facts.status == "garbage_status"  # raw spelling surfaced

        passport = PassportBuilder(journal, queue).build(job_id)
        assert passport.status is PassportStatus.COMPROMISED
        assert any(f.code == "unparseable_row_status" for f in passport.findings)

        fresh = CausalJournal(tmp_path / "backfill.sqlite3")
        report = backfill_journal(fresh, queue, [job_id])
        # Only the enqueue is provable from a corrupt row — nothing invented:
        assert report.appended == 1
        kinds = [record.kind for record in fresh.records_for_job(job_id)]
        assert kinds == [EventKind.JOB_ENQUEUED]


class TestCausalCompleteness:
    async def test_completed_without_verification_started_is_never_verified(
        self, tmp_path: Path
    ) -> None:
        """E: the verification phase is part of the canonical successful
        chain — a journal that omits its record must NOT certify VERIFIED."""
        from nexus_ai_agent.provenance import PassportStatus

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=1)

        # A journal that lost exactly the verification-started record:
        incomplete = CausalJournal(tmp_path / "incomplete.sqlite3")
        for record in journal.records_for_job(job_id):
            payload = record.payload
            if payload["kind"] == "job_verification_started":
                continue
            incomplete.append(
                CausalEvent(
                    kind=EventKind(payload["kind"]),
                    job_id=job_id,
                    job_type=payload["job_type"],
                    idempotency_key=payload["idempotency_key"],
                    attempt=payload.get("attempt"),
                    status=payload.get("status"),
                    payload_digest=payload.get("payload_digest"),
                    result_digest=payload.get("result_digest"),
                    occurred_at=payload["occurred_at"],
                )
            )
        passport = PassportBuilder(incomplete, queue).build(job_id)
        assert passport.status is PassportStatus.INCOMPLETE
        missing = [
            f
            for f in passport.findings
            if f.code == "missing_transition_record" and "job_verification_started" in f.detail
        ]
        assert missing, "verification-started gap must be named explicitly"

    async def test_truncated_journal_tail_is_incomplete_not_verified(self, tmp_path: Path) -> None:
        """Tail truncation: the prefix verifies, but the affected job's
        account is incomplete — the completeness check catches it."""
        import sqlite3 as _sqlite3

        from nexus_ai_agent.provenance import PassportStatus

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=1)

        connection = _sqlite3.connect(journal.db_path)
        connection.execute(
            "DELETE FROM nexus_causal_journal WHERE job_id = ? AND kind IN ('job_completed')",
            (job_id,),
        )
        connection.commit()
        connection.close()

        passport = PassportBuilder(journal, queue).build(job_id)
        assert passport.status is PassportStatus.INCOMPLETE
        assert any(f.code == "missing_transition_record" for f in passport.findings)


class TestRealJournalFailureDuringExecution:
    async def test_wrong_schema_journal_degrades_execution_stays_green(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C with a REAL broken journal (not a fake observer): the sidecar
        exists but its schema is foreign — every record attempt fails, the
        job still completes, and the degradation is logged."""
        import sqlite3 as _sqlite3

        path = tmp_path / "broken.sqlite3"
        journal = CausalJournal(path)  # opens healthy...
        # ...then the file is externally replaced by a foreign-schema
        # database AFTER construction (corruption between init and execution):
        connection = _sqlite3.connect(path)
        connection.execute("DROP TABLE nexus_causal_journal")
        connection.execute("CREATE TABLE nexus_causal_journal (x TEXT)")
        connection.commit()
        connection.close()
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )

        async def handler(payload: dict[str, object]) -> dict[str, object]:
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", handler)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        with caplog.at_level("WARNING"):
            assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        assert any("causal_ledger_observe_failed" in record.message for record in caplog.records)


class TestMultiAttemptPassport:
    async def test_retryable_failure_then_retry_is_verified_not_compromised(
        self, tmp_path: Path
    ) -> None:
        """The real retry lifecycle, real queue + real verifier lane:

        attempt 1 fails retryable -> operator requeue -> attempt 2 completes.
        The passport must treat attempt 2 as the current truth, keep attempt
        1's failure as history (never a divergence), reconcile the digests
        with the right attempt, and find the chain intact.
        """
        import sqlite3 as _sqlite3

        journal = CausalJournal(tmp_path / "causal.sqlite3")
        queue = InProcessJobQueue(
            tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
        )
        calls = {"n": 0}

        async def flaky(payload: dict[str, object]) -> dict[str, object]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient render outage")
            return _ok_result(tmp_path)

        queue.register_handler("creative_render", flaky)
        queue.register_artifact_verifier("creative_render", _ok_verifier)
        job_id = await queue.enqueue(
            job_type="creative_render", idempotency_key="k1", payload=_payload()
        )
        assert await _wait_terminal(queue, job_id) is JobStatus.FAILED_RETRYABLE
        await _wait_record(journal, job_id, EventKind.JOB_FAILED, attempt=1)

        # The sanctioned operator requeue (same entrypoint the lifecycle
        # tests use): row back to pending, then resumed -> attempt 2.
        with _sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            connection.execute(
                "UPDATE nexus_job_queue SET status = 'pending' WHERE id = ?",
                (job_id,),
            )
        assert await queue.resume_pending() == [job_id]
        assert await _wait_terminal(queue, job_id) is JobStatus.COMPLETED
        await _wait_record(journal, job_id, EventKind.JOB_COMPLETED, attempt=2)

        passport = PassportBuilder(journal, queue).build(job_id)
        assert passport.status is PassportStatus.VERIFIED, passport.findings
        codes = [f.code for f in passport.findings]
        assert "status_divergence" not in codes
        assert "result_digest_divergence" not in codes
        assert "event_conflict_recorded" not in codes

        # History: exactly one failure (attempt 1) and one completion
        # (attempt 2) — both retained, chain intact.
        records = journal.records_for_job(job_id)
        failures = [r for r in records if r.kind is EventKind.JOB_FAILED]
        completions = [r for r in records if r.kind is EventKind.JOB_COMPLETED]
        assert len(failures) == 1 and failures[0].payload.get("attempt") == 1
        assert len(completions) == 1 and completions[0].payload.get("attempt") == 2
        assert verify_chain(records).ok
        assert verify_chain(journal.all_records()).ok
