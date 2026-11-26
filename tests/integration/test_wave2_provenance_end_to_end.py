"""Wave 2 end-to-end integration tests: CreativeGraph authority, outbox, and passports."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.spine.models import (
    CreativeGraph as SpineCreativeGraph,
)
from nexus_ai_agent.creative.spine.models import (
    Intent,
    RulesIntentResolver,
)
from nexus_ai_agent.jobs.creative_passport import attempt_id as creative_attempt_id
from nexus_ai_agent.jobs.verification import VerificationOutcome
from nexus_ai_agent.provenance.graph import (
    CreativeGraph as DurableCreativeGraph,
)
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.observer import QueueLedgerObserver
from nexus_ai_agent.provenance.outbox import OutboxReconciler
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus


def test_spine_graph_acts_as_planning_dto_over_durable_creative_graph(tmp_path: Path) -> None:
    durable = DurableCreativeGraph(tmp_path / "durable_graph.sqlite3")
    intent = RulesIntentResolver().resolve(
        "trim 5 seconds without dropping audio", project_id="proj-42"
    )
    assert isinstance(intent, Intent)

    planning = SpineCreativeGraph("proj-42")
    n_intent = planning.add_node("intent", label=intent.goal, data={"intent_id": intent.intent_id})
    n_ir = planning.add_node(
        "creative_ir",
        label="TimelineIR(trim=0..5s)",
        data={"ir_version": 1},
        parents=(n_intent.node_id,),
    )
    n_plan = planning.add_node(
        "compiled_plan",
        label="Plan(timeline.trim)",
        data={"operations": ["timeline.trim"]},
        parents=(n_ir.node_id,),
    )
    n_dag = planning.add_node(
        "command_dag",
        label="CommandDAG(1 step)",
        data={"commands": ["cmd_1"]},
        parents=(n_plan.node_id,),
    )

    plan_dto = planning.to_plan_graph(intent=intent)
    assert plan_dto.project_id == "proj-42"
    assert len(plan_dto.nodes) == 4
    assert plan_dto.plan_digest.startswith("sha256:")

    records = planning.to_provenance_records()
    assert len(records) == 4
    assert all(r.epistemic_status == "HYPOTHESIS" for r in records)

    id_map = planning.sync_to_durable_graph(durable)
    assert set(id_map) == {n_intent.node_id, n_ir.node_id, n_plan.node_id, n_dag.node_id}
    assert durable.verify_acyclic() is True
    assert durable.integrity_check().ok is True

    # Verify lineage traversal on the durable single authority.
    ancestors = durable.ancestors(id_map[n_dag.node_id])
    ancestor_ids = {item["node"]["node_id"] for item in ancestors}
    assert {id_map[n_intent.node_id], id_map[n_ir.node_id], id_map[n_plan.node_id]} <= ancestor_ids

    # Round-trip projection from durable authority back to planning DTO.
    projected = SpineCreativeGraph.from_durable_graph(durable, project_id="proj-42")
    assert len(projected.nodes()) == 4
    assert {n.kind for n in projected.nodes()} == {
        "intent",
        "creative_ir",
        "compiled_plan",
        "command_dag",
    }

    # Explicit record_planning_lineage API on DurableCreativeGraph.
    pipeline_ids = durable.record_planning_lineage(
        project_id="proj-42",
        intent_id="int-explicit",
        intent_payload={"goal": "grade warm"},
        creative_ir_id="ir-explicit",
        creative_ir_payload={"lut": "warm_v1"},
        compiled_plan_id="plan-explicit",
        compiled_plan_payload={"steps": ["color.grade"]},
        command_dag_id="dag-explicit",
        command_dag_payload={"nodes": ["cmd_grade"]},
    )
    assert len(pipeline_ids) == 4
    assert durable.verify_acyclic() is True


@pytest.mark.asyncio
async def test_wave2_end_to_end_queue_outbox_graph_and_passport_binding(tmp_path: Path) -> None:
    queue_db = tmp_path / "queue.sqlite3"
    journal_db = tmp_path / "journal.sqlite3"
    graph_db = tmp_path / "creative_graph.sqlite3"

    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"ftyp-wave2-rendered-video-bytes")
    art_sha = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()

    journal = CausalJournal(journal_db)
    observer = QueueLedgerObserver(journal)
    durable_graph = DurableCreativeGraph(graph_db)

    base_rev = "sha256:" + "1" * 64
    accepted_rev = "sha256:" + "2" * 64
    in_ver = "ver_in_" + "3" * 16
    out_ver = "ver_out_" + "4" * 16

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "artifact_path": str(artifact),
            "sha256": art_sha,
            "project_id": "proj-e2e",
            "base_revision_id": base_rev,
            "accepted_revision_id": accepted_rev,
            "input_asset_version_ids": [in_ver],
            "asset_version_id": out_ver,
            "artifact_binding": {
                "artifact_id": art_sha,
                "project_id": "proj-e2e",
                "revision_id": accepted_rev,
                "artifact_version_id": out_ver,
            },
        }

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(
            ok=True,
            reason_code=None,
            summary={
                "status": "verified",
                "physical_identity": {
                    "artifact_path": str(artifact),
                    "sha256": art_sha,
                    "size_bytes": artifact.stat().st_size,
                },
            },
        )

    queue = InProcessJobQueue(
        queue_db,
        handlers={"creative_render": handler},
        artifact_verifiers={"creative_render": verifier},
        causal_observer=observer,
    )
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key="wave2-e2e-1",
        payload={
            "idempotency_key": "wave2-e2e-1",
            "project_id": "proj-e2e",
            "revision_id": base_rev,
            "input_asset_version_ids": [in_ver],
            "user_id": 1001,
            "chat_id": 2002,
        },
    )
    for _ in range(100):
        if await queue.get_status(job_id) == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.02)

    assert await queue.get_status(job_id) == JobStatus.COMPLETED

    # Drain queue outbox into durable CreativeGraph.
    report = OutboxReconciler(queue.outbox_store(), durable_graph).reconcile_pending()
    assert report.failed == 0
    assert report.applied >= 2

    # Build ArtifactPassport and verify complete Wave 2 revision/asset/provenance binding.
    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.VERIFIED

    lb = passport.content["predicate"]["lineage_binding"]
    assert lb["project_id"] == "proj-e2e"
    assert lb["base_revision_id"] == base_rev
    assert lb["accepted_revision_id"] == accepted_rev
    assert lb["input_asset_version_ids"] == [in_ver]
    assert lb["output_asset_version_id"] == out_ver
    assert lb["job_id"] == job_id
    assert lb["attempt_id"] == creative_attempt_id(job_id, 1)
    assert lb["attempt_epoch"] == 1
    assert isinstance(lb["verification_proof_digest"], str)
    assert lb["verification_proof_digest"].startswith("sha256:")
    assert isinstance(lb["provenance_node_id"], str)

    # Confirm passport's provenance_node_id matches the reconciled ARTIFACT node.
    art_node = durable_graph.get_node(lb["provenance_node_id"])
    assert art_node is not None
    assert art_node.kind == "ARTIFACT"
    assert art_node.content_digest == art_sha
    assert art_node.payload["accepted_revision_id"] == accepted_rev
    assert durable_graph.integrity_check().ok is True

    await queue.shutdown()


def test_spine_graph_operations_and_passport_missing_facts_lineage_binding(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.spine.models import CompiledIntent, GraphError

    g = SpineCreativeGraph("proj-ops")
    assert g.project_id == "proj-ops"
    with pytest.raises(GraphError):
        g.add_node("intent", parents=("missing",))
    with pytest.raises(GraphError):
        g.node("missing")

    root = g.add_node("intent", label="root")
    left = g.add_node("creative_ir", parents=(root.node_id,))
    right = g.add_node("creative_ir", parents=(root.node_id,))
    join = g.add_node("compiled_plan", parents=(left.node_id, right.node_id))
    assert set(g.children(root.node_id)) == {left.node_id, right.node_id}
    assert set(g.parents(join.node_id)) == {left.node_id, right.node_id}
    assert set(g.descendants(root.node_id)) == {left.node_id, right.node_id, join.node_id}

    with pytest.raises(GraphError):
        g.remove_node(root.node_id)
    g.remove_node(join.node_id)
    g.remove_node(left.node_id)
    assert g.children(root.node_id) == (right.node_id,)

    compiled = CompiledIntent(intent_id="i1", operations=("op.a",))
    plan_dto = g.to_plan_graph(compiled_intent=compiled)
    assert plan_dto.compiled_intent == compiled

    # PassportBuilder on a job present in journal only (facts is None) + singular asset_version_id.
    journal = CausalJournal(tmp_path / "journal_only.sqlite3")
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id="journal-only-job",
            job_type="creative_render",
            idempotency_key="k",
            status="pending",
            occurred_at="2026-11-25T00:00:00+00:00",
        )
    )

    class _FactsStub:
        def get_job_facts(self, job_id: str) -> None:
            return None

    missing_pp = PassportBuilder(journal, _FactsStub(), sha256_fn=lambda _p: "0" * 64).build(
        "journal-only-job"
    )
    assert missing_pp.status is PassportStatus.INCOMPLETE
    assert missing_pp.content["predicate"]["lineage_binding"]["job_id"] == "journal-only-job"
    assert missing_pp.content["predicate"]["lineage_binding"]["project_id"] is None

    # Singular payload["asset_version_id"] + attempt_history fallback for attempt_id.
    from nexus_ai_agent.provenance.models import JobFacts

    class _SingularFactsStub:
        def get_job_facts(self, job_id: str) -> JobFacts:
            return JobFacts(
                job_id=job_id,
                job_type="creative_render",
                idempotency_key="k2",
                status="completed",
                attempt=1,
                payload={"asset_version_id": "ver-single", "project_id": "p-single"},
                payload_digest="sha256:" + "0" * 64,
                result={"artifact_path": "/nonexistent.mp4", "sha256": "sha256:" + "1" * 64},
                result_digest="sha256:" + "2" * 64,
                error=None,
                created_at="2026-11-25T00:00:00+00:00",
                started_at="2026-11-25T00:00:01+00:00",
                finished_at="2026-11-25T00:00:02+00:00",
                verification=None,
                attempt_history=[{"attempt": 1, "attempt_id": "att-from-history"}],
            )

    singular_pp = PassportBuilder(
        journal, _SingularFactsStub(), sha256_fn=lambda _p: "0" * 64
    ).build("singular-job")
    lb2 = singular_pp.content["predicate"]["lineage_binding"]
    assert lb2["input_asset_version_ids"] == ["ver-single"]
    assert lb2["attempt_id"] == "att-from-history"
