"""Gate 5 final repair — execution fencing, publication correctness, durable truth.

Every scenario here was RED on the pre-repair tree (PR#78 head ``5f273f08``
merged into main ``16daebc``) and is GREEN after the repair.  The five
reproductions of the adversarial review are kept under their original names
(R1..R5) next to the fifteen required adversarial scenarios (T1..T15).

Shared root cause (one abstraction, not three patches): job ownership was
keyed on ``job_id`` + ``status`` only, so a stale execution (cancelled,
reclaimed, or a duplicate claim from a second process) could still
complete / fail / reopen / notify / publish over the *current* execution.
The repair makes the per-row ``attempt`` counter the execution's **fencing
token** (``jobs.lifecycle.ExecutionClaim``): reservation is a PENDING-only
CAS that mints the token, every later worker-owned transition carries
``AND attempt = ?`` and reports commit-confirmed as a ``bool``, side effects
(publication, notification) run only for the current owner, and a refused
re-probe restores the previous published artifact.

"Two processes" are modelled as two ``InProcessJobQueue`` instances over
the same SQLite sidecar — exactly the situation of the bot process and the
``nexus jobs resume`` CLI (or two bot processes) sharing one durable queue.
Real process kills are not simulated; crash *windows* are injected.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    VERIFICATION_RESULT_KEY,
    ArtifactPublication,
    InProcessJobQueue,
    JobCompletion,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.lifecycle import TRANSITIONS, assert_transition, is_failure
from nexus_ai_agent.jobs.verification import VerificationOutcome

pytestmark = pytest.mark.integration

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _row(db: Path, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT status, attempt, result_json, error, started_at FROM nexus_job_queue "
            "WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


async def _wait_status(queue: InProcessJobQueue, job_id: str, wanted: JobStatus) -> None:
    for _ in range(500):
        if await queue.get_status(job_id) is wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {wanted}: {await queue.get_status(job_id)}")


async def _drain(queue: InProcessJobQueue, job_id: str) -> JobStatus:
    for _ in range(500):
        status = await queue.get_status(job_id)
        if status in TERMINAL:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def _settle(queue: InProcessJobQueue, job_id: str) -> None:
    """Let the queue's task for ``job_id`` (if any) run to its end."""
    task = queue._tasks.get(job_id)
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - outcome checked by caller
            pass
    await asyncio.sleep(0.02)


class _Recorder:
    """Completion hook recorder: (status, who) per notification."""

    def __init__(self) -> None:
        self.events: list[tuple[JobStatus, object]] = []

    async def __call__(self, completion: JobCompletion) -> None:
        who = (completion.result or {}).get("who") if completion.result else None
        self.events.append((completion.status, who))

    def successes(self) -> list[object]:
        return [who for status, who in self.events if status is JobStatus.COMPLETED]


class _Gate:
    """A handler whose N-th call parks on its own event.

    Call 1 is "worker A" (the first, soon-to-be-stale execution); call 2 is
    "worker B" (the current owner after the takeover).  Each call returns a
    result that names who produced it, so the durable truth can be checked.
    """

    def __init__(self, *, raise_on: set[int] | None = None) -> None:
        self.calls = 0
        self.events: dict[int, asyncio.Event] = {}
        self.raise_on = raise_on or set()

    def event(self, call: int) -> asyncio.Event:
        return self.events.setdefault(call, asyncio.Event())

    async def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        call = self.calls
        await self.event(call).wait()
        if call in self.raise_on:
            raise RuntimeError(f"worker {call} crashed on purpose")
        return {"who": f"worker-{call}"}


def _two_queues(
    db: Path, handler: Any, *, hook_a: Any = None, hook_b: Any = None
) -> tuple[InProcessJobQueue, InProcessJobQueue]:
    queue_a = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=hook_a)
    queue_b = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=hook_b)
    queue_a.register_handler("fenced", handler)
    queue_b.register_handler("fenced", handler)
    return queue_a, queue_b


