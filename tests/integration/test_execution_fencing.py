"""Gate-5 FINAL REPAIR — T1-T15 execution-ownership adversarial suite.

Every test is one named acceptance item of the repair (mission section 19):
a scenario, an assertion of non-breach, and — where the pre-repair tree was
breached — the exact legacy behavior now impossible (OLD evidence: the
R1-R5 repros at ``6b86633`` in the repair report; mutation probes M1-M10
kill each guard).

The queue is single-process by design (asyncio tasks + one SQLite sidecar);
"two processes" below are two ``InProcessJobQueue`` instances over one DB
file — the exact durable-state interleaving the contract must survive.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobCompletion,
    default_artifact_publications,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.feature_verification import (
    pdf_extract_verifier,
    pdf_text_artifact_path,
    pdf_text_backup_path,
    pdf_text_staged_path,
    publish_pdf_text_artifact,
    publish_text_artifact,
    recover_pdf_text_publication,
    retire_pdf_text_artifact,
    retract_pdf_text_artifact,
)
from nexus_ai_agent.jobs.fencing import PUBLICATION_JOURNAL_KEY, ExecutionToken
from nexus_ai_agent.jobs.lifecycle import assert_transition
from nexus_ai_agent.jobs.verification import VerificationOutcome

pytestmark = pytest.mark.integration

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


async def _ok(payload: dict[str, object]) -> dict[str, object]:
    return {"ok": True}


def _insert(queue: InProcessJobQueue, key: str) -> str:
    """A pending row with NO auto-scheduling (the raw claim surface)."""
    return queue._insert_or_get("t", key, {})


def _row(db_path: Path, job_id: str) -> tuple[Any, ...]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "SELECT status, attempt, owner_token, result_json, error FROM nexus_job_queue "
            "WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        con.close()


def _backdate_lease(db_path: Path, job_id: str) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", job_id),
    )
    con.commit()
    con.close()


def _steal(db_a: InProcessJobQueue, db_b: InProcessJobQueue, job_id: str) -> ExecutionToken:
    """W2's fenced takeover of a lease-lapsed generation, then its own claim."""
    _backdate_lease(db_a.db_path, job_id)
    reclaimed = db_b._reclaim_stale()
    assert job_id in reclaimed
    token = db_b._mark_processing(job_id)
    assert token is not None
    return token


def _claiming_handler(body: str):
    async def handler(payload: dict[str, object]) -> dict[str, object]:
        src = Path(str(payload["file_path"]))
        staged = pdf_text_staged_path(src)
        publish_text_artifact(staged, body)
        import hashlib

        data = staged.read_bytes()
        return {
            "message": "ok",
            "artifact_path": str(staged),
            "artifact_kind": "text",
            "content_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    return handler


def _pdf_queue(
    db_path: Path,
    body: str,
    *,
    verifier: Any = pdf_extract_verifier,
    hook: Any = None,
) -> InProcessJobQueue:
    return InProcessJobQueue(
        db_path,
        {"pdf_extract": _claiming_handler(body)},
        on_job_finished=hook,
        artifact_verifiers={"pdf_extract": verifier},
        artifact_publications=default_artifact_publications(),
    )


# ------------------------------------------------------------------ T1
@pytest.mark.asyncio
async def test_t1_stale_cancel_cannot_reopen_newer_execution(tmp_path: Path) -> None:
    """T1: a cancelled OLD worker cannot reopen a NEWER execution to pending."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t1")
    tok1 = q1._mark_processing(job)
    assert tok1 is not None
    tok2 = _steal(q1, q2, job)

    # OLD worker's cancelled task fires late — twice: mid-run and after terminal.
    assert q1._mark_pending(job, tok1) is False
    assert _row(db, job)[0] == JobStatus.PROCESSING.value  # gen-2 run untouched
    assert q2._mark_completed(job, {"by": 2}, tok2) is True
    assert q1._mark_pending(job, tok1) is False
    status, attempt, owner, result_json, _ = _row(db, job)
    assert status == JobStatus.COMPLETED.value
    assert attempt == 2 and result_json == '{"by": 2}'


# ------------------------------------------------------------------ T2/T3
@pytest.mark.asyncio
async def test_t2_t3_stale_worker_cannot_mark_newer_attempt_completed_or_failed(
    tmp_path: Path,
) -> None:
    """T2/T3: an OLD worker cannot mark a NEWER attempt completed or failed."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t2")
    tok1 = q1._mark_processing(job)
    tok2 = _steal(q1, q2, job)

    assert q1._mark_completed(job, {"by": "stale"}, tok1) is False
    assert q1._mark_failed(job, "stale boom", JobStatus.FAILED_TERMINAL, None, tok1) is False
    status, _, _, result_json, error = _row(db, job)
    assert status == JobStatus.PROCESSING.value
    assert result_json != '{"by": "stale"}' and error is None

    # The CURRENT owner commits fine — proving the refusals were the fence.
    assert q2._mark_completed(job, {"by": 2}, tok2) is True
    assert q1._mark_failed(job, "late boom", JobStatus.FAILED_RETRYABLE, None, tok1) is False
    assert _row(db, job)[0] == JobStatus.COMPLETED.value


