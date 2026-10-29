from __future__ import annotations

import pytest

from nexus_ai_agent.nagar.p0 import (
    CausalGraph,
    EvidenceKind,
    EvidenceRef,
    GraphEdge,
    GraphNode,
    IntentStatus,
    NodeKind,
    SemanticSegment,
    canonical_hash,
    compile_nel,
    invalidate_after_revision,
    resolve_intent,
    resolve_segment,
)


def _segment(**overrides: object) -> SemanticSegment:
    values: dict[str, object] = {
        "segment_id": "seg-intro",
        "project_id": "project-1",
        "tenant_id": "tenant-1",
        "asset_id": "asset-1",
        "timeline_revision": 3,
        "label": "intro",
        "start_pts": 0,
        "end_pts": 120,
        "timebase_num": 1,
        "timebase_den": 30,
        "evidence_refs": (
            EvidenceRef(
                evidence_id="detector:pause-1",
                project_id="project-1",
                tenant_id="tenant-1",
                timeline_revision=3,
                kind=EvidenceKind.DETERMINISTIC_CHECK,
                digest="sha256:" + "b" * 64,
                verified=True,
            ),
        ),
        "evidence_kind": EvidenceKind.DETERMINISTIC_CHECK,
        "confidence": 0.95,
        "uncertainty": 0.02,
        "source_model": "pause-detector.v1",
    }
    values.update(overrides)
    return SemanticSegment.model_validate(values)


def test_ambiguous_intent_never_becomes_commit_ready() -> None:
    result = resolve_intent(
        text="shorten the intro", project_id="project-1", ambiguities=("two intro candidates",)
    )
    assert result.status is IntentStatus.NEEDS_CLARIFICATION
    assert result.intent is not None and result.intent.requires_confirmation


def test_nel_compiles_to_allowlisted_typed_ir_and_stable_hash() -> None:
    source = """NEL 1
PROJECT project-1
INTENT "tighten_intro"
SELECT semantic_segment("intro")
TRIM duration_delta=-2.0s
PRESERVE speech_continuity=true
REQUIRE evidence("pause_detector.v1")
PREVIEW
"""
    result = compile_nel(source)
    assert result.diagnostics == ()
    assert result.ir is not None
    assert result.ir.operation == "timeline.trim"
    assert result.ir.duration_delta_us == -2_000_000
    assert result.canonical == compile_nel(source).canonical
    assert canonical_hash(result.ast)  # type: ignore[arg-type]


def test_nel_rejects_unknown_statement_and_does_not_fallback() -> None:
    result = compile_nel("NEL 1\nPROJECT p\nRUN shell('rm -rf /')")
    assert result.ast is None
    assert any(item.code == "syntax_error" for item in result.diagnostics)


def test_semantic_time_requires_exact_revision_and_tenant() -> None:
    candidate = _segment()
    resolved = resolve_segment(
        label="intro",
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        candidates=[candidate],
    )
    assert resolved.selected == candidate
    stale = resolve_segment(
        label="intro",
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=4,
        candidates=[candidate],
    )
    assert stale.clarification_required and stale.reason == "segment_not_found"
    cross_tenant = resolve_segment(
        label="intro",
        project_id="project-1",
        tenant_id="tenant-2",
        timeline_revision=3,
        candidates=[candidate],
    )
    assert cross_tenant.clarification_required


def test_revision_change_invalidates_segment() -> None:
    assert invalidate_after_revision(_segment(), 3).valid
    assert not invalidate_after_revision(_segment(), 4).valid


def test_causal_graph_rejects_cross_scope_edge_and_verified_without_evidence() -> None:
    graph = CausalGraph()
    intent = GraphNode(
        node_id="intent-1",
        kind=NodeKind.USER_INTENT,
        project_id="p",
        tenant_id="t",
        provenance="human",
    )
    operation = GraphNode(
        node_id="op-1",
        kind=NodeKind.OPERATION,
        project_id="p",
        tenant_id="t",
        provenance="compiler",
    )
    graph.add_node(intent)
    graph.add_node(operation)
    graph.add_edge(
        GraphEdge(
            edge_id="edge-1",
            relation="targets",
            source_id="intent-1",
            target_id="op-1",
            project_id="p",
            tenant_id="t",
        )
    )
    assert graph.compensation_targets(intent_id="intent-1", tenant_id="t") == (operation,)
    with pytest.raises(ValueError, match="verified_receipt"):
        graph.receipt(
            intent_id="intent-1",
            operation_id="op-1",
            job_id="job",
            artifact_id=None,
            verification_id=None,
            evidence_refs=(),
            status="VERIFIED_SUCCESS",
        )
        with pytest.raises(PermissionError, match="cross_scope"):
            graph.add_edge(
                GraphEdge(
                    edge_id="edge-2",
                    relation="targets",
                    source_id="intent-1",
                    target_id="op-1",
                    project_id="p",
                    tenant_id="other",
                )
            )


def test_verified_receipt_requires_explicit_present_artifact_status() -> None:
    graph = CausalGraph()
    with pytest.raises(ValueError, match="verified_receipt_evidence"):
        graph.receipt(
            intent_id="intent-1",
            operation_id="op-1",
            job_id="job",
            artifact_id="artifact",
            verification_id="verification",
            evidence_refs=("evidence",),
            status="VERIFIED_SUCCESS",
            artifact_evidence={"sha256": "sha256:" + "a" * 64},
            verifier_evidence={"status": "verified", "sha256": "sha256:" + "a" * 64},
        )
