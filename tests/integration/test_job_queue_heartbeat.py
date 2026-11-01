"""Task-256: queue-owned heartbeat liveness, upgrade and fenced recovery proofs.

Every test uses a real InProcessJobQueue over a temporary SQLite sidecar. Raw
SQL appears only in isolated legacy/attack fixtures, never as an operational
recovery path. Time is controlled through the queue's single UTC clock seam;
worker progress is event-gated rather than proven by long sleeps.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters import in_process_job_queue as queue_module
from nexus_ai_agent.adapters.in_process_job_queue import (
    ArtifactPublication,
    InProcessJobQueue,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.lifecycle import ExecutionClaim
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.provenance.models import CausalEvent, EventKind

pytestmark = pytest.mark.integration

JOB_TYPE = "heartbeat-proof"
OLD_STARTED = "2000-01-01T00:00:00+00:00"
STALE_WINDOW = timedelta(hours=1)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)

    def iso(self) -> str:
        return self.now.isoformat()


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    controlled = _Clock()
    monkeypatch.setattr(queue_module, "_utc_now", lambda: controlled.now)
    return controlled


class _Gate:
    """Handler/verifier park per call so attempts are independently released."""

    def __init__(self) -> None:
        self.calls = 0
        self.events: dict[int, asyncio.Event] = {}
        self.verifier_calls = 0
        self.thread_events: dict[int, threading.Event] = {}

    def event(self, call: int) -> asyncio.Event:
        return self.events.setdefault(call, asyncio.Event())

    async def handler(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        call = self.calls
        await self.event(call).wait()
        return {"who": f"worker-{call}", "artifact_path": str(payload.get("artifact_path", ""))}

    def verifier(
        self, payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        # The verifier is called from a worker thread; block on a threading
        # event mirrored by the asyncio gate for deterministic release.
        call = self.verifier_calls
        self.verifier_calls += 1
        self.thread_events.setdefault(call, threading.Event()).wait(timeout=5)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "ok"})


class _Recorder:
    def __init__(self) -> None:
        self.events: list[CausalEvent] = []

    def observe(self, event: CausalEvent) -> None:
        self.events.append(event)

    def kinds(self, job_id: str) -> list[EventKind]:
        return [event.kind for event in self.events if event.job_id == job_id]


def _row(db: Path, job_id: str) -> dict[str, Any]:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM nexus_job_queue WHERE id = ?", (job_id,)).fetchone()
    assert row is not None
    return dict(row)


def _history_entry(job_id: str, attempt: int, status: str = "processing") -> dict[str, object]:
    return {
        "attempt": attempt,
        "attempt_id": queue_module.creative_attempt_id(job_id, attempt),
        "status": status,
        "started_at": OLD_STARTED,
        "verifying_at": None,
        "verified_at": None,
        "finished_at": None,
        "error": None,
        "execution_result_json": None,
        "verification_json": None,
        "execution_evidence_json": None,
        "passport_json": None,
        "reconciled_from_attempt_id": None,
    }


def _seed_inflight(
    db: Path,
    *,
    key: str,
    status: JobStatus = JobStatus.PROCESSING,
    heartbeat: object = None,
    attempt: object = 1,
    result: dict[str, object] | None = None,
) -> str:
    """Create a real identity, then place an isolated row in a test state."""
    queue = InProcessJobQueue(db, artifact_verifiers={})
    payload = {"value": key}
    outcome = queue._insert_or_get(JOB_TYPE, key, payload)
    job_id = outcome.job_id
    history = (
        [_history_entry(job_id, int(attempt), status.value)]
        if isinstance(attempt, int) and attempt > 0
        else []
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            UPDATE nexus_job_queue
            SET status = ?, started_at = ?, heartbeat_at = ?, attempt = ?,
                attempt_history_json = ?, result_json = ?
            WHERE id = ?
            """,
            (
                status.value,
                OLD_STARTED,
                heartbeat,
                attempt,
                json.dumps(history),
                json.dumps(result) if result is not None else None,
                job_id,
            ),
        )
    return job_id


async def _wait_status(queue: InProcessJobQueue, job_id: str, wanted: JobStatus) -> None:
    async def poll() -> None:
        while await queue.get_status(job_id) != wanted:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