# ------------------------------------------------------------------ T4/T15
@pytest.mark.asyncio
async def test_t4_t15_no_success_notification_without_durable_completed(
    tmp_path: Path,
) -> None:
    """T4/T15: a stale worker can never announce success; every success
    notification is exactly one durable COMMIT_CONFIRMED outcome."""
    done = asyncio.Event()
    calls: list[JobCompletion] = []

    async def slow(payload: dict[str, object]) -> dict[str, object]:
        await done.wait()
        return {"ok": True}

    async def hook(completion: JobCompletion) -> None:
        calls.append(completion)

    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": slow}, on_job_finished=hook)
    job = await q1.enqueue(job_type="t", idempotency_key="t4", payload={})
    for _ in range(200):
        if (await q1.get_status(job)) is JobStatus.PROCESSING:
            break
        await asyncio.sleep(0.01)
    await _steal_and_finish(db, job, hook)
    done.set()  # OLD worker finishes and reports success
    await asyncio.sleep(0.3)

    assert [c.status for c in calls] == [JobStatus.COMPLETED]  # exactly one, the durable one
    assert all(c.job_id == job for c in calls)
    assert (await q1.get_status(job)) is JobStatus.COMPLETED
    assert (await q1.get_result(job)) == {"ok": True}


async def _steal_and_finish(db: Path, job: str, hook: Any) -> None:
    """W2 takes over the lapsed lease and durably completes the job through
    the real execution path (so the success fan-out is the durable one)."""
    q2 = InProcessJobQueue(db, {"t": _ok}, on_job_finished=hook)
    _backdate_lease(db, job)
    assert job in q2._reclaim_stale()
    assert job in await q2.resume_pending()
    for _ in range(200):
        if (await q2.get_status(job)) in TERMINAL:
            break
        await asyncio.sleep(0.01)
    assert (await q2.get_status(job)) is JobStatus.COMPLETED


# ------------------------------------------------------------------ T5
def test_t5_two_processes_cannot_both_own_one_execution(tmp_path: Path) -> None:
    """T5: one durable job → one valid owner → one fenced generation. The
    contract does NOT allow dual claim (R5 reclassified: real ownership bug)."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t5")

    first = q1._mark_processing(job)
    second = q2._mark_processing(job)
    assert first is not None
    assert second is None  # strict PENDING→PROCESSING CAS: no re-claim of live work
    status, attempt, owner, _, _ = _row(db, job)
    assert (status, attempt) == (JobStatus.PROCESSING.value, 1)
    assert owner == first.owner_token

    # And the concurrent form: exactly one thread wins a fresh row.
    job2 = _insert(q1, "t5b")
    results: list[ExecutionToken | None] = []
    import threading

    def claim(q: InProcessJobQueue) -> None:
        results.append(q._mark_processing(job2))

    threads = [threading.Thread(target=claim, args=(q,)) for q in (q1, q2, q1, q2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert _row(db, job2)[1] == 1


# ------------------------------------------------------------------ T6
def test_t6_takeover_after_valid_expiry_works(tmp_path: Path) -> None:
    """T6: startup recovery takes over exactly the lease-expired generations."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t6")
    tok1 = q1._mark_processing(job)
    assert tok1 is not None

    _backdate_lease(db, job)
    assert q2._reclaim_stale() == [job]
    status, attempt, owner, _, _ = _row(db, job)
    assert status == JobStatus.PENDING.value
    assert owner is None  # displaced holder invalidated
    assert attempt == 1  # generation preserved (advances only at reservation)

    tok2 = q2._mark_processing(job)
    assert tok2 is not None and tok2.generation == 2 and tok2.owner_token != tok1.owner_token
    # The displaced token is now definitively dead:
    assert q1._mark_completed(job, {}, tok1) is False
    assert q2._mark_completed(job, {"ok": 2}, tok2) is True


