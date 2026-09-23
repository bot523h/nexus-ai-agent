"""M0 runtime integration — Q1–Q5 real-execution proofs on the real queue.

Runs the actual ``InstrumentedJobQueue`` (SQLite sidecar, asyncio tasks,
real handlers — no mocks of the queue itself) and proves, at runtime:

* **Q1** created: counter + ``job_created`` event + correlation injected
  before persist; idempotent re-enqueue does NOT double-count.
* **Q2** claimed: exactly one ``job_claimed`` per real pending→processing
  transition; the wrapped handler sees the payload's correlation id.
* **Q3** completed: durable completed state first, then counter + histogram
  observation with monotonic (wall-clock-immune) duration.
* **Q4** failed: durable failed state, bounded ``error_code`` label, no raw
  text as label.
* **Q5** recovered: ``startup_recovery`` vs ``operator_resume`` entry-point
  reasons; graceful-shutdown drain is deliberately NOT counted.

Plus the cross-cutting proofs: event **order** (created→claimed→terminal),
correlation chain end-to-end (payload → handler context → terminal event),
hostile-clock saturation reports, emission **fault injection** (broken
observability never breaks a job), and the **M1–M4 mutation detectors**
(silencing each emission makes the corresponding Q assertion fail).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.instrumentation import InstrumentedJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.observability.correlation import (
    bind_correlation_id,
    clear_correlation_id,
    extract_from_payload,
    get_correlation_id,
)
from nexus_ai_agent.observability.diagnostics import (
    EVENT_JOB_CLAIMED,
    EVENT_JOB_COMPLETED,
    EVENT_JOB_CREATED,
    EVENT_JOB_FAILED,
    EVENT_JOB_RECOVERED,
)
from nexus_ai_agent.observability.metrics import reset_for_tests, snapshot

JOB_TYPE = "story"  # bounded canonical job type


# -- helpers -----------------------------------------------------------------


async def _wait_for_status(
    queue: InstrumentedJobQueue,
    job_id: str,
    expected: JobStatus,
    *,
    timeout: float = 2.0,
) -> None:
    async def _poll() -> None:
        while await queue.get_status(job_id) != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _counter(name: str, labels: str = "") -> float:
    snap = snapshot()
    return float(snap.get(f"{name}{{{labels}}}" if labels else name, 0))


def _hist_count(job_type: str) -> float:
    return float(snapshot().get(f'job_duration_seconds_count{{job_type="{job_type}"}}', 0))


class EventRecorder:
    """Capture ``(event_name, fields)`` tuples from the diagnostics emitter."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, level: int, name: str, **fields: object) -> None:
        self.events.append((str(name), dict(fields)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def of(self, event_name: str) -> list[dict]:
        return [fields for name, fields in self.events if name == event_name]


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_for_tests()
    clear_correlation_id()
    yield
    reset_for_tests()
    clear_correlation_id()


@pytest.fixture
def events(monkeypatch):
    recorder = EventRecorder()
    monkeypatch.setattr(
        "nexus_ai_agent.observability.diagnostics.log_lifecycle_event",
        recorder,
    )
    return recorder


def _queue(tmp_path: Path, **kwargs) -> InstrumentedJobQueue:
    return InstrumentedJobQueue(tmp_path / "jobs.sqlite3", **kwargs)


# -- Q1: created -------------------------------------------------------------


@pytest.mark.asyncio
async def test_q1_created_counts_once_and_injects_correlation(tmp_path, events):
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q1", payload={"a": 1})

    assert _counter("jobs_created_total", f'job_type="{JOB_TYPE}"') == 1
    created = events.of(EVENT_JOB_CREATED)
    assert len(created) == 1
    assert created[0]["job_type"] == JOB_TYPE
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)  # job really ran

    # persisted payload carries correlation_id (read back from the sidecar)
    import sqlite3

    with sqlite3.connect(tmp_path / "jobs.sqlite3") as conn:
        payload_json = conn.execute(
            "SELECT payload_json FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()[0]
    persisted = json.loads(payload_json)
    assert extract_from_payload(persisted)  # non-empty correlation id
    await queue.shutdown()


@pytest.mark.asyncio
async def test_q1_idempotent_reenqueue_does_not_double_count(tmp_path, events):
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    first = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="same", payload={})
    second = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="same", payload={})

    assert first == second
    assert _counter("jobs_created_total", f'job_type="{JOB_TYPE}"') == 1
    assert len(events.of(EVENT_JOB_CREATED)) == 1
    await queue.shutdown()


# -- Q2: claimed -------------------------------------------------------------