# ===========================================================================
# R5 / T5 — WHO OWNS A JOB: reservation is a PENDING-only CAS
# ===========================================================================
@pytest.mark.asyncio
async def test_t5_two_processes_cannot_both_own_one_execution(tmp_path: Path) -> None:
    """R5 reclassified: with PROCESSING accepted by the reservation UPDATE, a
    duplicate ``enqueue`` (same idempotency key) from a *second process*
    re-claimed a row that was already executing and ran the handler twice.
    Now the reservation is PENDING → PROCESSING only."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="dup", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert _row(db, job_id)["attempt"] == 1

    # second process: identical dispatch collapses on the key, then tries to run it
    duplicate = await queue_b.enqueue(job_type="fenced", idempotency_key="dup", payload={})
    assert duplicate == job_id
    await _settle(queue_b, job_id)

    assert gate.calls == 1, "a PROCESSING row must not be claimable a second time"
    assert _row(db, job_id)["attempt"] == 1, "no second reservation may mint a token"

    gate.event(1).set()
    assert await _drain(queue_a, job_id) is JobStatus.COMPLETED
    assert (await queue_a.get_result(job_id) or {})["who"] == "worker-1"


def test_reservation_matrix_has_no_processing_to_processing_edge() -> None:
    """The old ``(PROCESSING, PROCESSING)`` 'resume reclaim' edge is gone:
    takeover is an explicit, fenced PROCESSING/VERIFYING → PENDING reset
    followed by a fresh PENDING → PROCESSING reservation."""
    assert (JobStatus.PROCESSING, JobStatus.PROCESSING) not in TRANSITIONS
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.PROCESSING, JobStatus.PROCESSING)


# ===========================================================================
# F2 / R2 — cancellation / reclaim race
# ===========================================================================
@pytest.mark.asyncio
async def test_t1_cancelled_old_worker_cannot_reopen_a_newer_execution(tmp_path: Path) -> None:
    """R2 reproduction: A is PROCESSING, B (another process) takes the row
    over and COMPLETES it; then A is cancelled and its ``_mark_pending``
    ran ``WHERE id = ?`` — the completed job was reopened to PENDING."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)

    # takeover by the second process (startup recovery) → new execution
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    assert gate.calls == 2 and _row(db, job_id)["attempt"] == 2
    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED

    # the stale worker is cancelled now
    queue_a._tasks[job_id].cancel()
    await _settle(queue_a, job_id)

    assert await queue_a.get_status(job_id) is JobStatus.COMPLETED, "COMPLETED must never reopen"
    assert _row(db, job_id)["attempt"] == 2
    assert (await queue_a.get_result(job_id) or {})["who"] == "worker-2"


@pytest.mark.asyncio
async def test_t1b_cancelled_old_worker_cannot_reset_the_new_owners_processing_row(
    tmp_path: Path,
) -> None:
    """Same race while the new owner is still PROCESSING: the stale
    ``processing → pending`` must not land on execution 2."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    assert _row(db, job_id)["attempt"] == 2

    queue_a._tasks[job_id].cancel()
    await _settle(queue_a, job_id)
    row = _row(db, job_id)
    assert row["status"] == JobStatus.PROCESSING.value, "stale cancel must not reset the new owner"
    assert row["started_at"] is not None

    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED


# ===========================================================================
# F3 / R3 + T2 / T3 / T4 / T15 — durable completion truth
# ===========================================================================
@pytest.mark.asyncio
async def test_t2_old_worker_cannot_mark_newer_attempt_completed(tmp_path: Path) -> None:
    """pg-boss #925 shape: A's handler returns after B took over; A's
    ``_mark_completed ... WHERE status IN (processing, verifying)`` matched
    B's live row and settled execution 2 with execution 1's result."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    hook_a, hook_b = _Recorder(), _Recorder()
    queue_a, queue_b = _two_queues(db, gate, hook_a=hook_a, hook_b=hook_b)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)

    gate.event(1).set()  # stale worker finishes first
    await _settle(queue_a, job_id)
    row = _row(db, job_id)
    assert row["status"] == JobStatus.PROCESSING.value, "execution 2 is still running"
    assert row["result_json"] is None
    assert hook_a.events == [], "T4: the stale worker must not notify success"

    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED
    assert (await queue_b.get_result(job_id) or {})["who"] == "worker-2"
    assert hook_b.successes() == ["worker-2"]
    assert hook_a.events == []


@pytest.mark.asyncio
async def test_t3_old_worker_cannot_mark_newer_attempt_failed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate(raise_on={1})
    hook_a, hook_b = _Recorder(), _Recorder()
    queue_a, queue_b = _two_queues(db, gate, hook_a=hook_a, hook_b=hook_b)

    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)

    gate.event(1).set()  # stale worker crashes
    await _settle(queue_a, job_id)
    row = _row(db, job_id)
    assert row["status"] == JobStatus.PROCESSING.value, "stale failure must not land on attempt 2"
    assert row["error"] is None
    assert hook_a.events == [], "a fenced-off failure is not announced either"

    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED
    assert hook_b.successes() == ["worker-2"]