# ------------------------------------------------------------------ T7
def test_t7_takeover_without_valid_expiry_rejected(tmp_path: Path) -> None:
    """T7: a fresh lease is never reclaimed — takeover without a valid
    expiry is rejected at the fence."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t7")
    tok1 = q1._mark_processing(job)
    assert tok1 is not None

    assert q2._reclaim_stale() == []  # fresh lease: no takeover
    status, attempt, owner, _, _ = _row(db, job)
    assert (status, attempt, owner) == (JobStatus.PROCESSING.value, 1, tok1.owner_token)
    assert q2._mark_processing(job) is None  # cannot claim live work
    assert q1._mark_completed(job, {"ok": 1}, tok1) is True  # owner unharmed


# ------------------------------------------------------------------ T8
def test_t8_stale_publication_cannot_overwrite_current_owner_artifact(tmp_path: Path) -> None:
    """T8: a stale worker's publication cannot overwrite the current owner's
    artifact — neither through the journal fence nor through a late restore."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\nfake\n")
    published = pdf_text_artifact_path(src)
    staged = pdf_text_staged_path(src)

    # The current owner's live artifact (O committed earlier; Y2 published now).
    publish_text_artifact(published, "CURRENT OWNER")
    publish_text_artifact(staged, "STALE CANDIDATE")
    stale_result = {
        "artifact_path": str(staged),
        PUBLICATION_JOURNAL_KEY: {"backup": "gone", "published_inode": 0},
    }

    # Stale retract with a bogus/dead journal: inode guard refuses to touch
    # the destination (published inode != 0).
    retract_pdf_text_artifact(
        {"file_path": str(src)},
        {
            **stale_result,
            PUBLICATION_JOURNAL_KEY: {
                "backup": str(pdf_text_backup_path(src)),
                "published_inode": 0,
                "had_previous": True,
            },
        },
    )
    assert published.read_text(encoding="utf-8") == "CURRENT OWNER"

    # And the DB-side journal write of a stale generation matches zero rows.
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t8")
    tok1 = q1._mark_processing(job)
    tok2 = _steal(q1, q2, job)
    assert q1._record_publication(job, tok1, {"stale": True}) is False
    assert q2._record_publication(job, tok2, {"fresh": True}) is True
    assert _row(db, job)[3] == '{"fresh": true}'