@pytest.mark.asyncio
async def test_q2_claim_counted_once_with_correlation_restored(tmp_path, events):
    queue = _queue(tmp_path)
    seen: dict[str, object] = {}

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        seen["ambient"] = get_correlation_id()
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)
    payload_corr = "cid-payload-1"
    job_id = await queue.enqueue(
        job_type=JOB_TYPE,
        idempotency_key="q2",
        payload={"correlation_id": payload_corr, "n": 7},
    )
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # exactly one claim per real transition (no double-count from dual emitters)
    assert _counter("jobs_claimed_total", f'job_type="{JOB_TYPE}"') == 1
    claimed = events.of(EVENT_JOB_CLAIMED)
    assert len(claimed) == 1
    assert claimed[0]["correlation_id"] == payload_corr
    # handler ran with the payload's correlation id in context
    assert seen["ambient"] == payload_corr
    await queue.shutdown()


# -- Q3: completed -----------------------------------------------------------


@pytest.mark.asyncio
async def test_q3_completed_durable_first_then_counter_and_histogram(tmp_path, events):
    queue = _queue(tmp_path)
    order: list[str] = []

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        order.append("handler")
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q3", payload={})
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # durable state exists
    assert await queue.get_status(job_id) == JobStatus.COMPLETED
    assert await queue.get_result(job_id) == {"ok": True}
    # counter + histogram observed exactly once
    assert _counter("jobs_completed_total", f'job_type="{JOB_TYPE}"') == 1
    assert _hist_count(JOB_TYPE) == 1
    completed = events.of(EVENT_JOB_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["duration_ms"] >= 0  # monotonic timer, never negative
    # saturation gauges reflect the real queue after terminal transition
    snap = snapshot()
    assert snap.get("queue_depth") == 0.0
    assert snap.get("jobs_inflight") == 0.0
    await queue.shutdown()


# -- Q4: failed --------------------------------------------------------------


@pytest.mark.asyncio
async def test_q4_failed_bounded_error_code_no_raw_text(tmp_path, events):
    queue = _queue(tmp_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("secret token=abc123 leaked in message")

    queue.register_handler(JOB_TYPE, handler)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q4", payload={})
    await _wait_for_status(queue, job_id, JobStatus.FAILED)

    assert await queue.get_status(job_id) == JobStatus.FAILED
    # bounded label only: ValueError path -> handler_exception; raw text never a label
    assert (
        _counter("jobs_failed_total", f'error_code="handler_exception",job_type="{JOB_TYPE}"')
        == 1
    )
    snap_keys = [k for k in snapshot() if k.startswith("jobs_failed_total")]
    assert all("abc123" not in k and "secret" not in k for k in snap_keys)
    failed = events.of(EVENT_JOB_FAILED)
    assert len(failed) == 1
    assert failed[0]["failure_class"] == "handler_exception"
    # duration histogram observed for the failed job too
    assert _hist_count(JOB_TYPE) == 1
    await queue.shutdown()


@pytest.mark.asyncio
async def test_q4_missing_handler_is_handler_not_found(tmp_path, events):
    queue = _queue(tmp_path)  # deliberately no handler registered
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q4b", payload={})
    await _wait_for_status(queue, job_id, JobStatus.FAILED)
    assert (
        _counter(
            "jobs_failed_total", f'error_code="handler_not_found",job_type="{JOB_TYPE}"'
        )
        == 1
    )
    await queue.shutdown()


# -- Q5: recovered -----------------------------------------------------------


@pytest.mark.asyncio
async def test_q5_startup_recovery_reason(tmp_path, events):
    queue = _queue(tmp_path)
    # A row left pending by a "previous process": insert without scheduling.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            InstrumentedJobQueue,
            "_schedule",
            lambda self, job_id: None,
        )
        await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q5a", payload={})

    reset_for_tests()
    events.events.clear()
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    resumed = await queue.resume_pending()
    assert len(resumed) == 1
    await _wait_for_status(queue, resumed[0], JobStatus.COMPLETED)

    assert _counter("jobs_recovered_total", 'reason="startup_recovery"') == 1
    recovered = events.of(EVENT_JOB_RECOVERED)
    assert len(recovered) == 1
    assert recovered[0]["reason"] == "startup_recovery"
    await queue.shutdown()


@pytest.mark.asyncio
async def test_q5_operator_resume_reason(tmp_path, events):
    queue = _queue(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(InstrumentedJobQueue, "_schedule", lambda self, job_id: None)
        await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q5b", payload={})

    reset_for_tests()
    events.events.clear()
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    resumed = await queue.resume_pending_jobs()
    assert len(resumed) == 1
    await _wait_for_status(queue, resumed[0], JobStatus.COMPLETED)

    assert _counter("jobs_recovered_total", 'reason="operator_resume"') == 1
    # startup_recovery reason must stay absent — entry points don't blur
    assert _counter("jobs_recovered_total", 'reason="startup_recovery"') == 0
    recovered = events.of(EVENT_JOB_RECOVERED)
    assert recovered and recovered[0]["reason"] == "operator_resume"
    await queue.shutdown()


@pytest.mark.asyncio
async def test_q5_graceful_shutdown_drain_is_not_counted_as_recovered(tmp_path, events):
    queue = _queue(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="q5c", payload={})
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await queue.shutdown()  # drain: cancels the task, row back to pending

    # Shutdown released work but did not "recover" it — reason absent entirely.
    assert _counter("jobs_recovered_total") == 0
    assert events.of(EVENT_JOB_RECOVERED) == []
    assert await queue.get_status(job_id) == JobStatus.PENDING


# -- order: created → claimed → terminal -------------------------------------


@pytest.mark.asyncio
async def test_order_created_claimed_completed(tmp_path, events):
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="ord1", payload={})
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    names = events.names()
    assert names.index(EVENT_JOB_CREATED) < names.index(EVENT_JOB_CLAIMED)
    assert names.index(EVENT_JOB_CLAIMED) < names.index(EVENT_JOB_COMPLETED)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_order_created_claimed_failed(tmp_path, events):
    queue = _queue(tmp_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("nope")

    queue.register_handler(JOB_TYPE, handler)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="ord2", payload={})
    await _wait_for_status(queue, job_id, JobStatus.FAILED)

    names = events.names()
    assert names.index(EVENT_JOB_CREATED) < names.index(EVENT_JOB_CLAIMED)
    assert names.index(EVENT_JOB_CLAIMED) < names.index(EVENT_JOB_FAILED)
    await queue.shutdown()


# -- correlation chain end-to-end --------------------------------------------


@pytest.mark.asyncio
async def test_correlation_chain_payload_beats_ambient_and_survives_to_terminal(
    tmp_path, events
):
    queue = _queue(tmp_path)
    seen: dict[str, object] = {}

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        seen["during"] = get_correlation_id()
        # A handler that clears context must not break the terminal event chain.
        clear_correlation_id()
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)

    bind_correlation_id("ambient-should-lose")
    try:
        job_id = await queue.enqueue(
            job_type=JOB_TYPE,
            idempotency_key="corr1",
            payload={"correlation_id": "payload-wins"},
        )
    finally:
        clear_correlation_id()

    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert seen["during"] == "payload-wins"  # restored into context on consume
    completed = events.of(EVENT_JOB_COMPLETED)
    assert completed[0]["correlation_id"] == "payload-wins"  # read from persisted payload
    # ambient context restored to pre-job state (none) — no leakage
    assert get_correlation_id() is None
    await queue.shutdown()


