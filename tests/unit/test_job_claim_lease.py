"""Unit contract for P1 CLAIM + LEASE (domain + ClaimLeaseStore)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.job_claim_lease import ClaimLeaseStore
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.domain.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseClaimOutcome,
    LeaseMutationOutcome,
    compute_expiry,
    is_lease_expired,
    validate_lease_ttl_seconds,
    validate_owner_id,
)


def _seed_pending(db: Path, *, job_id: str = "job-1", job_type: str = "echo") -> None:
    """Insert a pending row the same way the queue would (no asyncio needed)."""
    # Opening InProcessJobQueue creates the table + P1 columns.
    InProcessJobQueue(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                job_type,
                f"idem-{job_id}",
                '{"value": 1}',
                JobStatus.PENDING.value,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()


def test_domain_validate_owner_and_ttl() -> None:
    assert validate_owner_id("  worker-a  ") == "worker-a"
    with pytest.raises(ValueError):
        validate_owner_id("   ")
    with pytest.raises(ValueError):
        validate_owner_id("bad\x00id")
    assert validate_lease_ttl_seconds(DEFAULT_LEASE_TTL_SECONDS) == DEFAULT_LEASE_TTL_SECONDS
    with pytest.raises(ValueError):
        validate_lease_ttl_seconds(0)
    with pytest.raises(TypeError):
        validate_lease_ttl_seconds(True)  # type: ignore[arg-type]


def test_domain_expiry_helpers() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    exp = compute_expiry(now, 30)
    assert exp == now + timedelta(seconds=30)
    assert is_lease_expired(None, now=now) is True  # legacy unfenced
    assert is_lease_expired(now - timedelta(seconds=1), now=now) is True
    assert is_lease_expired(now + timedelta(seconds=1), now=now) is False


def test_claim_job_wins_once(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db)
    store = ClaimLeaseStore(db)

    outcome, lease = store.claim_job("job-1", owner_id="worker-a", lease_ttl_seconds=30)
    assert outcome is LeaseClaimOutcome.CLAIMED
    assert lease is not None
    assert lease.owner_id == "worker-a"
    assert lease.lease_version == 1
    assert lease.lease_token
    assert lease.payload == {"value": 1}

    outcome2, lease2 = store.claim_job("job-1", owner_id="worker-b", lease_ttl_seconds=30)
    assert outcome2 is LeaseClaimOutcome.NOT_AVAILABLE
    assert lease2 is None

    snap = store.inspect_lease("job-1")
    assert snap is not None
    assert snap["owner_id"] == "worker-a"
    assert snap["lease_token"] == lease.lease_token


def test_claim_missing_and_terminal(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db, job_id="alive")
    store = ClaimLeaseStore(db)

    outcome, _ = store.claim_job("nope", owner_id="w")
    assert outcome is LeaseClaimOutcome.NOT_FOUND

    outcome, lease = store.claim_job("alive", owner_id="w")
    assert outcome is LeaseClaimOutcome.CLAIMED and lease is not None
    assert store.complete(
        "alive",
        owner_id="w",
        lease_token=lease.lease_token,
        lease_version=lease.lease_version,
        result={"ok": True},
    ) is LeaseMutationOutcome.OK

    outcome3, _ = store.claim_job("alive", owner_id="other")
    assert outcome3 is LeaseClaimOutcome.ALREADY_TERMINAL


def test_stale_lease_reclaim_bumps_version(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db)
    store = ClaimLeaseStore(db)
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)

    o1, lease1 = store.claim_job(
        "job-1", owner_id="worker-a", lease_ttl_seconds=10, now=past
    )
    assert o1 is LeaseClaimOutcome.CLAIMED and lease1 is not None
    assert lease1.lease_version == 1

    later = past + timedelta(seconds=11)
    o2, lease2 = store.claim_job(
        "job-1", owner_id="worker-b", lease_ttl_seconds=10, now=later
    )
    assert o2 is LeaseClaimOutcome.CLAIMED and lease2 is not None
    assert lease2.owner_id == "worker-b"
    assert lease2.lease_version == 2
    assert lease2.lease_token != lease1.lease_token


def test_fencing_rejects_stale_complete_and_heartbeat(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db)
    store = ClaimLeaseStore(db)
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)

    _, lease_a = store.claim_job(
        "job-1", owner_id="a", lease_ttl_seconds=5, now=past
    )
    assert lease_a is not None

    later = past + timedelta(seconds=6)
    _, lease_b = store.claim_job(
        "job-1", owner_id="b", lease_ttl_seconds=30, now=later
    )
    assert lease_b is not None

    # Stale holder A cannot complete or heartbeat after B reclaimed.
    assert (
        store.complete(
            "job-1",
            owner_id="a",
            lease_token=lease_a.lease_token,
            lease_version=lease_a.lease_version,
            result={"stolen": True},
        )
        is LeaseMutationOutcome.STALE_TOKEN
    )
    outcome, _exp = store.heartbeat(
        "job-1",
        owner_id="a",
        lease_token=lease_a.lease_token,
        lease_version=lease_a.lease_version,
        now=later,
    )
    assert outcome is LeaseMutationOutcome.STALE_TOKEN

    assert (
        store.complete(
            "job-1",
            owner_id="b",
            lease_token=lease_b.lease_token,
            lease_version=lease_b.lease_version,
            result={"ok": True},
            now=later,
        )
        is LeaseMutationOutcome.OK
    )
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            "SELECT status, result_json FROM nexus_job_queue WHERE id = ?", ("job-1",)
        ).fetchone()
    assert status[0] == JobStatus.COMPLETED.value
    assert "ok" in str(status[1])


def test_heartbeat_extends_live_lease(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db)
    store = ClaimLeaseStore(db)
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    _, lease = store.claim_job("job-1", owner_id="w", lease_ttl_seconds=10, now=t0)
    assert lease is not None
    t1 = t0 + timedelta(seconds=5)
    outcome, new_exp = store.heartbeat(
        "job-1",
        owner_id="w",
        lease_token=lease.lease_token,
        lease_version=lease.lease_version,
        lease_ttl_seconds=10,
        now=t1,
    )
    assert outcome is LeaseMutationOutcome.OK
    assert new_exp == t1 + timedelta(seconds=10)


def test_fail_and_release_fenced(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db, job_id="j-fail")
    _seed_pending(db, job_id="j-rel")
    store = ClaimLeaseStore(db)

    _, lf = store.claim_job("j-fail", owner_id="w")
    assert lf is not None
    assert (
        store.fail(
            "j-fail",
            owner_id="w",
            lease_token=lf.lease_token,
            lease_version=lf.lease_version,
            error="boom",
        )
        is LeaseMutationOutcome.OK
    )

    _, lr = store.claim_job("j-rel", owner_id="w")
    assert lr is not None
    assert (
        store.release_to_pending(
            "j-rel",
            owner_id="w",
            lease_token=lr.lease_token,
            lease_version=lr.lease_version,
        )
        is LeaseMutationOutcome.OK
    )
    snap = store.inspect_lease("j-rel")
    assert snap is not None
    assert snap["status"] == JobStatus.PENDING.value
    assert snap["owner_id"] is None


def test_claim_next_fifo_and_filter(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_pending(db, job_id="a", job_type="t1")
    _seed_pending(db, job_id="b", job_type="t2")
    _seed_pending(db, job_id="c", job_type="t1")
    # Force FIFO by created_at already identical — ORDER BY created_at, id
    store = ClaimLeaseStore(db)
    o, lease = store.claim_next(owner_id="w", job_type="t1")
    assert o is LeaseClaimOutcome.CLAIMED and lease is not None
    assert lease.job_id == "a"
    o2, lease2 = store.claim_next(owner_id="w", job_type="t1")
    assert o2 is LeaseClaimOutcome.CLAIMED and lease2 is not None
    assert lease2.job_id == "c"
    o3, _ = store.claim_next(owner_id="w", job_type="t1")
    assert o3 is LeaseClaimOutcome.NOT_AVAILABLE


def test_pre_p1_sidecar_upgrades_in_place(tmp_path: Path) -> None:
    """A queue DB created without lease columns still accepts ClaimLeaseStore."""
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
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
                finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, created_at)
            VALUES ('legacy-1', 'echo', 'k', '{}', 'pending', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()

    store = ClaimLeaseStore(db)
    outcome, lease = store.claim_job("legacy-1", owner_id="upgrader")
    assert outcome is LeaseClaimOutcome.CLAIMED and lease is not None
    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(nexus_job_queue)")}
    assert {"owner_id", "lease_token", "lease_version", "lease_expires_at"} <= cols


@pytest.mark.asyncio
async def test_existing_queue_enqueue_still_works_with_lease_columns(
    tmp_path: Path,
) -> None:
    """JobQueuePort surface is unchanged; P1 columns are additive only."""
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"echo": payload["v"]}

    queue.register_handler("echo", handler)
    job_id = await queue.enqueue(
        job_type="echo", idempotency_key="e-1", payload={"v": 9}
    )
    for _ in range(200):
        if await queue.get_status(job_id) == JobStatus.COMPLETED:
            break
        import asyncio

        await asyncio.sleep(0.01)
    assert await queue.get_status(job_id) == JobStatus.COMPLETED
    assert await queue.get_result(job_id) == {"echo": 9}