# ------------------------------------------------------------------ T9
@pytest.mark.asyncio
async def test_t9_reprobe_failure_preserves_previous_artifact(tmp_path: Path) -> None:
    """T9: a post-publish re-probe failure preserves the previous artifact
    (the exact F1 breach R1 proved on the pre-repair tree)."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\nfake\n")
    published = pdf_text_artifact_path(src)

    q1 = _pdf_queue(tmp_path / "q.sqlite3", "OLD GENERATION")
    job1 = await q1.enqueue(
        job_type="pdf_extract",
        idempotency_key="t9-1",
        payload={"user_id": 1, "file_id": "f1", "file_path": str(src)},
    )
    for _ in range(400):
        if (await q1.get_status(job1)) in TERMINAL:
            break
        await asyncio.sleep(0.01)
    assert (await q1.get_status(job1)) is JobStatus.COMPLETED
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"

    def poisoned(payload, result):
        if str(result.get("artifact_path", "")).endswith(".staged"):
            return pdf_extract_verifier(payload, result)
        return VerificationOutcome(
            ok=False,
            reason_code="probe_failed",
            summary={"status": "failed", "reason_code": "probe_failed", "detail": "poisoned"},
        )

    q2 = _pdf_queue(tmp_path / "q.sqlite3", "NEW GENERATION", verifier=poisoned)
    job2 = await q2.enqueue(
        job_type="pdf_extract",
        idempotency_key="t9-2",
        payload={"user_id": 1, "file_id": "f2", "file_path": str(src)},
    )
    for _ in range(400):
        if (await q2.get_status(job2)) in TERMINAL:
            break
        await asyncio.sleep(0.01)
    assert (await q2.get_status(job2)) is not JobStatus.COMPLETED
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"


# ------------------------------------------------------------------ T10
@pytest.mark.asyncio
async def test_t10_publish_failure_preserves_previous_artifact(tmp_path: Path) -> None:
    """T10: a publish failure preserves the previous artifact AND the staged
    temp is retracted (unchanged refusal semantics of the pre-repair lane)."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\nfake\n")
    published = pdf_text_artifact_path(src)
    staged = pdf_text_staged_path(src)
    publish_text_artifact(published, "OLD GENERATION")

    def exploding_publish(
        payload: dict[str, object], result: dict[str, object]
    ) -> dict[str, object]:
        raise OSError("publish exploded")

    calls: list[JobCompletion] = []

    async def hook(completion: JobCompletion) -> None:
        calls.append(completion)

    from nexus_ai_agent.adapters.in_process_job_queue import ArtifactPublication

    q = InProcessJobQueue(
        tmp_path / "q.sqlite3",
        {"pdf_extract": _claiming_handler("NEW GENERATION")},
        on_job_finished=hook,
        artifact_verifiers={"pdf_extract": pdf_extract_verifier},
        artifact_publications={
            "pdf_extract": ArtifactPublication(
                publish=exploding_publish,
                retract=retract_pdf_text_artifact,
                recover=recover_pdf_text_publication,
                retire=retire_pdf_text_artifact,
            )
        },
    )
    job = await q.enqueue(
        job_type="pdf_extract",
        idempotency_key="t10",
        payload={"user_id": 1, "file_id": "f1", "file_path": str(src)},
    )
    for _ in range(400):
        if (await q.get_status(job)) in TERMINAL:
            break
        await asyncio.sleep(0.01)
    assert (await q.get_status(job)) is not JobStatus.COMPLETED  # publish_failed refusal
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"
    assert not staged.exists()  # staged temp retracted
    assert [c.status for c in calls] == [JobStatus.FAILED_RETRYABLE] or [
        c.status for c in calls
    ] == [JobStatus.FAILED_TERMINAL]

    # Protocol-level failure modes of the real publisher (queue path above):
    # (a) claim mismatch raises before the swap — destination untouched;
    publish_text_artifact(staged, "NEW GENERATION")
    with pytest.raises(RuntimeError):
        publish_pdf_text_artifact({"file_path": str(src)}, {"artifact_path": str(staged) + ".no"})
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"

    # (b) the swap itself explodes — destination untouched, staged cleaned.
    publish_text_artifact(staged, "NEW GENERATION")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("swap exploded")

    import nexus_ai_agent.jobs.feature_verification as fv

    original = fv.os.replace
    fv.os.replace = boom  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            publish_pdf_text_artifact({"file_path": str(src)}, {"artifact_path": str(staged)})
    finally:
        fv.os.replace = original  # type: ignore[assignment]
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"
    assert not staged.exists()  # staged temp cleaned