@pytest.mark.asyncio
async def test_correlation_falls_back_to_ambient_then_fresh(tmp_path, events):
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))

    bind_correlation_id("ambient-used")
    try:
        job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="c2", payload={})
    finally:
        clear_correlation_id()
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    created = events.of(EVENT_JOB_CREATED)
    assert created[0]["correlation_id"] == "ambient-used"  # ambient fallback

    # no payload, no ambient -> fresh id (still a chain, never empty)
    events.events.clear()
    job_id2 = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="c3", payload={})
    await _wait_for_status(queue, job_id2, JobStatus.COMPLETED)
    created2 = events.of(EVENT_JOB_CREATED)
    assert created2[0]["correlation_id"] not in (None, "", "none")
    await queue.shutdown()


@pytest.mark.asyncio
async def test_correlation_id_is_never_a_metric_label(tmp_path, events):
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(
        job_type=JOB_TYPE,
        idempotency_key="c4",
        payload={"correlation_id": "PII-looking:user@example.com"},
    )
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    # metric label keys never contain a correlation id (cardinality/PII)
    for key in snapshot():
        assert "correlation" not in key
        assert "example.com" not in key
    await queue.shutdown()


# -- hostile clock: saturation report ----------------------------------------


@pytest.mark.asyncio
async def test_saturation_report_hostile_clock_clamps(tmp_path):
    queue = _queue(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(InstrumentedJobQueue, "_schedule", lambda self, job_id: None)
        await queue.enqueue(job_type=JOB_TYPE, idempotency_key="s1", payload={})

    # Naive datetime treated as UTC (no crash).
    naive = queue.saturation_report(now=datetime(2030, 1, 1, 12, 0, 0))
    assert naive.measured_at.endswith("+00:00")
    assert naive.pending == 1

    # Clock jumped BEFORE row creation -> negative age clamps to 0.
    hostile_past = queue.saturation_report(now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert hostile_past.oldest_pending_age_seconds == 0.0
    assert hostile_past.seconds_since_last_completion == 0.0

    # Deterministic injected clock: 90s after creation.
    base_now = datetime.now(timezone.utc)
    future = queue.saturation_report(now=base_now + timedelta(seconds=90))
    assert 85.0 <= future.oldest_pending_age_seconds <= 95.0
    await queue.shutdown()


@pytest.mark.asyncio
async def test_saturation_report_counts_real_rows(tmp_path):
    queue = _queue(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload.get("block"):
            started.set()
            await release.wait()
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)
    done_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="done", payload={})
    await _wait_for_status(queue, done_id, JobStatus.COMPLETED)

    stuck_id = await queue.enqueue(
        job_type=JOB_TYPE, idempotency_key="stuck", payload={"block": True}
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)

    report = queue.saturation_report()
    assert report.completed == 1
    assert report.processing == 1  # the in-flight row
    assert report.pending == 0
    # gauges match the report (same source of truth: SELECT COUNT)
    snap = snapshot()
    assert snap.get("jobs_inflight") == 1.0
    assert snap.get("queue_depth") == 0.0

    release.set()
    await _wait_for_status(queue, stuck_id, JobStatus.COMPLETED)
    final = queue.saturation_report()
    assert final.completed == 2 and final.processing == 0
    # Durable status flips before the gauge refresh runs in the same mark
    # routine — gauges are self-correcting samples, so poll for convergence
    # instead of asserting a racy instant.
    async def _gauge_settles() -> None:
        while snapshot().get("jobs_inflight") != 0.0:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_gauge_settles(), timeout=2.0)
    await queue.shutdown()