async def _wait_event(recorder: _Recorder, job_id: str, kind: EventKind) -> None:
    """Wait until the causal observer has recorded ``kind`` for ``job_id``.

    ``_record`` runs the observer in a worker thread, so a row can already be
    PROCESSING while its JOB_RESERVED event is still being recorded.  Waiting
    for the event keeps causal-order assertions deterministic under load
    instead of racing the observer thread.
    """

    async def poll() -> None:
        while kind not in recorder.kinds(job_id):
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


async def _wait_heartbeat(db: Path, job_id: str, expected: str) -> None:
    async def poll() -> None:
        while _row(db, job_id)["heartbeat_at"] != expected:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


def _legacy_schema(db: Path) -> None:
    """Pre-heartbeat schema, including the already-shipped attempt ledger."""
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE nexus_job_queue (
                id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,
                request_fingerprint TEXT,
                transaction_id TEXT,
                artifact_passport_json TEXT,
                attempt_history_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )


def _insert_legacy(db: Path, job_id: str, status: str, *, attempt: int = 1) -> None:
    payload = {"value": job_id}
    identity = queue_module.build_request_identity(JOB_TYPE, f"key-{job_id}", payload)
    history = [_history_entry(job_id, attempt, status)] if attempt else []
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, created_at,
                 started_at, attempt, request_id, request_fingerprint, transaction_id,
                 attempt_history_json, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                JOB_TYPE,
                f"key-{job_id}",
                json.dumps(payload),
                status,
                OLD_STARTED,
                OLD_STARTED if attempt else None,
                attempt,
                identity.request_id,
                identity.request_fingerprint,
                identity.transaction_id,
                json.dumps(history),
                json.dumps({"previous": job_id}) if status == "completed" else None,
            ),
        )


@pytest.mark.parametrize("status", [JobStatus.PROCESSING, JobStatus.VERIFYING])
@pytest.mark.parametrize(
    "offset", [timedelta(minutes=-30), timedelta(minutes=-60), timedelta(minutes=-61)]
)
async def test_normal_recovery_strict_heartbeat_boundary(
    tmp_path: Path, clock: _Clock, status: JobStatus, offset: timedelta
) -> None:
    db = tmp_path / "jobs.sqlite3"
    heartbeat = (clock.now + offset).isoformat()
    job_id = _seed_inflight(
        db, key=f"boundary-{status}-{offset}", status=status, heartbeat=heartbeat
    )
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler(JOB_TYPE, gate.handler)
    before = _row(db, job_id)

    recovered = await queue.recover_job(job_id, stale_after=STALE_WINDOW)
    if offset >= -STALE_WINDOW:
        assert recovered == []
        assert _row(db, job_id) == before
        assert gate.calls == 0
    else:
        assert recovered == [job_id]
        await _wait_status(queue, job_id, JobStatus.PROCESSING)
        row = _row(db, job_id)
        assert row["attempt"] == 2
        assert row["heartbeat_at"] == clock.iso()
        assert gate.calls == 1
        gate.event(1).set()
    await queue.shutdown()


@pytest.mark.parametrize("status", [JobStatus.PROCESSING, JobStatus.VERIFYING])
@pytest.mark.parametrize(
    "heartbeat",
    [
        None,
        "",
        "not-a-timestamp",
        "2026-10-29T11:00:00",
        "2026-10-29T11:00:00+03:00",
        "2026-10-29 11:00:00+00:00",
        "2026-10-29T13:00:00+00:00",
        1730000000,
        b"2026-10-29T11:00:00+00:00",
    ],
)
async def test_unknown_heartbeat_never_mutates_or_recovers(
    tmp_path: Path, clock: _Clock, status: JobStatus, heartbeat: object
) -> None:
    db = tmp_path / "jobs.sqlite3"
    job_id = _seed_inflight(
        db,
        key=f"unknown-{status}-{heartbeat!r}",
        status=status,
        heartbeat=heartbeat,
        result={"previous": "evidence"},
    )
    gate = _Gate()
    recorder = _Recorder()
    queue = InProcessJobQueue(db, artifact_verifiers={}, causal_observer=recorder)
    queue.register_handler(JOB_TYPE, gate.handler)
    before = _row(db, job_id)

    assert await queue.recover_job(job_id, stale_after=STALE_WINDOW) == []
    assert await queue.resume_pending(stale_after=STALE_WINDOW) == []
    assert await queue.resume_pending() == []
    assert await queue.resume_pending_jobs() == []
    assert _row(db, job_id) == before
    assert gate.calls == 0
    assert recorder.events == []
    await queue.shutdown()


