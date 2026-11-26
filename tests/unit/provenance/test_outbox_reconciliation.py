"""Unit tests for ProvenanceOutboxStore, OutboxReconciler, and CreativeGraph Wave 2 APIs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.provenance.graph import (
    CreativeGraph,
    EpistemicProofError,
    EpistemicStatus,
    create_epistemic_proof,
    node_identity,
)
from nexus_ai_agent.provenance.outbox import (
    OUTBOX_EVENT_ATTEMPT_RESERVED,
    OUTBOX_EVENT_ATTEMPT_SUPERSEDED,
    OUTBOX_EVENT_JOB_COMPLETED,
    OUTBOX_EVENT_JOB_FAILED,
    OutboxReconciler,
    ProvenanceOutboxStore,
    make_outbox_id,
)

_KEYS = {"k1": b"wave2-unit-test-secret-key-32bytes"}


def test_outbox_reconciler_crash_replay_and_idempotency(tmp_path: Path) -> None:
    queue_db = tmp_path / "queue.sqlite3"
    graph_db = tmp_path / "graph.sqlite3"

    outbox = ProvenanceOutboxStore(queue_db)
    graph = CreativeGraph(graph_db, proof_verification_keys=_KEYS)
    reconciler = OutboxReconciler(outbox, graph)

    # Enqueue milestone events into the outbox (simulating queue commit before crash).
    art_digest = "sha256:" + "a" * 64
    proof_digest = "sha256:" + "b" * 64
    expected_art_id = node_identity(
        "ARTIFACT",
        {"job_id": "job-1", "attempt_epoch": 2, "content_digest": art_digest},
    )
    proof = create_epistemic_proof(
        secret_key=_KEYS["k1"],
        key_id="k1",
        subject_id=expected_art_id,
        subject_digest=art_digest,
        verifier_id="wave2-verifier",
        evidence_digest=proof_digest,
    )

    id1 = outbox.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
        job_id="job-1",
        attempt_id="att-1",
        attempt_epoch=1,
        payload={"job_type": "creative_render", "project_id": "proj-1", "revision_id": "rev-1"},
        created_at="2026-11-25T10:00:00+00:00",
    )
    # Re-enqueueing same event is idempotent.
    assert (
        outbox.enqueue(
            event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
            job_id="job-1",
            attempt_id="att-1",
            attempt_epoch=1,
            payload={"job_type": "creative_render", "project_id": "proj-1", "revision_id": "rev-1"},
            created_at="2026-11-25T10:00:00+00:00",
        )
        == id1
    )
    outbox.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_SUPERSEDED,
        job_id="job-1",
        attempt_id="att-1",
        attempt_epoch=1,
        payload={"superseded_by": "att-2"},
        created_at="2026-11-25T10:00:01+00:00",
    )
    outbox.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
        job_id="job-1",
        attempt_id="att-2",
        attempt_epoch=2,
        payload={"job_type": "creative_render", "project_id": "proj-1", "revision_id": "rev-1"},
        created_at="2026-11-25T10:00:02+00:00",
    )
    outbox.enqueue(
        event_type=OUTBOX_EVENT_JOB_COMPLETED,
        job_id="job-1",
        attempt_id="att-2",
        attempt_epoch=2,
        payload={
            "job_type": "creative_render",
            "project_id": "proj-1",
            "base_revision_id": "rev-1",
            "accepted_revision_id": "rev-2",
            "input_asset_version_ids": ["ver-in-1"],
            "output_asset_version_id": "ver-out-2",
            "artifact_digest": art_digest,
            "verification_proof_digest": proof_digest,
            "verification_proof": proof.to_dict(),
        },
        created_at="2026-11-25T10:00:03+00:00",
    )

    # Crash simulation: before reconciler runs, graph is empty and 4 events are PENDING.
    assert len(outbox.list_pending()) == 4
    assert graph.counts()["nodes"] == 0

    report = reconciler.reconcile_pending(limit=100)
    assert report.scanned == 4
    assert report.applied == 4
    assert report.failed == 0
    assert outbox.list_pending() == []

    # Verify durable graph state.
    att1_node = graph.get_node(node_identity("EXECUTION", {"job_id": "job-1", "attempt_epoch": 1}))
    att2_node = graph.get_node(node_identity("EXECUTION", {"job_id": "job-1", "attempt_epoch": 2}))
    art_node = graph.get_node(
        node_identity(
            "ARTIFACT",
            {"job_id": "job-1", "attempt_epoch": 2, "content_digest": art_digest},
        )
    )
    assert att1_node is not None and att1_node.status == "superseded"
    assert att2_node is not None and att2_node.status == "active"
    assert art_node is not None
    assert art_node.epistemic_status is EpistemicStatus.VERIFIED_COMMIT
    assert graph.integrity_check().ok is True

    # Idempotency: second run does nothing.
    second = reconciler.reconcile_pending()
    assert second.scanned == 0
    assert second.applied == 0

    # Simulate crash AFTER graph write and BEFORE mark_applied by flipping one row back to PENDING.
    completed_id = make_outbox_id("job-1", 2, OUTBOX_EVENT_JOB_COMPLETED)
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            "UPDATE provenance_outbox SET status = 'PENDING' WHERE outbox_id = ?", (completed_id,)
        )
    replay = reconciler.reconcile_pending()
    assert replay.scanned == 1
    assert replay.applied == 1
    assert replay.failed == 0
    assert graph.integrity_check().ok is True


def test_outbox_reconciler_rejects_tampered_payload_fail_closed(tmp_path: Path) -> None:
    queue_db = tmp_path / "tamper_q.sqlite3"
    graph_db = tmp_path / "tamper_g.sqlite3"
    outbox = ProvenanceOutboxStore(queue_db)
    graph = CreativeGraph(graph_db, proof_verification_keys=_KEYS)
    reconciler = OutboxReconciler(outbox, graph)

    oid = outbox.enqueue(
        event_type=OUTBOX_EVENT_JOB_FAILED,
        job_id="job-tamper",
        attempt_id="att-1",
        attempt_epoch=1,
        payload={"error": "real_error"},
    )
    # Tamper payload_json directly in SQLite without updating payload_digest.
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            "UPDATE provenance_outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps({"error": "forged_error"}), oid),
        )

    report = reconciler.reconcile_pending()
    assert report.scanned == 1
    assert report.applied == 0
    assert report.failed == 1
    rows = outbox.list_all()
    assert rows[0].status == "FAILED_DEAD"
    assert rows[0].last_error is not None
    assert graph.counts()["nodes"] == 0


def test_commit_verified_revision_and_invalidate_downstream(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "verify_inv.sqlite3", proof_verification_keys=_KEYS)
    rev = graph.put_node(
        kind="REVISION",
        identity={"revision_id": "rev-base"},
        payload={"project_id": "proj-1"},
        logical_entity_id="revision:rev-base",
        revision_id="rev-base",
    )
    art = graph.put_node(
        kind="ARTIFACT",
        identity={"artifact_id": "art-1"},
        payload={"project_id": "proj-1"},
        logical_entity_id="artifact:art-1",
        revision_id="rev-1",
        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
        evidence_digest="sha256:" + "c" * 64,
    )
    assert rev.node is not None and art.node is not None
    graph.put_edge(
        relation="EXECUTION_PRODUCED_ARTIFACT",
        src=rev.node.node_id,
        dst=art.node.node_id,
        payload={"project_id": "proj-1"},
    )

    # Forged signature fails closed.
    bad_proof = create_epistemic_proof(
        secret_key=b"wrong-key-not-in-ring-at-all-32b",
        key_id="k1",
        subject_id=art.node.node_id,
        subject_digest=art.node.payload_digest,
        verifier_id="evil",
        evidence_digest="sha256:" + "c" * 64,
    )
    with pytest.raises(EpistemicProofError):
        graph.commit_verified_revision(node_id=art.node.node_id, verification_proof=bad_proof)

    # Valid proof promotes to VERIFIED_COMMIT.
    good_proof = create_epistemic_proof(
        secret_key=_KEYS["k1"],
        key_id="k1",
        subject_id=art.node.node_id,
        subject_digest=art.node.payload_digest,
        verifier_id="verifier",
        evidence_digest="sha256:" + "c" * 64,
    )
    promoted = graph.commit_verified_revision(
        node_id=art.node.node_id, verification_proof=good_proof
    )
    assert promoted.epistemic_status is EpistemicStatus.VERIFIED_COMMIT

    # Invalidating upstream revision propagates downstream to the artifact.
    impact = graph.invalidate_downstream(rev.node.node_id, reason="source_changed")
    assert art.node.node_id in impact.impacted_node_ids
    art_after = graph.get_node(art.node.node_id)
    assert art_after is not None and art_after.status == "invalidated"
    assert graph.integrity_check().ok is True


@pytest.mark.asyncio
async def test_queue_writes_outbox_in_same_sqlite_transaction(tmp_path: Path) -> None:
    queue_db = tmp_path / "integrated_q.sqlite3"
    graph_db = tmp_path / "integrated_g.sqlite3"
    queue = InProcessJobQueue(queue_db, artifact_verifiers={})
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="outbox-tx-1",
        payload={"prompt": "test", "idempotency_key": "outbox-tx-1"},
    )
    claim = queue._mark_processing(job_id)
    assert claim is not None
    assert queue._mark_completed(claim, {"sha256": "sha256:" + "d" * 64}) is True

    outbox = queue.outbox_store()
    pending = outbox.list_pending()
    assert [ev.event_type for ev in pending] == [
        OUTBOX_EVENT_ATTEMPT_RESERVED,
        OUTBOX_EVENT_JOB_COMPLETED,
    ]

    graph = CreativeGraph(graph_db, proof_verification_keys=_KEYS)
    report = OutboxReconciler(outbox, graph).reconcile_pending()
    assert report.applied == 2
    assert report.failed == 0
    assert graph.integrity_check().ok is True


def test_outbox_store_validation_memory_and_error_paths(tmp_path: Path) -> None:
    from nexus_ai_agent.provenance.outbox import (
        OUTBOX_EVENT_ATTEMPT_VERIFIED,
        insert_outbox_event_tx,
    )

    with pytest.raises(ValueError):
        ProvenanceOutboxStore()

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row
    mem_store = ProvenanceOutboxStore(memory_connection=mem_conn)
    with pytest.raises(ValueError):
        mem_store.list_pending(limit=0)
    with pytest.raises(ValueError):
        mem_store.enqueue(
            event_type="",
            job_id="j",
            attempt_id="a",
            attempt_epoch=1,
            payload={},
        )
    with pytest.raises(ValueError):
        mem_store.enqueue(
            event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
            job_id="j",
            attempt_id="a",
            attempt_epoch=-1,
            payload={},
        )

    # Simulate rollback on exception in memory connection context manager.
    with pytest.raises(RuntimeError), mem_store._connection(write=True) as conn:
        insert_outbox_event_tx(
            conn,
            event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
            job_id="j-rb",
            attempt_id="a-1",
            attempt_epoch=1,
            payload={},
        )
        raise RuntimeError("abort")
    assert mem_store.list_all() == []

    # Verified + failed events + terminal-node replay + non-object payload + already_applied.
    graph = CreativeGraph(tmp_path / "g_mem.sqlite3", proof_verification_keys=_KEYS)
    reconciler = OutboxReconciler(mem_store, graph)
    ev_digest = "sha256:" + "e" * 64
    mem_store.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
        job_id="j-fail",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={"evidence_digest": ev_digest},
    )
    mem_store.enqueue(
        event_type=OUTBOX_EVENT_JOB_FAILED,
        job_id="j-fail",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={"error": "boom"},
    )
    mem_store.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_VERIFIED,
        job_id="j-ver",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={"artifact_digest": ev_digest, "evidence_digest": ev_digest},
    )
    assert reconciler.reconcile_pending().applied == 3

    # Replay terminal/superseded events (including terminal job & attempt nodes).
    job_nid = node_identity("COMMAND", {"job_id": "j-fail"})
    graph.set_status(job_nid, "invalidated")
    with mem_conn:
        mem_conn.execute("UPDATE provenance_outbox SET status = 'PENDING'")
    replay = reconciler.reconcile_pending()
    assert replay.applied == 3

    # Non-dict JSON payload triggers dead-letter.
    bad_oid = mem_store.enqueue(
        event_type=OUTBOX_EVENT_JOB_FAILED,
        job_id="j-badjson",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={"ok": True},
    )
    with mem_conn:
        mem_conn.execute(
            "UPDATE provenance_outbox SET payload_json = '[]' WHERE outbox_id = ?",
            (bad_oid,),
        )
    bad_report = reconciler.reconcile_pending()
    assert bad_report.failed == 1
    assert mem_store.mark_applied(bad_oid) is False

    # Direct disk rollback + read-only list_pending + already_applied
    # + terminal supersede + no-digest complete.
    disk_store = ProvenanceOutboxStore(tmp_path / "disk_rb.sqlite3")
    with pytest.raises(RuntimeError), disk_store._connection(write=True) as conn:
        insert_outbox_event_tx(
            conn,
            event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
            job_id="j-disk-rb",
            attempt_id="a-1",
            attempt_epoch=1,
            payload={},
        )
        raise RuntimeError("disk abort")
    assert disk_store.list_all() == []
    assert disk_store.list_pending() == []
    assert disk_store.record_failure("missing", error="none") is False

    # Terminal supersede + JOB_COMPLETED without artifact_digest + already_applied race.
    from nexus_ai_agent.provenance.outbox import (
        OUTBOX_EVENT_ATTEMPT_SUPERSEDED,
        OUTBOX_EVENT_JOB_COMPLETED,
    )

    sup_id = mem_store.enqueue(
        event_type=OUTBOX_EVENT_ATTEMPT_SUPERSEDED,
        job_id="j-fail",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={"superseded_by": "a-2"},
    )
    comp_no_dig = mem_store.enqueue(
        event_type=OUTBOX_EVENT_JOB_COMPLETED,
        job_id="j-nodig",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={},
    )
    assert sup_id and comp_no_dig
    extra_rep = reconciler.reconcile_pending()
    assert extra_rep.applied == 2

    # Concurrent mark_applied race during reconcile_pending -> already_applied == 1.
    race_oid = mem_store.enqueue(
        event_type=OUTBOX_EVENT_JOB_COMPLETED,
        job_id="j-race",
        attempt_id="a-1",
        attempt_epoch=1,
        payload={},
    )
    orig_mark = mem_store.mark_applied

    def _raced_mark(outbox_id: str, *, applied_at: str | None = None) -> bool:
        if outbox_id == race_oid:
            orig_mark(outbox_id, applied_at=applied_at)
            return False
        return orig_mark(outbox_id, applied_at=applied_at)

    mem_store.mark_applied = _raced_mark  # type: ignore[method-assign]
    try:
        race_rep = reconciler.reconcile_pending()
        assert race_rep.already_applied == 1
    finally:
        mem_store.mark_applied = orig_mark  # type: ignore[method-assign]


def test_graph_commit_verified_revision_type_guard_and_revoked_project_filter(
    tmp_path: Path,
) -> None:
    graph = CreativeGraph(tmp_path / "guards.sqlite3", proof_verification_keys=_KEYS)
    with pytest.raises(EpistemicProofError):
        graph.commit_verified_revision(node_id="x", verification_proof="not-a-proof")  # type: ignore[arg-type]

    n1 = graph.put_node(
        kind="INTENT",
        identity={"project_id": "p-filter", "id": "1"},
        payload={"project_id": "p-filter"},
        logical_entity_id="intent:p-filter:1",
        revision_id="1",
    )
    n2 = graph.put_node(
        kind="INTENT",
        identity={"project_id": "p-other", "id": "2"},
        payload={"project_id": "p-other"},
        logical_entity_id="intent:p-other:2",
        revision_id="2",
    )
    assert n1.node is not None and n2.node is not None
    graph.set_status(n1.node.node_id, "revoked")
    assert graph.nodes_for_project("p-filter") == []
    assert [n.node_id for n in graph.nodes_for_project("p-other")] == [n2.node.node_id]
