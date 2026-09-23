"""Concurrency proofs for P1 CLAIM + LEASE."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.job_claim_lease import ClaimLeaseStore
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.domain.lease import LeaseClaimOutcome, LeaseMutationOutcome


def _seed_many(db: Path, n: int) -> None:
    InProcessJobQueue(db)
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute(
                """
                INSERT INTO nexus_job_queue
                    (id, job_type, idempotency_key, payload_json, status, created_at)
                VALUES (?, 'echo', ?, '{}', 'pending', ?)
                """,
                (f"job-{i:04d}", f"idem-{i:04d}", f"2026-01-01T00:00:{i % 60:02d}+00:00"),
            )
        conn.commit()


def test_two_workers_one_job_exactly_one_winner(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_many(db, 1)
    barrier = threading.Barrier(2)
    results: list[tuple[LeaseClaimOutcome, object]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        store = ClaimLeaseStore(db)
        barrier.wait(timeout=5)
        outcome, lease = store.claim_job("job-0000", owner_id=name, lease_ttl_seconds=60)
        with lock:
            results.append((outcome, lease))

    threads = [
        threading.Thread(target=worker, args=("wa",)),
        threading.Thread(target=worker, args=("wb",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    outcomes = [o for o, _ in results]
    assert outcomes.count(LeaseClaimOutcome.CLAIMED) == 1
    assert outcomes.count(LeaseClaimOutcome.NOT_AVAILABLE) == 1

    winner_lease = next(lease for o, lease in results if o is LeaseClaimOutcome.CLAIMED)
    assert winner_lease is not None
    store = ClaimLeaseStore(db)
    assert (
        store.complete(
            "job-0000",
            owner_id=winner_lease.owner_id,
            lease_token=winner_lease.lease_token,
            lease_version=winner_lease.lease_version,
            result={"by": winner_lease.owner_id},
        )
        is LeaseMutationOutcome.OK
    )
    with sqlite3.connect(db) as conn:
        status, result = conn.execute(
            "SELECT status, result_json FROM nexus_job_queue WHERE id = 'job-0000'"
        ).fetchone()
    assert status == JobStatus.COMPLETED.value
    assert result is not None


def test_n_workers_n_jobs_no_double_claim(tmp_path: Path) -> None:
    n_jobs = 40
    n_workers = 8
    db = tmp_path / "jobs.sqlite3"
    _seed_many(db, n_jobs)
    claimed_ids: list[str] = []
    lock = threading.Lock()

    def drain(name: str) -> int:
        store = ClaimLeaseStore(db)
        won = 0
        while True:
            outcome, lease = store.claim_next(owner_id=name, lease_ttl_seconds=30)
            if outcome is not LeaseClaimOutcome.CLAIMED or lease is None:
                return won
            with lock:
                claimed_ids.append(lease.job_id)
            store.complete(
                lease.job_id,
                owner_id=name,
                lease_token=lease.lease_token,
                lease_version=lease.lease_version,
                result={"ok": True},
            )
            won += 1

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(drain, f"w{i}") for i in range(n_workers)]
        totals = [f.result(timeout=30) for f in as_completed(futures)]

    assert sum(totals) == n_jobs
    assert len(claimed_ids) == n_jobs
    assert len(set(claimed_ids)) == n_jobs  # no duplicates


def test_stale_holder_complete_rejected_after_reclaim(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_many(db, 1)
    store = ClaimLeaseStore(db)
    t0 = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)

    o1, lease_a = store.claim_job(
        "job-0000", owner_id="stale", lease_ttl_seconds=5, now=t0
    )
    assert o1 is LeaseClaimOutcome.CLAIMED and lease_a is not None

    # Simulate crash: no heartbeat. Another worker reclaims after expiry.
    t1 = t0 + timedelta(seconds=6)
    o2, lease_b = store.claim_job(
        "job-0000", owner_id="fresh", lease_ttl_seconds=30, now=t1
    )
    assert o2 is LeaseClaimOutcome.CLAIMED and lease_b is not None
    assert lease_b.lease_version == lease_a.lease_version + 1

    # Stale A wakes up and tries to finish — fencing rejects.
    assert (
        store.complete(
            "job-0000",
            owner_id="stale",
            lease_token=lease_a.lease_token,
            lease_version=lease_a.lease_version,
            result={"from": "stale"},
            now=t1 + timedelta(seconds=1),
        )
        is LeaseMutationOutcome.STALE_TOKEN
    )
    assert (
        store.complete(
            "job-0000",
            owner_id="fresh",
            lease_token=lease_b.lease_token,
            lease_version=lease_b.lease_version,
            result={"from": "fresh"},
            now=t1 + timedelta(seconds=1),
        )
        is LeaseMutationOutcome.OK
    )


def test_resume_pending_does_not_steal_live_lease(tmp_path: Path) -> None:
    """P1 invariant: live non-expired processing rows survive resume_pending."""
    import asyncio

    db = tmp_path / "jobs.sqlite3"
    _seed_many(db, 1)
    store = ClaimLeaseStore(db)
    # Use wall-clock now + long TTL so _reset_unfinished (which reads wall clock)
    # still sees the lease as live.
    o, lease = store.claim_job(
        "job-0000", owner_id="live-owner", lease_ttl_seconds=3600
    )
    assert o is LeaseClaimOutcome.CLAIMED and lease is not None

    queue = InProcessJobQueue(db)

    async def _run() -> list[str]:
        return await queue.resume_pending()

    requeued = asyncio.run(_run())
    assert "job-0000" not in requeued

    snap = store.inspect_lease("job-0000")
    assert snap is not None
    assert snap["status"] == JobStatus.PROCESSING.value
    assert snap["owner_id"] == "live-owner"
    assert snap["lease_token"] == lease.lease_token
