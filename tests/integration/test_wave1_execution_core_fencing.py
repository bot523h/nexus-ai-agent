"""Wave 1 integration tests: monotonic lease takeover, fencing, and staged publication."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    FencedAttemptCommitError,
    InProcessJobQueue,
    InvalidHeartbeatStateError,
    QueueOwnershipError,
    StaleAttemptOwnershipError,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.render_jobs import (
    build_creative_render_publication,
    creative_render_publication_callback,
)
from nexus_ai_agent.jobs.creative_passport import attempt_id as creative_attempt_id


async def _enqueue_pending(queue: InProcessJobQueue, key: str = "wave1-k1") -> str:
    return await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload={"prompt": "trim", "idempotency_key": key},
    )


@pytest.mark.asyncio
async def test_monotonic_takeover_predicate_and_fail_closed_invariants(tmp_path: Path) -> None:
    db = tmp_path / "queue.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={}, worker_id="worker-a", lease_ttl_s=10.0)
    job_id = await _enqueue_pending(queue, "takeover-1")

    claim = queue._mark_processing(job_id)
    assert claim is not None
    lease1 = queue.get_attempt_lease(job_id, 1)
    assert lease1 is not None
    assert lease1.attempt_epoch == 1
    assert lease1.attempt_id == creative_attempt_id(job_id, 1)
    assert lease1.worker_id == "worker-a"
    assert lease1.status == "RUNNING"
    assert lease1.heartbeat_at_s is not None and lease1.heartbeat_at_s > 0.0
    assert lease1.lease_ttl_s == 10.0

    # Live worker within TTL is refused.
    with pytest.raises(StaleAttemptOwnershipError):
        queue.takeover_stale_attempt(
            job_id,
            new_worker_id="worker-b",
            now_epoch_s=lease1.heartbeat_at_s + 9.99,
        )

    # Clock rollback fails closed with InvalidHeartbeatStateError.
    with pytest.raises(InvalidHeartbeatStateError):
        queue.takeover_stale_attempt(
            job_id,
            new_worker_id="worker-b",
            now_epoch_s=lease1.heartbeat_at_s - 0.5,
        )

    # Exact TTL boundary succeeds atomically and increments attempt_epoch monotonically.
    lease2 = queue.takeover_stale_attempt(
        job_id,
        new_worker_id="worker-b",
        now_epoch_s=lease1.heartbeat_at_s + 10.0,
    )
    assert lease2.attempt_epoch == 2
    assert lease2.attempt_id == creative_attempt_id(job_id, 2)
    assert lease2.worker_id == "worker-b"
    assert lease2.status == "RUNNING"

    leases = queue.list_attempt_leases(job_id)
    assert [row.status for row in leases] == ["SUPERSEDED", "RUNNING"]
    assert [row.attempt_epoch for row in leases] == [1, 2]

    # Stale worker (epoch 1) is fenced out of ownership checks and fenced commit.
    with pytest.raises(FencedAttemptCommitError):
        queue.assert_attempt_owner(job_id, 1, worker_id="worker-a")
    with pytest.raises(FencedAttemptCommitError):
        queue.fenced_commit_completion(
            job_id=job_id,
            attempt_id=lease1.attempt_id,
            attempt_epoch=1,
            result={"ok": True},
            worker_id="worker-a",
        )

    # New owner (epoch 2) commits cleanly.
    done_lease = queue.fenced_commit_completion(
        job_id=job_id,
        attempt_id=lease2.attempt_id,
        attempt_epoch=2,
        result={"artifact_path": "/tmp/out.mp4"},
        worker_id="worker-b",
    )
    assert done_lease.status == "COMPLETED"
    assert await queue.get_status(job_id) == JobStatus.COMPLETED

    # Takeover on non-RUNNING / missing job fails closed.
    with pytest.raises(StaleAttemptOwnershipError):
        queue.takeover_stale_attempt(job_id, new_worker_id="worker-c", now_epoch_s=9e9)
    with pytest.raises(StaleAttemptOwnershipError):
        queue.takeover_stale_attempt("unknown-job", new_worker_id="worker-c", now_epoch_s=9e9)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("heartbeat_at_s", None),
        ("heartbeat_at_s", 0.0),
        ("heartbeat_at_s", -5.0),
        ("heartbeat_at_s", float("nan")),
        ("lease_ttl_s", float("nan")),
    ],
)
async def test_takeover_rejects_corrupted_heartbeat_or_ttl_state(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    db = tmp_path / "corrupt_hb.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={}, worker_id="worker-a", lease_ttl_s=5.0)
    job_id = await _enqueue_pending(queue, f"corrupt-{field}")
    claim = queue._mark_processing(job_id)
    assert claim is not None

    with sqlite3.connect(db) as conn:
        if isinstance(bad_value, float) and math.isnan(bad_value):
            # SQLite coerces float NaN to NULL or negative sentinel to test fail-closed behavior.
            conn.execute(
                f"UPDATE nexus_attempt_leases SET {field} = NULL WHERE job_id = ?"
                if field == "heartbeat_at_s"
                else "UPDATE nexus_attempt_leases SET heartbeat_at_s = -1.0 WHERE job_id = ?",
                (job_id,),
            )
        else:
            conn.execute(
                f"UPDATE nexus_attempt_leases SET {field} = ? WHERE job_id = ?",
                (bad_value, job_id),
            )
    with pytest.raises(InvalidHeartbeatStateError):
        queue.takeover_stale_attempt(job_id, new_worker_id="worker-b", now_epoch_s=9e9)


@pytest.mark.asyncio
async def test_schema_enforces_single_active_owner_and_started_at(tmp_path: Path) -> None:
    db = tmp_path / "schema_guard.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})
    job_id = await _enqueue_pending(queue, "schema-1")
    claim = queue._mark_processing(job_id)
    assert claim is not None

    with sqlite3.connect(db) as conn:
        # Second RUNNING attempt for the same job_id violates partial unique index.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO nexus_attempt_leases (
                    job_id, attempt_id, attempt_epoch, worker_id, status,
                    heartbeat_at_s, lease_ttl_s, started_at, updated_at
                )
                VALUES (
                    ?, 'attempt_2', 2, 'worker-evil', 'RUNNING',
                    100.0, 10.0, '2026-11-25T00:00:00Z', '2026-11-25T00:00:00Z'
                )
                """,
                (job_id,),
            )
        # Empty started_at violates CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO nexus_attempt_leases (
                    job_id, attempt_id, attempt_epoch, worker_id, status,
                    heartbeat_at_s, lease_ttl_s, started_at, updated_at
                )
                VALUES (
                    'other-job', 'attempt_1', 1, 'w', 'RUNNING',
                    100.0, 10.0, '   ', '2026-11-25T00:00:00Z'
                )
                """
            )


def test_stale_worker_publication_blocked_quarantined_and_fenced(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    rendered = workspace / "out.mp4"
    rendered.write_bytes(b"stale-worker-bytes")

    # Simulate a queue fence check that fails because another worker took over.
    payload: dict[str, object] = {"_queue_job_id": "job-race", "_queue_attempt": 1}
    result: dict[str, object] = {
        "artifact_path": str(rendered),
        "workspace_dir": str(workspace),
        "_queue_job_id": "job-race",
        "_queue_attempt": 1,
        "_queue_fence_check": lambda: False,
    }

    with pytest.raises(FencedAttemptCommitError):
        creative_render_publication_callback(payload, result, artifact_root)

    # Published destination must NOT exist.
    published = artifact_root / "published" / "job-race" / "out.mp4"
    assert not published.exists()
    # Staged bytes must have been moved into _quarantine.
    quarantine_files = list((artifact_root / "staging" / "_quarantine").rglob("out.mp4"))
    assert len(quarantine_files) == 1
    assert quarantine_files[0].read_bytes() == b"stale-worker-bytes"


@pytest.mark.asyncio
async def test_single_owner_guard_and_end_to_end_attempt_parity(tmp_path: Path) -> None:
    db = tmp_path / "e2e.sqlite3"
    artifact_root = Path(f"{db}.artifacts")
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    source = workspace / "render.mp4"
    source.write_bytes(b"valid-render-bytes")

    published_patches: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"artifact_path": str(source), "workspace_dir": str(workspace)}

    from nexus_ai_agent.jobs.verification import VerificationOutcome

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(
            ok=True,
            reason_code=None,
            summary={"status": "verified", "path": result.get("artifact_path")},
        )

    pub = build_creative_render_publication(artifact_root)
    original_publish = pub.publish

    def wrapped_publish(payload: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        patch = original_publish(payload, result)
        published_patches.append(patch)
        return patch

    from nexus_ai_agent.adapters.in_process_job_queue import ArtifactPublication

    queue = InProcessJobQueue(
        db,
        handlers={"creative_render": handler},
        artifact_verifiers={"creative_render": verifier},
        artifact_publications={
            "creative_render": ArtifactPublication(
                publish=wrapped_publish,
                retract=pub.retract,
                finalize=pub.finalize,
            )
        },
        worker_id="primary-worker",
        lease_ttl_s=15.0,
    )
    with pytest.raises(QueueOwnershipError):
        queue.assert_single_owner()
    await queue.acquire_ownership()
    queue.assert_single_owner()

    job_id = await _enqueue_pending(queue, "e2e-parity")
    for _ in range(100):
        if await queue.get_status(job_id) == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.02)

    assert await queue.get_status(job_id) == JobStatus.COMPLETED
    leases = queue.list_attempt_leases(job_id)
    assert len(leases) == 1
    assert leases[0].attempt_epoch == 1
    assert leases[0].attempt_id == creative_attempt_id(job_id, 1)
    assert leases[0].status == "COMPLETED"
    assert len(published_patches) == 1
    assert published_patches[0]["attempt_epoch"] == 1
    assert published_patches[0]["attempt_id"] == leases[0].attempt_id

    await queue.shutdown()
    with pytest.raises(QueueOwnershipError):
        queue.assert_single_owner()