# ------------------------------------------------------------------ T11
def test_t11_crash_during_publication_recovers_correctly(tmp_path: Path) -> None:
    """T11: every crash window around the swap has a deterministic recovery.

    (i)   backup written, swap not journaled → duplicate backup removed,
          destination intact;
    (ii)  journaled swap, commit lost → pre-swap bytes restored (no
          uncommitted publication survives), inode-guarded;
    (iii) committed, backup not retired → committed bytes stand; a later
          recover without journal retires the stray backup.
    """
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\nfake\n")
    published = pdf_text_artifact_path(src)
    backup = pdf_text_backup_path(src)
    staged = pdf_text_staged_path(src)

    # (i) crash after backup, before swap/journal.
    publish_text_artifact(published, "OLD GENERATION")
    publish_text_artifact(staged, "CRASHED CANDIDATE")
    backup.unlink(missing_ok=True)
    import os as _os

    _os.link(published, backup)
    recover_pdf_text_publication({"file_path": str(src)}, None)
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"
    assert not backup.exists()

    # (ii) crash after journaled swap, before fenced commit.
    publish_text_artifact(staged, "CRASHED CANDIDATE")
    patch = publish_pdf_text_artifact({"file_path": str(src)}, {"artifact_path": str(staged)})
    assert published.read_text(encoding="utf-8") == "CRASHED CANDIDATE"
    prior = {PUBLICATION_JOURNAL_KEY: patch[PUBLICATION_JOURNAL_KEY]}
    recover_pdf_text_publication({"file_path": str(src)}, prior)
    assert published.read_text(encoding="utf-8") == "OLD GENERATION"

    # (iii) commit confirmed, retire crashed → bytes stand; stray retired later.
    publish_text_artifact(staged, "CRASHED CANDIDATE")
    patch = publish_pdf_text_artifact({"file_path": str(src)}, {"artifact_path": str(staged)})
    # fenced commit "confirmed" here (durable row would carry the journal):
    committed = {PUBLICATION_JOURNAL_KEY: patch[PUBLICATION_JOURNAL_KEY]}
    # retire crashed → backup orphan exists:
    assert backup.exists()
    # recovery after the commit: inode guard sees a live swap whose journal
    # says the swap IS the committed one — a NEW row's recover (no journal)
    # only retires the stray:
    recover_pdf_text_publication({"file_path": str(src)}, None)
    assert published.read_text(encoding="utf-8") == "CRASHED CANDIDATE"
    assert not backup.exists()
    retire_pdf_text_artifact({"file_path": str(src)}, committed)  # idempotent


# ------------------------------------------------------------------ T12/T13
@pytest.mark.asyncio
async def test_t12_t13_terminal_states_never_reopen(tmp_path: Path) -> None:
    """T12/T13: completed and failed_terminal never reopen — no transition
    out of them exists (matrix + fence both refuse)."""
    db = tmp_path / "q.sqlite3"
    q = InProcessJobQueue(db, {"t": _ok})
    job_done = _insert(q, "t12")
    tok = q._mark_processing(job_done)
    assert q._mark_completed(job_done, {"ok": True}, tok) is True

    job_dead = _insert(q, "t13")
    tok3 = q._mark_processing(job_dead)
    assert q._mark_failed(job_dead, "boom", JobStatus.FAILED_TERMINAL, None, tok3) is True

    stale = ExecutionToken(job_id=job_done, generation=0, owner_token="stale")
    for job in (job_done, job_dead):
        assert q._mark_pending(job, stale) is False
        assert q._mark_processing(job) is None
        assert q._mark_completed(job, {}, stale) is False
        assert q._mark_failed(job, "x", JobStatus.FAILED_RETRYABLE, None, stale) is False
    assert _row(db, job_done)[0] == JobStatus.COMPLETED.value
    assert _row(db, job_dead)[0] == JobStatus.FAILED_TERMINAL.value

    for source in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL, JobStatus.FAILED_RETRYABLE):
        with pytest.raises(ValueError):
            assert_transition(source, JobStatus.PENDING)
        with pytest.raises(ValueError):
            assert_transition(source, JobStatus.COMPLETED)


# ------------------------------------------------------------------ T14
@pytest.mark.asyncio
async def test_t14_failed_retryable_cannot_silently_retry(tmp_path: Path) -> None:
    """T14: the scheduler edge does not exist — a failed_retryable row is
    NEVER silently retried (resume ignores it, the matrix refuses the edge)."""
    db = tmp_path / "q.sqlite3"
    q1 = InProcessJobQueue(db, {"t": _ok})
    q2 = InProcessJobQueue(db, {"t": _ok})
    job = _insert(q1, "t14")
    tok = q1._mark_processing(job)
    assert q1._mark_failed(job, "transient", JobStatus.FAILED_RETRYABLE, None, tok) is True

    assert await q2.resume_pending() == []
    assert await q2.resume_pending_jobs() == []
    assert _row(db, job)[0] == JobStatus.FAILED_RETRYABLE.value
    with pytest.raises(ValueError):
        assert_transition(JobStatus.FAILED_RETRYABLE, JobStatus.PENDING)