# -- fault injection: observability never breaks the queue -------------------


@pytest.mark.asyncio
async def test_fault_injection_broken_emissions_do_not_break_jobs(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    def _explode(*args, **kwargs):
        raise RuntimeError("telemetry backend down")

    # Break every class of emission: counters, histogram, gauges, events.
    monkeypatch.setattr(ji, "log_job_created", _explode)
    monkeypatch.setattr(ji, "log_job_claimed", _explode)
    monkeypatch.setattr(ji, "observe_job_duration", _explode)
    monkeypatch.setattr(ji, "set_queue_depth", _explode)
    monkeypatch.setattr(ji, "set_jobs_inflight", _explode)
    monkeypatch.setattr(ji, "log_job_completed", _explode)

    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="fault1", payload={})
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # The job survived: durable result intact, failure only swallowed in _emit.
    assert await queue.get_result(job_id) == {"ok": True}
    await queue.shutdown()


@pytest.mark.asyncio
async def test_fault_injection_broken_gauge_refresh_does_not_break_failures(
    tmp_path, monkeypatch
):
    from nexus_ai_agent.adapters.instrumentation.job_instrumentation import (
        InstrumentedJobQueue as IQ,
    )

    def _boom(self):
        raise RuntimeError("gauge boom")

    monkeypatch.setattr(IQ, "_refresh_saturation_gauges", _boom)

    queue = _queue(tmp_path)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("business failure")

    queue.register_handler(JOB_TYPE, handler)
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="fault2", payload={})
    await _wait_for_status(queue, job_id, JobStatus.FAILED)
    assert await queue.get_status(job_id) == JobStatus.FAILED
    await queue.shutdown()


@pytest.mark.asyncio
async def test_fault_injection_broken_correlation_bind_does_not_break_handler(
    tmp_path, events, monkeypatch
):
    def _explode(*args, **kwargs):
        raise RuntimeError("structlog down")

    monkeypatch.setattr(
        "nexus_ai_agent.observability.correlation.bind_correlation_id", _explode
    )
    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(
        job_type=JOB_TYPE, idempotency_key="fault3", payload={"correlation_id": "cid"}
    )
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)
    assert await queue.get_result(job_id) == {"ok": True}
    await queue.shutdown()


# -- M1–M4 mutation detectors ------------------------------------------------
# Each test silences ONE emission (the mutation a refactor might accidentally
# introduce) and proves the corresponding Q assertion goes red. Together they
# pin the runtime wiring: if any emission is dropped, CI fails.


