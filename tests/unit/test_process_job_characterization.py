"""Characterization contract for ``InProcessJobQueue._process_job`` (A-11).

``_process_job`` was decomposed into stage helpers (claim-time structural
failure, reservation, trim preflight, cancellation reopen, unverified and
verified completion).  These tests pin the *externally observable* behaviour
of every exit of the pipeline — durable row state, causal events, completion
notifications — so the decomposition (and any later edit) cannot drift
silently.  They were written against the pre-refactor method and must stay
green on both shapes; they deliberately drive the REAL queue (SQLite file,
real handler/verifier registration, real completion hook), never the helpers.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    VERIFICATION_RESULT_KEY,
    InProcessJobQueue,
    JobCompletion,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.failure_semantics import typed_failure_error
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.provenance.models import CausalEvent, EventKind

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
FAILED = {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


class _Ledger:
    """Records every causal event the queue emits, in order."""

    def __init__(self) -> None:
        self.events: list[CausalEvent] = []

    def observe(self, event: CausalEvent) -> None:
        self.events.append(event)

    def kinds(self, job_id: str) -> list[EventKind]:
        return [event.kind for event in self.events if event.job_id == job_id]

    def last(self, job_id: str, kind: EventKind) -> CausalEvent:
        matching = [e for e in self.events if e.job_id == job_id and e.kind is kind]
        assert matching, f"no {kind} event for {job_id}: {self.kinds(job_id)}"
        return matching[-1]


class _Harness:
    def __init__(self, db: Path) -> None:
        self.db = db
        self.ledger = _Ledger()
        self.completions: list[JobCompletion] = []
        self.queue = InProcessJobQueue(
            db, on_job_finished=self._finished, causal_observer=self.ledger
        )

    async def _finished(self, completion: JobCompletion) -> None:
        self.completions.append(completion)

    def row(self, job_id: str) -> sqlite3.Row:
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM nexus_job_queue WHERE id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return row

    async def drain(self, job_id: str, *, timeout: float = 5.0) -> JobStatus:
        async def _poll() -> JobStatus:
            while True:
                status = await self.queue.get_status(job_id)
                if status in TERMINAL:
                    return status
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(_poll(), timeout=timeout)

    async def wait_status(self, job_id: str, expected: JobStatus) -> None:
        async def _poll() -> None:
            while await self.queue.get_status(job_id) is not expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_poll(), timeout=5.0)


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path / "jobs.sqlite3")


PAYLOAD: dict[str, object] = {"value": 7, "idempotency_key": "k-1"}


# ---------------------------------------------------------------------------
# Stage 0 — claim-time structural failure (no handler for the job type)
# ---------------------------------------------------------------------------
async def test_no_handler_fails_terminal_before_any_reservation(harness: _Harness) -> None:
    job_id = await harness.queue.enqueue(job_type="ghost", idempotency_key="g-1", payload=PAYLOAD)
    assert await harness.drain(job_id) is JobStatus.FAILED_TERMINAL

    row = harness.row(job_id)
    assert "no handler registered for job type ghost" in str(row["error"])
    assert row["attempt"] == 0 and row["started_at"] is None, "never reserved"

    assert EventKind.JOB_RESERVED not in harness.ledger.kinds(job_id)
    failed = harness.ledger.last(job_id, EventKind.JOB_FAILED)
    assert failed.detail == {"phase": "claim_time_structural"}
    assert failed.status == JobStatus.FAILED_TERMINAL.value
    assert failed.attempt is None
    assert failed.idempotency_key == "g-1"
    assert failed.payload_digest is not None

    assert [c.status for c in harness.completions] == [JobStatus.FAILED_TERMINAL]
    completion = harness.completions[0]
    assert completion.job_id == job_id and completion.job_type == "ghost"
    assert completion.result is None and completion.payload == {}
    assert "no handler registered" in str(completion.error)


# ---------------------------------------------------------------------------
# Stage 1 — reservation + the row-state guards of the entry point
# ---------------------------------------------------------------------------
async def test_terminal_or_unknown_rows_are_never_executed(harness: _Harness) -> None:
    calls = 0

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness.queue.register_handler("echo", handler)
    job_id = await harness.queue.enqueue(job_type="echo", idempotency_key="e-1", payload=PAYLOAD)
    assert await harness.drain(job_id) is JobStatus.COMPLETED
    assert calls == 1
    before = list(harness.ledger.kinds(job_id))

    # A terminal row is a no-op, whatever asked for it.
    await harness.queue._process_job(job_id)
    # An unknown durable spelling is fail-closed: never executed.
    with sqlite3.connect(harness.db) as connection:
        connection.execute("UPDATE nexus_job_queue SET status = 'weird' WHERE id = ?", (job_id,))
    await harness.queue._process_job(job_id)
    # A row that does not exist is a no-op too.
    await harness.queue._process_job("does-not-exist")

    assert calls == 1
    assert harness.ledger.kinds(job_id) == before
    assert len(harness.completions) == 1


async def test_successful_run_records_reservation_before_completion(harness: _Harness) -> None:
    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"echo": payload["value"]}

    harness.queue.register_handler("echo", handler)
    job_id = await harness.queue.enqueue(job_type="echo", idempotency_key="e-2", payload=PAYLOAD)
    assert await harness.drain(job_id) is JobStatus.COMPLETED

    kinds = harness.ledger.kinds(job_id)
    assert kinds.index(EventKind.JOB_RESERVED) < kinds.index(EventKind.JOB_COMPLETED)
    reserved = harness.ledger.last(job_id, EventKind.JOB_RESERVED)
    assert reserved.attempt == 1 and reserved.status == JobStatus.PROCESSING.value
    assert reserved.idempotency_key == "e-2"
    completed = harness.ledger.last(job_id, EventKind.JOB_COMPLETED)
    assert completed.attempt == 1 and completed.result_digest is not None

    assert await harness.queue.get_result(job_id) == {"echo": 7}
    assert [(c.status, c.result, c.error, c.payload) for c in harness.completions] == [
        (JobStatus.COMPLETED, {"echo": 7}, None, PAYLOAD)
    ]


# ---------------------------------------------------------------------------
# Stage 3 — handler outcomes that are failures
# ---------------------------------------------------------------------------
async def test_handler_exception_is_classified_persisted_and_announced(
    harness: _Harness,
) -> None:
    async def handler(payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("boom")

    harness.queue.register_handler("crash", handler)
    job_id = await harness.queue.enqueue(job_type="crash", idempotency_key="c-1", payload=PAYLOAD)
    status = await harness.drain(job_id)
    assert status in FAILED

    row = harness.row(job_id)
    assert row["error"] == "boom" and row["attempt"] == 1

    kinds = harness.ledger.kinds(job_id)
    assert kinds.index(EventKind.JOB_RESERVED) < kinds.index(EventKind.JOB_FAILED)
    failed = harness.ledger.last(job_id, EventKind.JOB_FAILED)
    assert failed.error == "boom" and failed.attempt == 1 and failed.status == status.value

    assert [(c.status, c.result, c.error, c.payload) for c in harness.completions] == [
        (status, None, "boom", PAYLOAD)
    ]


async def test_non_dict_handler_result_fails_closed(harness: _Harness) -> None:
    async def handler(payload: dict[str, object]) -> Any:
        return ["not", "a", "dict"]

    harness.queue.register_handler("bad", handler)
    job_id = await harness.queue.enqueue(job_type="bad", idempotency_key="b-1", payload=PAYLOAD)
    assert await harness.drain(job_id) in FAILED
    assert harness.row(job_id)["error"] == "job handler must return a dictionary"
    assert await harness.queue.get_result(job_id) is None
    assert [c.status in FAILED for c in harness.completions] == [True]
    assert harness.completions[0].error == "job handler must return a dictionary"


async def test_typed_success_false_result_is_a_failure_and_never_verified(
    harness: _Harness,
) -> None:
    verifier_calls = 0

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationOutcome(ok=True, reason_code=None, summary={})

    typed = {"success": False, "error_code": "creative.unsupported_operation"}

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return dict(typed)

    harness.queue.register_handler("typed", handler)
    harness.queue.register_artifact_verifier("typed", verifier)
    job_id = await harness.queue.enqueue(job_type="typed", idempotency_key="t-1", payload=PAYLOAD)
    status = await harness.drain(job_id)
    assert status in FAILED
    assert verifier_calls == 0, "a self-declared failure is never verified"

    expected_error = typed_failure_error("creative.unsupported_operation")
    assert harness.row(job_id)["error"] == expected_error
    failed = harness.ledger.last(job_id, EventKind.JOB_FAILED)
    assert failed.error == expected_error and failed.result_digest is not None
    assert [(c.status, c.result, c.error) for c in harness.completions] == [
        (status, typed, expected_error)
    ]


# ---------------------------------------------------------------------------
# Cancellation during the handler — reopen only OUR row, announce nothing
# ---------------------------------------------------------------------------
async def test_cancellation_during_handler_reopens_the_row_without_notifying(
    harness: _Harness,
) -> None:
    started = asyncio.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await asyncio.sleep(3600)
        return {"ok": True}

    harness.queue.register_handler("slow", handler)
    job_id = await harness.queue.enqueue(job_type="slow", idempotency_key="s-1", payload=PAYLOAD)
    await asyncio.wait_for(started.wait(), timeout=5)
    await harness.wait_status(job_id, JobStatus.PROCESSING)

    await harness.queue.shutdown()

    row = harness.row(job_id)
    assert row["status"] == JobStatus.PENDING.value and row["attempt"] == 1
    reopened = harness.ledger.last(job_id, EventKind.JOB_REOPENED)
    assert reopened.detail == {"phase": "cancellation"}
    assert reopened.attempt == 1 and reopened.status == JobStatus.PENDING.value
    assert harness.ledger.kinds(job_id).count(EventKind.JOB_REOPENED) == 1
    assert harness.completions == [], "a cancelled attempt announces nothing"


# ---------------------------------------------------------------------------
# Stage 4b — verified lane
# ---------------------------------------------------------------------------
async def test_verifier_refusal_fails_the_job_with_the_reason(harness: _Harness) -> None:
    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": True, "artifact_path": "/nowhere"}

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(ok=False, reason_code="artifact_missing", summary={})

    harness.queue.register_handler("render", handler)
    harness.queue.register_artifact_verifier("render", verifier)
    job_id = await harness.queue.enqueue(job_type="render", idempotency_key="r-1", payload=PAYLOAD)
    status = await harness.drain(job_id)
    assert status in FAILED

    assert harness.row(job_id)["error"] == "verification_failed:artifact_missing"
    kinds = harness.ledger.kinds(job_id)
    assert kinds.index(EventKind.JOB_RESERVED) < kinds.index(EventKind.JOB_VERIFICATION_STARTED)
    assert kinds.index(EventKind.JOB_VERIFICATION_STARTED) < kinds.index(EventKind.JOB_FAILED)
    assert EventKind.JOB_COMPLETED not in kinds
    assert await harness.queue.get_result(job_id) is None
    assert [(c.status, c.result, c.error) for c in harness.completions] == [
        (status, None, "verification_failed:artifact_missing")
    ]


async def test_verifier_acceptance_completes_with_the_verification_block(
    harness: _Harness,
) -> None:
    outcome = VerificationOutcome(ok=True, reason_code=None, summary={"duration_us": 500})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": True, "artifact_path": "/somewhere"}

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return outcome

    harness.queue.register_handler("render", handler)
    harness.queue.register_artifact_verifier("render", verifier)
    job_id = await harness.queue.enqueue(job_type="render", idempotency_key="r-2", payload=PAYLOAD)
    assert await harness.drain(job_id) is JobStatus.COMPLETED

    final = await harness.queue.get_result(job_id)
    assert final == {
        "success": True,
        "artifact_path": "/somewhere",
        VERIFICATION_RESULT_KEY: outcome.block,
    }
    kinds = harness.ledger.kinds(job_id)
    assert kinds.index(EventKind.JOB_VERIFICATION_STARTED) < kinds.index(EventKind.JOB_COMPLETED)
    started = harness.ledger.last(job_id, EventKind.JOB_VERIFICATION_STARTED)
    assert started.status == JobStatus.VERIFYING.value and started.attempt == 1
    assert [(c.status, c.result, c.error) for c in harness.completions] == [
        (JobStatus.COMPLETED, final, None)
    ]