def _always_ok(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
    return VerificationOutcome(ok=True, reason_code=None, summary={"status": "verified"})


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_lane", [False, True], ids=["unverified", "verified"])
async def test_t15_no_success_notification_without_durable_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified_lane: bool
) -> None:
    """R3 reproduction: ``_mark_completed`` CAS-missed (row not ours) and the
    queue still announced ✅ — durable status ≠ notification.  Now the
    transition result gates the notification — on both commit paths (job
    types without a verifier and the verify → commit lane)."""
    import nexus_ai_agent.adapters.in_process_job_queue as queue_module

    db = tmp_path / "jobs.sqlite3"
    hook = _Recorder()
    verifiers: dict[str, Any] = {"plain": _always_ok} if verified_lane else {}
    queue = InProcessJobQueue(db, artifact_verifiers=verifiers, on_job_finished=hook)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"who": "me"}

    queue.register_handler("plain", handler)
    original = queue_module.InProcessJobQueue._mark_completed

    def _cas_miss(self: Any, *args: Any, **kwargs: Any) -> bool:
        # the durable write is *not* performed: simulate a lost ownership race
        return False

    monkeypatch.setattr(queue_module.InProcessJobQueue, "_mark_completed", _cas_miss)
    job_id = await queue.enqueue(job_type="plain", idempotency_key="k", payload={})
    await _settle(queue, job_id)
    monkeypatch.setattr(queue_module.InProcessJobQueue, "_mark_completed", original)

    assert _row(db, job_id)["status"] != JobStatus.COMPLETED.value
    assert hook.successes() == [], "no ✅ without a committed COMPLETED row"


@pytest.mark.asyncio
async def test_r4_bare_success_false_is_a_failure_not_a_completion(tmp_path: Path) -> None:
    """R4: ``{"success": False}`` without an ``error_code`` is still the
    handler saying "I failed"; an unverified lane completed it."""
    db = tmp_path / "jobs.sqlite3"
    hook = _Recorder()
    queue = InProcessJobQueue(db, artifact_verifiers={}, on_job_finished=hook)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"success": False}

    queue.register_handler("bare", handler)
    job_id = await queue.enqueue(job_type="bare", idempotency_key="k", payload={})
    status = await _drain(queue, job_id)
    assert is_failure(status)
    assert _row(db, job_id)["error"] == "typed_failure:unspecified"
    assert hook.successes() == []


# ===========================================================================
# T6 / T7 — explicit, fenced takeover
# ===========================================================================
@pytest.mark.asyncio
async def test_t6_takeover_after_valid_expiry_reclaims_the_row(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)
    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)

    # the row has been executing "for two hours"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )
    assert await queue_b.resume_pending(stale_after=timedelta(hours=1)) == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    assert _row(db, job_id)["attempt"] == 2
    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED
    gate.event(1).set()
    await _settle(queue_a, job_id)
    assert (await queue_a.get_result(job_id) or {})["who"] == "worker-2"


@pytest.mark.asyncio
async def test_t7_takeover_without_valid_expiry_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue_a, queue_b = _two_queues(db, gate)
    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)

    assert await queue_b.resume_pending(stale_after=timedelta(hours=1)) == []
    await asyncio.sleep(0.02)
    row = _row(db, job_id)
    assert row["status"] == JobStatus.PROCESSING.value and row["attempt"] == 1
    assert gate.calls == 1

    # the operator entry point never touches in-flight rows either
    assert await queue_b.resume_pending_jobs() == []
    gate.event(1).set()
    assert await _drain(queue_a, job_id) is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_startup_takeover_skips_rows_this_process_is_executing(tmp_path: Path) -> None:
    """Calling ``resume_pending`` in the owner process itself must not
    reset its own live execution (which would strand the row in PENDING)."""
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate()
    queue = InProcessJobQueue(db, artifact_verifiers={})
    queue.register_handler("fenced", gate)
    job_id = await queue.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue, job_id, JobStatus.PROCESSING)
    assert await queue.resume_pending() == []
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value
    gate.event(1).set()
    assert await _drain(queue, job_id) is JobStatus.COMPLETED