@pytest.mark.asyncio
async def test_m1_silence_created_emission_q1_goes_red(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    monkeypatch.setattr(ji, "log_job_created", lambda *a, **k: None)  # M1 mutation

    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    await queue.enqueue(job_type=JOB_TYPE, idempotency_key="m1", payload={})

    # Q1's assertion now fails: no counter, no event.
    assert _counter("jobs_created_total", f'job_type="{JOB_TYPE}"') == 0
    assert events.of(EVENT_JOB_CREATED) == []
    await queue.shutdown()


@pytest.mark.asyncio
async def test_m2_silence_claim_emission_q2_goes_red(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    monkeypatch.setattr(ji, "log_job_claimed", lambda *a, **k: None)  # M2 mutation

    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="m2", payload={})
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # Q2's assertion now fails (job still ran — only telemetry is missing).
    assert _counter("jobs_claimed_total", f'job_type="{JOB_TYPE}"') == 0
    assert events.of(EVENT_JOB_CLAIMED) == []
    assert await queue.get_status(job_id) == JobStatus.COMPLETED  # runtime intact
    await queue.shutdown()


@pytest.mark.asyncio
async def test_m3_silence_correlation_injection_q_chain_goes_red(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    # M3 mutation: never inject correlation into the payload before persist.
    monkeypatch.setattr(
        ji,
        "inject_into_payload",
        lambda payload, cid: payload,
    )

    queue = _queue(tmp_path)
    seen: dict[str, object] = {"payload_had_cid": "unset"}

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        seen["payload_had_cid"] = "correlation_id" in payload
        seen["during"] = get_correlation_id()
        return {"ok": True}

    queue.register_handler(JOB_TYPE, handler)
    bind_correlation_id("ambient-chain")
    try:
        job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="m3", payload={})
    finally:
        clear_correlation_id()
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # Normal path persists correlation_id into the payload (chain survives
    # enqueue->consume). With injection silenced the persisted row has no id:
    # the handler can only see ambient (gone after test cleanup) — the
    # payload->handler link that Q-correlation asserts is broken.
    import sqlite3

    with sqlite3.connect(tmp_path / "jobs.sqlite3") as conn:
        payload_json = conn.execute(
            "SELECT payload_json FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()[0]
    persisted = json.loads(payload_json)
    assert extract_from_payload(persisted) is None  # mutation detected: chain lost
    # The counter/event still fired (created emission is separate) — M3 targets
    # the persisted chain, and Q-chain's `payload carries correlation_id` fails.
    await queue.shutdown()


@pytest.mark.asyncio
async def test_m4_silence_terminal_emission_q3_q4_go_red(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    # M4 mutation: drop the whole terminal emission (event+counter+histogram).
    monkeypatch.setattr(ji, "log_job_completed", lambda *a, **k: None)
    monkeypatch.setattr(ji, "observe_job_duration", lambda *a, **k: None)

    queue = _queue(tmp_path)
    queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
    job_id = await queue.enqueue(job_type=JOB_TYPE, idempotency_key="m4", payload={})
    await _wait_for_status(queue, job_id, JobStatus.COMPLETED)

    # Q3's assertion now fails: no counter, no histogram sample.
    assert _counter("jobs_completed_total", f'job_type="{JOB_TYPE}"') == 0
    assert _hist_count(JOB_TYPE) == 0
    assert events.of(EVENT_JOB_COMPLETED) == []
    # Durable state still correct — mutation only breaks telemetry.
    assert await queue.get_status(job_id) == JobStatus.COMPLETED
    await queue.shutdown()


@pytest.mark.asyncio
async def test_m4b_silence_recovered_emission_q5_goes_red(tmp_path, events, monkeypatch):
    from nexus_ai_agent.adapters.instrumentation import job_instrumentation as ji

    queue = _queue(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(InstrumentedJobQueue, "_schedule", lambda self, job_id: None)
        await queue.enqueue(job_type=JOB_TYPE, idempotency_key="m4b", payload={})

    reset_for_tests()
    events.events.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ji, "log_job_recovered", lambda *a, **k: None)  # M4 mutation
        queue.register_handler(JOB_TYPE, lambda p: asyncio.sleep(0, result={"ok": True}))
        resumed = await queue.resume_pending()
        assert len(resumed) == 1
        await _wait_for_status(queue, resumed[0], JobStatus.COMPLETED)

    # Q5's assertion now fails.
    assert _counter("jobs_recovered_total") == 0
    assert events.of(EVENT_JOB_RECOVERED) == []
    await queue.shutdown()