async def test_legacy_schema_upgrade_adds_null_heartbeat_without_inventing_evidence(
    tmp_path: Path, clock: _Clock
) -> None:
    db = tmp_path / "jobs.sqlite3"
    _legacy_schema(db)
    rows = {
        "processing": "processing",
        "verifying": "verifying",
        "completed": "completed",
        "failed": "failed_terminal",
        "no-attempt": "processing",
    }
    for job_id, status in rows.items():
        _insert_legacy(db, job_id, status, attempt=0 if job_id == "no-attempt" else 1)
    before = {job_id: _row(db, job_id) for job_id in rows}

    queue = InProcessJobQueue(db, artifact_verifiers={})
    after = {job_id: _row(db, job_id) for job_id in rows}
    for job_id in rows:
        assert after[job_id]["heartbeat_at"] is None
        assert {
            k: v for k, v in after[job_id].items() if k not in ("heartbeat_at", "unknown_outcome")
        } == before[job_id]

    gate = _Gate()
    queue.register_handler(JOB_TYPE, gate.handler)
    for job_id in ("processing", "verifying", "no-attempt"):
        assert await queue.recover_job(job_id, stale_after=STALE_WINDOW) == []
    assert await queue.resume_pending(stale_after=STALE_WINDOW) == []
    assert await queue.resume_pending() == []
    assert gate.calls == 0
    assert {job_id: _row(db, job_id) for job_id in rows} == after

    # Repeated initialization is additive and idempotent.
    queue._initialize_db()
    queue._initialize_db()
    assert {job_id: _row(db, job_id) for job_id in rows} == after
    columns = [row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(nexus_job_queue)")]
    assert columns.count("heartbeat_at") == 1
    await queue.shutdown()


async def test_live_slow_worker_refreshes_and_cannot_be_taken_over(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler(JOB_TYPE, gate.handler)
    tick = asyncio.Event()

    async def gated_wait() -> None:
        await tick.wait()
        tick.clear()

    monkeypatch.setattr(queue, "_wait_for_heartbeat", gated_wait)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="slow-alive", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    initial = _row(db, job_id)
    assert initial["heartbeat_at"] == initial["started_at"] == clock.iso()

    # The operation is now two hours old, but the owner is still alive and
    # emits a real queue-owned refresh at the current controlled time.
    clock.advance(hours=2)
    tick.set()
    await _wait_heartbeat(db, job_id, clock.iso())
    row = _row(db, job_id)
    assert row["started_at"] == initial["started_at"]
    assert row["heartbeat_at"] == clock.iso()

    peer = InProcessJobQueue(db, artifact_verifiers={})
    assert await peer.recover_job(job_id, stale_after=STALE_WINDOW) == []
    assert await peer.resume_pending(stale_after=STALE_WINDOW) == []
    assert _row(db, job_id) == row
    assert gate.calls == 1
    gate.event(1).set()
    await _wait_status(queue, job_id, JobStatus.COMPLETED)
    await queue.shutdown()
    await peer.shutdown()
    assert queue._heartbeat_tasks == {}


async def test_heartbeat_cadence_processing_verifying_and_cleanup(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert queue_module.HEARTBEAT_INTERVAL_SECONDS == 5.0
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    verify_entered = asyncio.Event()
    verify_release = threading.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        gate.calls += 1
        await gate.event(gate.calls).wait()
        return {"artifact_path": "staged"}

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        verify_entered.set()
        assert verify_release.wait(timeout=5)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "ok"})

    queue = InProcessJobQueue(db, artifact_verifiers={JOB_TYPE: verifier})
    queue.register_handler(JOB_TYPE, handler)
    tick = asyncio.Event()

    async def gated_wait() -> None:
        await tick.wait()
        tick.clear()
        clock.advance(seconds=queue_module.HEARTBEAT_INTERVAL_SECONDS)

    monkeypatch.setattr(queue, "_wait_for_heartbeat", gated_wait)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="cadence", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    for _ in range(2):
        expected = (clock.now + timedelta(seconds=5)).isoformat()
        tick.set()
        await _wait_heartbeat(db, job_id, expected)
        assert clock.iso() == expected
        assert _row(db, job_id)["attempt"] == 1

    gate.event(1).set()
    await asyncio.wait_for(verify_entered.wait(), timeout=3)
    await _wait_status(queue, job_id, JobStatus.VERIFYING)
    verifying_beat = _row(db, job_id)["heartbeat_at"]
    assert verifying_beat == clock.iso()
    tick.set()
    await _wait_heartbeat(db, job_id, (clock.now + timedelta(seconds=5)).isoformat())
    assert _row(db, job_id)["status"] == JobStatus.VERIFYING.value
    verify_release.set()
    await _wait_status(queue, job_id, JobStatus.COMPLETED)
    await queue.shutdown()
    assert queue._heartbeat_tasks == {}


async def test_recovery_cas_binds_observed_heartbeat_and_loses_to_refresh(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    stale_beat = (clock.now - timedelta(hours=2)).isoformat()
    job_id = _seed_inflight(db, key="cas-refresh", heartbeat=stale_beat)
    owner = InProcessJobQueue(db, artifact_verifiers={})
    recoverer = InProcessJobQueue(db, artifact_verifiers={})
    read_done = threading.Event()
    release_read = threading.Event()
    original_decode = recoverer._decode_attempt_history

    def paused_decode(raw: str) -> list[dict[str, object]]:
        read_done.set()
        assert release_read.wait(timeout=5)
        return original_decode(raw)

    monkeypatch.setattr(recoverer, "_decode_attempt_history", paused_decode)
    recovery = asyncio.create_task(recoverer.recover_job(job_id, stale_after=STALE_WINDOW))
    await asyncio.to_thread(read_done.wait, 3)
    clock.advance(hours=2)
    assert await asyncio.to_thread(owner._refresh_heartbeat, ExecutionClaim(job_id, 1)) is True
    refreshed = _row(db, job_id)["heartbeat_at"]
    assert refreshed == clock.iso()
    release_read.set()
    assert await recovery == []
    assert _row(db, job_id)["heartbeat_at"] == refreshed
    assert _row(db, job_id)["attempt"] == 1
    await owner.shutdown()
    await recoverer.shutdown()


async def test_stale_worker_heartbeat_finish_failure_and_cancel_are_fenced(
    tmp_path: Path, clock: _Clock
) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a = InProcessJobQueue(db, artifact_verifiers={})
    queue_b = InProcessJobQueue(db, artifact_verifiers={})
    queue_a.register_handler(JOB_TYPE, gate.handler)
    queue_b.register_handler(JOB_TYPE, gate.handler)
    job_id = await queue_a.enqueue(job_type=JOB_TYPE, idempotency_key="stale-worker", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    clock.advance(hours=2)
    assert await queue_b.recover_job(job_id, stale_after=STALE_WINDOW) == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    current = _row(db, job_id)
    assert current["attempt"] == 2

    stale = ExecutionClaim(job_id, 1)
    assert queue_a._refresh_heartbeat(stale) is False
    assert queue_a._mark_completed(stale, {"stale": True}) is False
    assert queue_a._mark_failed(stale, "stale") is False
    assert await queue_a.cancel(job_id, expected_attempt=1) is False
    assert await queue_a.cancel(job_id, expected_attempt=1) is False
    assert _row(db, job_id) == current

    gate.event(1).set()
    gate.event(2).set()
    await _wait_status(queue_b, job_id, JobStatus.COMPLETED)
    assert (await queue_b.get_result(job_id) or {})["who"] == "worker-2"
    await queue_a.shutdown()
    await queue_b.shutdown()


@pytest.mark.parametrize("attempt", [0, -1])
async def test_invalid_persisted_attempt_cannot_cancel(tmp_path: Path, attempt: int) -> None:
    db = tmp_path / "jobs.sqlite3"
    job_id = _seed_inflight(db, key=f"invalid-{attempt}", attempt=attempt)
    queue = InProcessJobQueue(db, artifact_verifiers={})
    before = _row(db, job_id)
    assert await queue.cancel(job_id, expected_attempt=1) is False
    assert _row(db, job_id) == before
    await queue.shutdown()


@pytest.mark.parametrize("expected", [0, -1, True])
async def test_invalid_expected_attempt_cannot_cancel(tmp_path: Path, expected: int) -> None:
    db = tmp_path / "jobs.sqlite3"
    job_id = _seed_inflight(db, key=f"expected-{expected}")
    queue = InProcessJobQueue(db, artifact_verifiers={})
    before = _row(db, job_id)
    assert await queue.cancel(job_id, expected_attempt=expected) is False
    assert await queue.cancel("missing-job", expected_attempt=1) is False
    assert _row(db, job_id) == before
    await queue.shutdown()


async def test_manual_unknown_recovery_cancel_reconcile_resume_and_repeated_cancel(
    tmp_path: Path, clock: _Clock
) -> None:
    db = tmp_path / "jobs.sqlite3"
    job_id = _seed_inflight(db, key="manual", heartbeat=None, result={"previous": "output"})
    gate = _Gate()
    recorder = _Recorder()
    queue = InProcessJobQueue(db, artifact_verifiers={}, causal_observer=recorder)
    queue.register_handler(JOB_TYPE, gate.handler)
    identity = await queue.get_request_identity(job_id)
    before = _row(db, job_id)

    # Maintenance ownership is not encoded in the queue. The operator contract
    # is modeled here: while ownership/worker-stop proof is absent, no cancel
    # or resume is issued and the unknown row remains untouched.
    maintenance_proven = False
    original_worker_stopped = True  # seeded row has no local owner task
    assert original_worker_stopped
    if maintenance_proven:
        pytest.fail("uncertain ownership must not call recovery")
    assert await queue.recover_job(job_id, stale_after=STALE_WINDOW) == []
    assert _row(db, job_id) == before

    # Independent proof in this isolated test: no old task exists, this is the
    # only queue instance, and the exact current attempt was read from the row.
    maintenance_proven = True
    assert maintenance_proven and original_worker_stopped
    current_attempt = int(before["attempt"])
    assert await queue.cancel(job_id, expected_attempt=current_attempt) is True
    cancelled = _row(db, job_id)
    assert cancelled["status"] == JobStatus.PENDING.value
    assert cancelled["heartbeat_at"] is None
    assert cancelled["attempt"] == current_attempt
    history = json.loads(cancelled["attempt_history_json"])
    assert history[0]["status"] == "interrupted"
    assert EventKind.JOB_REOPENED in recorder.kinds(job_id)
    assert await queue.get_request_identity(job_id) == identity

    # Unresolved irreversible external effects: cancellation does not schedule
    # a retry, and the operator does not call resume until reconciliation.
    await asyncio.sleep(0)
    assert gate.calls == 0
    assert await queue.cancel(job_id, expected_attempt=current_attempt) is False

    effects_reconciled = True
    assert effects_reconciled
    assert await queue.resume_pending_jobs() == [job_id]
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    successor = _row(db, job_id)
    assert successor["attempt"] == current_attempt + 1
    assert successor["heartbeat_at"] == clock.iso()
    assert await queue.get_request_identity(job_id) == identity
    assert await queue.cancel(job_id, expected_attempt=current_attempt) is False
    assert queue._mark_completed(ExecutionClaim(job_id, current_attempt), {"late": True}) is False
    assert _row(db, job_id) == successor
    gate.event(1).set()
    await _wait_status(queue, job_id, JobStatus.COMPLETED)
    await queue.shutdown()


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL]
)
async def test_terminal_rows_never_reopen_or_change_attempt(
    tmp_path: Path, clock: _Clock, status: JobStatus
) -> None:
    db = tmp_path / "jobs.sqlite3"
    job_id = _seed_inflight(
        db,
        key=f"terminal-{status}",
        status=status,
        heartbeat=(clock.now - timedelta(days=1)).isoformat(),
        result={"terminal": True},
    )
    queue = InProcessJobQueue(db, artifact_verifiers={})
    before = _row(db, job_id)
    assert await queue.recover_job(job_id, stale_after=STALE_WINDOW) == []
    assert await queue.resume_pending() == []
    assert await queue.resume_pending_jobs() == []
    assert await queue.cancel(job_id, expected_attempt=1) is False
    assert queue._refresh_heartbeat(ExecutionClaim(job_id, 1)) is False
    assert _row(db, job_id) == before
    await queue.shutdown()


async def test_stale_worker_cannot_publish_after_takeover(tmp_path: Path, clock: _Clock) -> None:
    db = tmp_path / "jobs.sqlite3"
    handler_gate = _Gate()
    verify_entered = asyncio.Event()
    verify_release = threading.Event()
    published: list[str] = []

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        handler_gate.calls += 1
        call = handler_gate.calls
        await handler_gate.event(call).wait()
        return {"artifact_path": str(tmp_path / f"attempt-{call}.txt")}

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        verify_entered.set()
        assert verify_release.wait(timeout=5)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "ok"})

    def publish(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        published.append(str(result["artifact_path"]))
        return {}

    publication = ArtifactPublication(publish=publish, retract=lambda payload, result: None)
    queue_a = InProcessJobQueue(
        db, artifact_verifiers={JOB_TYPE: verifier}, artifact_publications={JOB_TYPE: publication}
    )
    queue_b = InProcessJobQueue(db, artifact_verifiers={}, artifact_publications={})
    queue_a.register_handler(JOB_TYPE, handler)
    queue_b.register_handler(JOB_TYPE, handler)
    job_id = await queue_a.enqueue(job_type=JOB_TYPE, idempotency_key="publish-fence", payload={})
    handler_gate.event(1).set()
    await asyncio.wait_for(verify_entered.wait(), timeout=3)
    await _wait_status(queue_a, job_id, JobStatus.VERIFYING)
    clock.advance(hours=2)
    assert await queue_b.recover_job(job_id, stale_after=STALE_WINDOW) == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    verify_release.set()
    await asyncio.sleep(0.02)
    assert published == []
    assert _row(db, job_id)["attempt"] == 2
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value
    handler_gate.event(2).set()
    await _wait_status(queue_b, job_id, JobStatus.COMPLETED)
    await queue_a.shutdown()
    await queue_b.shutdown()


async def test_heartbeat_write_failure_never_fabricates_evidence(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler(JOB_TYPE, gate.handler)
    tick = asyncio.Event()

    async def gated_wait() -> None:
        await tick.wait()
        tick.clear()

    monkeypatch.setattr(queue, "_wait_for_heartbeat", gated_wait)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="db-failure", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    before = _row(db, job_id)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_heartbeat BEFORE UPDATE OF heartbeat_at ON nexus_job_queue
            BEGIN SELECT RAISE(ABORT, 'heartbeat storage unavailable'); END
            """
        )
    clock.advance(seconds=5)
    tick.set()
    owner = queue._tasks[job_id]
    heartbeat = queue._heartbeat_tasks[owner]
    await asyncio.wait_for(asyncio.gather(heartbeat, return_exceptions=True), timeout=3)
    assert _row(db, job_id) == before
    assert "job_heartbeat_refresh_failed" in caplog.text
    gate.event(1).set()
    await _wait_status(queue, job_id, JobStatus.COMPLETED)
    await queue.shutdown()
    assert queue._heartbeat_tasks == {}


async def test_shutdown_and_cancel_stop_heartbeat_children(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler(JOB_TYPE, gate.handler)
    tick = asyncio.Event()

    async def gated_wait() -> None:
        await tick.wait()

    monkeypatch.setattr(queue, "_wait_for_heartbeat", gated_wait)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="cleanup", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    owner = queue._tasks[job_id]
    heartbeat = queue._heartbeat_tasks[owner]
    assert await queue.cancel(job_id, expected_attempt=1) is True
    await asyncio.wait_for(asyncio.gather(owner, heartbeat, return_exceptions=True), timeout=3)
    assert heartbeat.done()
    assert queue._heartbeat_tasks == {}
    assert _row(db, job_id)["heartbeat_at"] is None

    job_id2 = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="shutdown", payload={})
    await _wait_status(queue, job_id2, JobStatus.PROCESSING)
    await queue.shutdown()
    assert queue._heartbeat_tasks == {}
    assert all(task.done() for task in queue._tasks.values())


async def test_takeover_and_reopen_causal_order(tmp_path: Path, clock: _Clock) -> None:
    db = tmp_path / "jobs.sqlite3"
    stale_beat = (clock.now - timedelta(hours=2)).isoformat()
    job_id = _seed_inflight(db, key="causal", heartbeat=stale_beat)
    recorder = _Recorder()
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={}, causal_observer=recorder)
    queue.register_handler(JOB_TYPE, gate.handler)
    assert await queue.recover_job(job_id, stale_after=STALE_WINDOW) == [job_id]
    assert recorder.kinds(job_id)[0] is EventKind.JOB_TAKEOVER
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    await _wait_event(recorder, job_id, EventKind.JOB_RESERVED)
    assert EventKind.JOB_RESERVED in recorder.kinds(job_id)
    assert recorder.kinds(job_id).index(EventKind.JOB_TAKEOVER) < recorder.kinds(job_id).index(
        EventKind.JOB_RESERVED
    )
    assert await queue.cancel(job_id, expected_attempt=2) is True
    assert EventKind.JOB_REOPENED in recorder.kinds(job_id)
    await queue.shutdown()