# ===========================================================================
# T12 / T13 / T14 — terminal states never reopen; no silent retry
# ===========================================================================
@pytest.mark.asyncio
async def test_t12_completed_state_never_reopens(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("plain", handler)
    job_id = await queue.enqueue(job_type="plain", idempotency_key="k", payload={})
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

    claim = ExecutionClaim(job_id=job_id, attempt=1)
    assert queue._mark_pending(claim) is False
    assert queue._mark_verifying(claim) is False
    assert queue._mark_failed(claim, "late", JobStatus.FAILED_TERMINAL) is False
    assert queue._mark_completed(claim, {"late": True}) is False
    assert await queue.resume_pending() == []
    assert await queue.shutdown() is None
    row = _row(db, job_id)
    assert row["status"] == JobStatus.COMPLETED.value and row["attempt"] == 1


@pytest.mark.asyncio
async def test_t13_failed_terminal_state_never_reopens(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    gate = _Gate(raise_on={2})
    queue_a, queue_b = _two_queues(db, gate)
    job_id = await queue_a.enqueue(job_type="fenced", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)
    gate.event(2).set()
    failed = await _drain(queue_b, job_id)
    assert is_failure(failed)

    queue_a._tasks[job_id].cancel()  # stale cancel after the terminal failure
    await _settle(queue_a, job_id)
    assert await queue_a.get_status(job_id) is failed
    from nexus_ai_agent.jobs.lifecycle import ExecutionClaim

    assert queue_a._mark_pending(ExecutionClaim(job_id=job_id, attempt=2)) is False
    assert await queue_a.get_status(job_id) is failed


@pytest.mark.asyncio
async def test_t14_retryable_state_cannot_silently_retry_without_a_scheduler(
    tmp_path: Path,
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        raise OSError(28, "no space left on device")  # ENOSPC ⇒ RETRYABLE

    queue.register_handler("io", handler)
    job_id = await queue.enqueue(job_type="io", idempotency_key="k", payload={})
    assert await _drain(queue, job_id) is JobStatus.FAILED_RETRYABLE
    assert await queue.resume_pending() == []
    assert await queue.resume_pending_jobs() == []
    assert await queue.resume_pending(stale_after=timedelta(0)) == []
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.FAILED_RETRYABLE, JobStatus.PENDING)
    assert _row(db, job_id)["status"] == JobStatus.FAILED_RETRYABLE.value


# ===========================================================================
# F1 / R1 + T8 / T9 / T10 / T11 — artifact publication correctness
# ===========================================================================
def make_pdf_with_text(dest: Path, text: str) -> Path:
    """A real one-page PDF with extractable text and correct xref offsets
    (self-contained: CI runs the bare pytest console script)."""
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        ),
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    dest.write_bytes(bytes(out))
    return dest


def _pdf_env(tmp_path: Path, text: str) -> tuple[Path, Path]:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    return pdf_dir, make_pdf_with_text(pdf_dir / "doc.pdf", text)


@pytest.fixture
def fake_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    class _FakeRag:
        async def add_document(self, *args: object, **kwargs: object) -> None:
            return None

    module = types.SimpleNamespace(AdvancedRAGEngine=_FakeRag)
    monkeypatch.setitem(__import__("sys").modules, "nexus_ai_agent.features.rag", module)


@pytest.mark.asyncio
async def test_t9_reprobe_failure_restores_the_previous_artifact(
    tmp_path: Path, fake_rag: None
) -> None:
    """R1 reproduction: ``os.replace(staged, published)`` destroyed the
    previous valid artifact *before* the re-probe; when the re-probe refused,
    the old bytes were already gone and the refused bytes stayed published.
    Now publication keeps a recoverable backup and a refused re-probe
    restores it byte-for-byte."""
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_extract_verifier,
        pdf_text_artifact_path,
        pdf_text_staged_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir, source = _pdf_env(tmp_path, "fresh extraction")
    final = pdf_text_artifact_path(source)
    final.write_text("OLD VALID EXTRACTION", encoding="utf-8")

    def refusing_reprobe(
        payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        if str(result.get("artifact_path")) == str(final):
            return VerificationOutcome(
                ok=False,
                reason_code="sha256_mismatch",
                summary={"status": "failed", "reason_code": "sha256_mismatch"},
            )
        return pdf_extract_verifier(payload, result)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    queue.register_artifact_verifier("pdf_extract", refusing_reprobe)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="reprobe",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f"},
    )
    status = await _drain(queue, job_id)
    assert status is JobStatus.FAILED_RETRYABLE
    chain = await queue.get_result_chain(job_id)
    assert chain["failure_reason"] == "verification_failed:reprobe_failed"
    assert final.read_text(encoding="utf-8") == "OLD VALID EXTRACTION"
    assert not pdf_text_staged_path(source).exists()
    assert sorted(p.name for p in pdf_dir.iterdir()) == ["doc.extracted.txt", "doc.pdf"]


@pytest.mark.asyncio
async def test_t9b_reprobe_failure_without_previous_artifact_leaves_no_artifact(
    tmp_path: Path, fake_rag: None
) -> None:
    from nexus_ai_agent.jobs.feature_verification import (
        pdf_extract_verifier,
        pdf_text_artifact_path,
    )
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir, source = _pdf_env(tmp_path, "fresh extraction")
    final = pdf_text_artifact_path(source)

    def refusing_reprobe(
        payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        if str(result.get("artifact_path")) == str(final):
            raise RuntimeError("probe tool exploded")  # verifier crash at re-probe
        return pdf_extract_verifier(payload, result)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    queue.register_artifact_verifier("pdf_extract", refusing_reprobe)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="reprobe2",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f"},
    )
    assert is_failure(await _drain(queue, job_id))
    assert not final.exists(), "refused bytes must not occupy the final name"
    assert sorted(p.name for p in pdf_dir.iterdir()) == ["doc.pdf"]


@pytest.mark.asyncio
async def test_t10_publish_failure_midway_preserves_previous_artifact(
    tmp_path: Path, fake_rag: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename itself fails after the backup step: previous artifact
    intact, staged temp retracted, no backup residue."""
    import nexus_ai_agent.jobs.feature_verification as fv
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir, source = _pdf_env(tmp_path, "fresh extraction")
    final = fv.pdf_text_artifact_path(source)
    final.write_text("OLD VALID EXTRACTION", encoding="utf-8")
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if str(dst) == str(final):
            raise OSError(5, "input/output error")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(fv.os, "replace", failing_replace)
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="pubfail",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f"},
    )
    status = await _drain(queue, job_id)
    assert is_failure(status)
    chain = await queue.get_result_chain(job_id)
    assert chain["failure_reason"] == "verification_failed:publish_failed"
    assert final.read_text(encoding="utf-8") == "OLD VALID EXTRACTION"
    assert sorted(p.name for p in pdf_dir.iterdir()) == ["doc.extracted.txt", "doc.pdf"]


@pytest.mark.asyncio
async def test_t11_crash_between_publish_and_reprobe_recovers(
    tmp_path: Path, fake_rag: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash window: the rename happened, the re-probe never ran, the row is
    still VERIFYING.  Startup takeover re-runs the job; the backup residue
    of the crashed attempt is cleaned and the final bytes are the verified
    ones — no stray files remain."""
    import nexus_ai_agent.adapters.in_process_job_queue as queue_module
    from nexus_ai_agent.jobs.feature_verification import pdf_text_artifact_path
    from nexus_ai_agent.worker import process_pdf_job

    pdf_dir, source = _pdf_env(tmp_path, "survives a crash")
    final = pdf_text_artifact_path(source)
    final.write_text("OLD VALID EXTRACTION", encoding="utf-8")

    original = queue_module.InProcessJobQueue._verify_safely
    state = {"crashed": False}

    async def crash_at_reprobe(self: Any, verifier: Any, payload: Any, result: Any) -> Any:
        if not state["crashed"] and str(result.get("artifact_path")) == str(final):
            state["crashed"] = True
            raise RuntimeError("process died between rename and re-probe")
        return await original(self, verifier, payload, result)

    monkeypatch.setattr(queue_module.InProcessJobQueue, "_verify_safely", crash_at_reprobe)
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("pdf_extract", process_pdf_job)
    job_id = await queue.enqueue(
        job_type="pdf_extract",
        idempotency_key="crash",
        payload={"user_id": 1, "file_path": str(source), "file_id": "f"},
    )
    await _settle(queue, job_id)
    assert state["crashed"]
    assert _row(db := tmp_path / "jobs.sqlite3", job_id)["status"] == JobStatus.VERIFYING.value
    assert (pdf_dir / "doc.extracted.txt.prev").exists(), "the backup survives the crash"

    # a fresh process takes the orphaned row over
    recovered = InProcessJobQueue(db)
    recovered.register_handler("pdf_extract", process_pdf_job)
    assert await recovered.resume_pending() == [job_id]
    assert await _drain(recovered, job_id) is JobStatus.COMPLETED
    assert "survives a crash" in final.read_text(encoding="utf-8")
    assert sorted(p.name for p in pdf_dir.iterdir()) == ["doc.extracted.txt", "doc.pdf"]
    assert _row(db, job_id)["attempt"] == 2


@pytest.mark.asyncio
async def test_t8_stale_worker_publication_cannot_overwrite_current_owners_artifact(
    tmp_path: Path,
) -> None:
    """Side-effect fencing: A's handler returns after B took over.  Before
    the repair A's ``processing → verifying`` matched B's row, A published
    over B's destination and completed B's execution.  Now A is fenced
    before verification, never publishes, and B's artifact wins."""
    db = tmp_path / "jobs.sqlite3"
    destination = tmp_path / "published.txt"
    published_by: list[str] = []

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "verified"})

    def publish(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        destination.write_text(str(result["who"]), encoding="utf-8")
        published_by.append(str(result["who"]))
        return {"artifact_path": str(destination)}

    publication = ArtifactPublication(publish=publish, retract=lambda p, r: None)
    gate = _Gate()
    queue_a = InProcessJobQueue(
        db, artifact_verifiers={"pub": verifier}, artifact_publications={"pub": publication}
    )
    queue_b = InProcessJobQueue(
        db, artifact_verifiers={"pub": verifier}, artifact_publications={"pub": publication}
    )
    queue_a.register_handler("pub", gate)
    queue_b.register_handler("pub", gate)

    job_id = await queue_a.enqueue(job_type="pub", idempotency_key="k", payload={})
    await _wait_status(queue_a, job_id, JobStatus.PROCESSING)
    assert await queue_b.resume_pending() == [job_id]
    await _wait_status(queue_b, job_id, JobStatus.PROCESSING)

    gate.event(1).set()  # stale worker returns first
    await _settle(queue_a, job_id)
    assert published_by == [], "a fenced execution must not publish"
    assert not destination.exists()
    assert _row(db, job_id)["status"] == JobStatus.PROCESSING.value

    gate.event(2).set()
    assert await _drain(queue_b, job_id) is JobStatus.COMPLETED
    assert published_by == ["worker-2"]
    assert destination.read_text(encoding="utf-8") == "worker-2"
    result = await queue_b.get_result(job_id) or {}
    assert result["who"] == "worker-2" and result[VERIFICATION_RESULT_KEY]["status"] == "verified"


@pytest.mark.asyncio
async def test_t8b_publication_is_fenced_immediately_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is re-checked right before ``publish`` runs (the last
    fence before the irreversible side effect): a takeover that lands
    between verification and publication stops the publish."""
    db = tmp_path / "jobs.sqlite3"
    destination = tmp_path / "published.txt"
    published: list[str] = []
    hook = _Recorder()

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "verified"})

    def publish(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        destination.write_text("stale", encoding="utf-8")
        published.append("stale")
        return {"artifact_path": str(destination)}

    queue = InProcessJobQueue(
        db,
        artifact_verifiers={"pub": verifier},
        artifact_publications={
            "pub": ArtifactPublication(publish=publish, retract=lambda p, r: None)
        },
        on_job_finished=hook,
    )

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"who": "stale"}

    queue.register_handler("pub", handler)

    import nexus_ai_agent.adapters.in_process_job_queue as queue_module

    original = queue_module.InProcessJobQueue._verify_safely

    async def takeover_after_verify(self: Any, verifier: Any, payload: Any, result: Any) -> Any:
        outcome = await original(self, verifier, payload, result)
        # another process takes the row over while we hold a green verdict
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE nexus_job_queue SET status = 'processing', attempt = attempt + 1"
            )
        return outcome

    monkeypatch.setattr(queue_module.InProcessJobQueue, "_verify_safely", takeover_after_verify)
    job_id = await queue.enqueue(job_type="pub", idempotency_key="k", payload={})
    await _settle(queue, job_id)

    assert published == [] and not destination.exists()
    assert hook.events == []
    row = _row(db, job_id)
    assert row["status"] == JobStatus.PROCESSING.value and row["attempt"] == 2
