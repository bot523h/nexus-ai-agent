"""Comprehensive unit, property, and adversarial tests for Durable Creative-Graph (R28).

Exercises every Stage 2-A invariant in ``src/nexus_ai_agent/provenance/graph.py``:
* formal ontology separation (``logical_entity_id``, ``revision_id``, ``content_digest``);
* strict DAG acyclicity (fail-closed rejection of self-loops and multi-hop cycles);
* deterministic Lexicographic Kahn topological ordering across random insertion orders;
* RFC 8785 JCS canonical serialization with exact ``int``/``Fraction`` and float refusal;
* counterfactual causal impact propagation (``compute_causal_impact_set`` and atomic apply);
* epistemic proof gates (``HYPOTHESIS``, ``MEASURED_EVIDENCE``, ``VERIFIED_COMMIT``);
* tamper-evident hash chain, restart verification, sealed atomic snapshots, backup/restore,
  journal rebuild, concurrency, and SQL-injection refusal (zero SQL interpolation).
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import threading
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.provenance import (
    GRAPH_CHAIN_GENESIS,
    CanonicalValueError,
    CausalEvent,
    CausalJournal,
    CreativeGraph,
    EntityIdentityTriple,
    EpistemicProof,
    EpistemicProofError,
    EpistemicStatus,
    EventKind,
    GraphCycleError,
    GraphIntegrityError,
    GraphStateError,
    LedgerRecord,
    canonical_graph_digest,
    canonical_graph_json,
    compute_proof_signature,
    create_epistemic_proof,
    edge_identity,
    node_identity,
    normalize_canonical_value,
    validate_sha256_digest,
    verify_epistemic_proof,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _build_vertical_slice(graph: CreativeGraph) -> dict[str, str]:
    """Create a complete PROJECT -> ... -> ARTIFACT + attachments vertical slice."""
    ids: dict[str, str] = {}
    project = graph.put_node(
        "PROJECT",
        {"project_id": "proj-alpha"},
        {"title": "Alpha Cinema"},
        logical_entity_id="project:alpha",
        revision_id="rev:1",
        content_digest=_sha256(b"project-alpha-v1"),
    ).node
    assert project is not None
    ids["PROJECT"] = project.node_id

    intent = graph.put_node(
        "INTENT",
        {"intent_id": "intent-1"},
        {"fps": Fraction(24000, 1001), "duration_frames": 240},
        logical_entity_id="intent:alpha",
        revision_id="rev:1",
        content_digest=_sha256(b"intent-1-v1"),
    ).node
    assert intent is not None
    ids["INTENT"] = intent.node_id

    revision = graph.put_node(
        "REVISION",
        {"revision_id": "rev-1"},
        {"parent": None, "seq": 1},
        logical_entity_id="revision:alpha",
        revision_id="rev:1",
        content_digest=_sha256(b"revision-1"),
    ).node
    assert revision is not None
    ids["REVISION"] = revision.node_id

    plan = graph.put_node(
        "PLAN",
        {"plan_id": "plan-1"},
        {"steps": 3},
        logical_entity_id="plan:alpha",
        revision_id="rev:1",
        content_digest=_sha256(b"plan-1"),
    ).node
    assert plan is not None
    ids["PLAN"] = plan.node_id

    command = graph.put_node(
        "COMMAND",
        {"command_id": "cmd-1"},
        {"op": "graph.sample_op", "start": Fraction(1, 2)},
        logical_entity_id="command:alpha",
        revision_id="rev:1",
        content_digest=_sha256(b"cmd-1"),
    ).node
    assert command is not None
    ids["COMMAND"] = command.node_id

    execution = graph.put_node(
        "EXECUTION",
        {"job_id": "job-1", "attempt": 1},
        {"status": "completed"},
        logical_entity_id="execution:job-1",
        revision_id="attempt:1",
        content_digest=_sha256(b"exec-1"),
        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
        evidence_digest=_sha256(b"exec-evidence-1"),
    ).node
    assert execution is not None
    ids["EXECUTION"] = execution.node_id

    art_digest = _sha256(b"final-render-bytes")
    art_id = node_identity("ARTIFACT", {"artifact_id": "art-1"})
    art_proof = create_epistemic_proof(
        subject_id=art_id,
        subject_digest=art_digest,
        verifier_id="veritas-omnia-v1",
        evidence_digest=_sha256(b"veritas-measurement"),
    )
    artifact = graph.put_node(
        "ARTIFACT",
        {"artifact_id": "art-1"},
        {"codec": "prores", "aspect": Fraction(16, 9)},
        logical_entity_id="artifact:hero-cut",
        revision_id="rev:1",
        content_digest=art_digest,
        epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
        proof=art_proof,
    ).node
    assert artifact is not None
    ids["ARTIFACT"] = artifact.node_id

    verification = graph.put_node(
        "VERIFICATION",
        {"verification_id": "ver-1"},
        {"verdict": "ok"},
        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
        evidence_digest=_sha256(b"ver-evidence"),
    ).node
    assert verification is not None
    ids["VERIFICATION"] = verification.node_id

    passport = graph.put_node(
        "PASSPORT",
        {"passport_id": "pass-1"},
        {"status": "verified"},
    ).node
    assert passport is not None
    ids["PASSPORT"] = passport.node_id

    graph.put_edge("PROJECT_HAS_INTENT", ids["PROJECT"], ids["INTENT"])
    graph.put_edge("INTENT_COMPILED_TO_REVISION", ids["INTENT"], ids["REVISION"])
    graph.put_edge("REVISION_HAS_PLAN", ids["REVISION"], ids["PLAN"])
    graph.put_edge("PLAN_EMITS_COMMAND", ids["PLAN"], ids["COMMAND"])
    graph.put_edge("COMMAND_EXECUTED_AS", ids["COMMAND"], ids["EXECUTION"])
    graph.put_edge("EXECUTION_PRODUCED_ARTIFACT", ids["EXECUTION"], ids["ARTIFACT"])
    graph.put_edge("ARTIFACT_VERIFIED_BY", ids["ARTIFACT"], ids["VERIFICATION"])
    graph.put_edge("ARTIFACT_HAS_PASSPORT", ids["ARTIFACT"], ids["PASSPORT"])
    return ids


# --------------------------------------------------------------------------- #
# 1. Canonical Serialization, Exact Rationals & Identity Validation
# --------------------------------------------------------------------------- #
def test_canonical_serialization_exact_rationals_and_key_order() -> None:
    d1 = canonical_graph_digest({"b": Fraction(2, 4), "a": [1, True, None, "ok"]})
    d2 = canonical_graph_digest({"a": (1, True, None, "ok"), "b": Fraction(1, 2)})
    assert d1 == d2
    assert validate_sha256_digest(d1) == d1
    assert canonical_graph_json({"x": Fraction(6, 9)}) == '{"x":{"__rational__":[2,3]}}'
    assert normalize_canonical_value(Fraction(-4, 8)) == {"__rational__": [-1, 2]}


def test_canonical_serialization_refuses_floats_and_invalid_structures() -> None:
    for bad in (
        1.5,
        0.0,
        float("nan"),
        float("inf"),
        {"t": 0.25},
        {1: "non-str-key"},
        {"__rational__": [1, 2]},
        b"raw-bytes",
        Decimal("1.25"),
        {1, 2, 3},
    ):
        with pytest.raises(CanonicalValueError):
            canonical_graph_digest(bad)

    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    with pytest.raises(CanonicalValueError, match="cyclic sequence"):
        normalize_canonical_value(cyclic_list)

    cyclic_dict: dict[str, Any] = {}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(CanonicalValueError, match="cyclic mapping"):
        normalize_canonical_value(cyclic_dict)


def test_identity_and_digest_validators_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown node kind"):
        node_identity("UNKNOWN_KIND", {"id": "x"})
    with pytest.raises(ValueError, match="non-empty identity"):
        node_identity("ARTIFACT", {})
    with pytest.raises(ValueError, match="unknown relation"):
        edge_identity("BAD_RELATION", "a", "b")
    for bad_digest in ("", "md5:" + "a" * 64, "sha256:SHORT", "sha256:" + "A" * 64):
        with pytest.raises(ValueError):
            validate_sha256_digest(bad_digest)


# --------------------------------------------------------------------------- #
# 2. Formal Ontology: Identity Through Change (Triple Identity Separation)
# --------------------------------------------------------------------------- #
def test_formal_ontology_triple_separates_logical_revision_and_content(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "ontology.sqlite")
    digest_v1 = _sha256(b"hero-cut-bytes-v1")
    digest_v2 = _sha256(b"hero-cut-bytes-v2")

    triple_v1 = EntityIdentityTriple(
        logical_entity_id="asset:hero-cut",
        revision_id="rev:0001",
        content_digest=digest_v1,
    )
    triple_v2 = EntityIdentityTriple(
        logical_entity_id="asset:hero-cut",
        revision_id="rev:0002",
        content_digest=digest_v2,
    )
    # Different logical entity sharing identical bytes with rev:0001
    triple_clone = EntityIdentityTriple(
        logical_entity_id="asset:trailer-cut",
        revision_id="rev:0001",
        content_digest=digest_v1,
    )

    n1 = graph.put_node("ARTIFACT", {"slot": "hero-r1"}, entity_triple=triple_v1).node
    n2 = graph.put_node("ARTIFACT", {"slot": "hero-r2"}, entity_triple=triple_v2).node
    n3 = graph.put_node("ARTIFACT", {"slot": "trailer-r1"}, entity_triple=triple_clone).node
    assert n1 is not None and n2 is not None and n3 is not None

    assert n1.entity_triple == triple_v1
    assert n2.entity_triple == triple_v2
    assert n1.logical_entity_id == n2.logical_entity_id == "asset:hero-cut"
    assert n1.revision_id != n2.revision_id
    assert n1.content_digest != n2.content_digest

    revs = graph.revisions_of_entity("asset:hero-cut")
    assert [n.revision_id for n in revs] == ["rev:0001", "rev:0002"]

    same_bytes = graph.nodes_by_content_digest(digest_v1)
    assert {n.logical_entity_id for n in same_bytes} == {"asset:hero-cut", "asset:trailer-cut"}

    with pytest.raises(ValueError, match="not both"):
        graph.put_node(
            "ARTIFACT",
            {"slot": "conflict"},
            entity_triple=triple_v1,
            logical_entity_id="asset:other",
        )
    with pytest.raises(ValueError, match="non-empty"):
        EntityIdentityTriple("   ", "rev:1", digest_v1)
    with pytest.raises(ValueError, match="non-empty"):
        EntityIdentityTriple("asset:a", "", digest_v1)


# --------------------------------------------------------------------------- #
# 3. Strict DAG Acyclicity & Lexicographic Kahn's Algorithm
# --------------------------------------------------------------------------- #
def test_strict_dag_refuses_self_loops_and_multihop_cycles(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "dag.sqlite")
    a = graph.put_node("ARTIFACT", {"id": "a"}).node
    b = graph.put_node("ARTIFACT", {"id": "b"}).node
    c = graph.put_node("ARTIFACT", {"id": "c"}).node
    d = graph.put_node("ARTIFACT", {"id": "d"}).node
    assert a and b and c and d

    # Self-loop refused immediately
    with pytest.raises(GraphCycleError, match="self-loop"):
        graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, a.node_id)

    # Diamond DAG is valid (acyclic)
    graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id)
    graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, c.node_id)
    graph.put_edge("ARTIFACT_DERIVED_FROM", b.node_id, d.node_id)
    graph.put_edge("ARTIFACT_DERIVED_FROM", c.node_id, d.node_id)

    # Direct back-edge refused
    with pytest.raises(GraphCycleError, match="creates a cycle"):
        graph.put_edge("ARTIFACT_DERIVED_FROM", b.node_id, a.node_id)

    # Multi-hop back-edge d -> a refused
    with pytest.raises(GraphCycleError, match="creates a cycle"):
        graph.put_edge("ARTIFACT_DERIVED_FROM", d.node_id, a.node_id)

    assert graph.verify_acyclic() is True
    assert graph.integrity_check().ok


def test_lexicographic_kahn_and_graph_digest_are_permutation_invariant(tmp_path: Path) -> None:
    """Property test: 15 random insertion orders yield identical topo order & JCS digests."""
    node_specs = [(f"n{i}", {"seq": i, "ratio": Fraction(i + 1, 3)}) for i in range(8)]
    edge_pairs = [
        ("n0", "n2"),
        ("n0", "n3"),
        ("n1", "n3"),
        ("n1", "n4"),
        ("n2", "n5"),
        ("n3", "n5"),
        ("n3", "n6"),
        ("n4", "n6"),
        ("n5", "n7"),
        ("n6", "n7"),
    ]

    reference_order: list[str] | None = None
    reference_digest: str | None = None
    reference_lineage_digest: str | None = None
    rng = random.Random(20261124)

    for trial in range(15):
        graph = CreativeGraph(tmp_path / f"perm_{trial}.sqlite")
        shuffled_nodes = list(node_specs)
        shuffled_edges = list(edge_pairs)
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_edges)

        id_map: dict[str, str] = {}
        for label, payload in shuffled_nodes:
            res = graph.put_node(
                "ARTIFACT",
                {"label": label},
                payload,
                logical_entity_id=f"entity:{label}",
                revision_id="rev:1",
            )
            assert res.node is not None
            id_map[label] = res.node.node_id
        for src_l, dst_l in shuffled_edges:
            graph.put_edge("ARTIFACT_DERIVED_FROM", id_map[src_l], id_map[dst_l])

        order = graph.topological_sort()
        digest = graph.graph_digest()
        lin_digest = graph.lineage_digest(id_map["n7"])

        # Topological invariant: every edge src precedes dst
        pos = {nid: idx for idx, nid in enumerate(order)}
        for src_l, dst_l in edge_pairs:
            assert pos[id_map[src_l]] < pos[id_map[dst_l]]

        # Lexicographic-minimum invariant: at every step, the emitted node MUST
        # be the lexicographically smallest currently-ready (zero-indegree) node_id.
        prereqs: dict[str, set[str]] = {nid: set() for nid in order}
        for src_l, dst_l in edge_pairs:
            prereqs[id_map[dst_l]].add(id_map[src_l])
        emitted: set[str] = set()
        remaining = set(order)
        for chosen in order:
            ready = [nid for nid in remaining if prereqs[nid].issubset(emitted)]
            assert chosen == min(ready)
            emitted.add(chosen)
            remaining.remove(chosen)

        if reference_order is None:
            reference_order = order
            reference_digest = digest
            reference_lineage_digest = lin_digest
        else:
            assert order == reference_order
            assert digest == reference_digest
            assert lin_digest == reference_lineage_digest


# --------------------------------------------------------------------------- #
# 4. Causal Counterfactual & Invalidation Propagation
# --------------------------------------------------------------------------- #
def test_compute_causal_impact_set_diamond_and_atomic_propagation(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "causal.sqlite")
    root = graph.put_node("PROJECT", {"id": "p"}).node
    intent = graph.put_node("INTENT", {"id": "i"}).node
    sibling = graph.put_node("INTENT", {"id": "sibling"}).node
    rev_a = graph.put_node("REVISION", {"id": "ra"}).node
    rev_b = graph.put_node("REVISION", {"id": "rb"}).node
    plan = graph.put_node("PLAN", {"id": "pl"}).node
    art = graph.put_node("ARTIFACT", {"id": "art"}).node
    inactive_art = graph.put_node("ARTIFACT", {"id": "inactive"}).node
    assert root and intent and sibling and rev_a and rev_b and plan and art and inactive_art

    graph.put_edge("PROJECT_HAS_INTENT", root.node_id, intent.node_id)
    graph.put_edge("PROJECT_HAS_INTENT", root.node_id, sibling.node_id)
    graph.put_edge("INTENT_COMPILED_TO_REVISION", intent.node_id, rev_a.node_id)
    graph.put_edge("INTENT_COMPILED_TO_REVISION", intent.node_id, rev_b.node_id)
    graph.put_edge("REVISION_HAS_PLAN", rev_a.node_id, plan.node_id)
    graph.put_edge("REVISION_HAS_PLAN", rev_b.node_id, plan.node_id)
    graph.put_edge("DECISION_AFFECTS", plan.node_id, art.node_id)
    graph.put_edge("DECISION_AFFECTS", plan.node_id, inactive_art.node_id)
    graph.set_status(inactive_art.node_id, "revoked")

    impact = graph.compute_causal_impact_set(intent.node_id)
    assert impact.origin_node_id == intent.node_id
    assert impact.affected_count == 4
    assert root.node_id not in impact.impacted_node_ids
    assert sibling.node_id not in impact.impacted_node_ids
    assert inactive_art.node_id not in impact.impacted_node_ids

    # Topological order: both rev_a & rev_b precede plan, which precedes art
    order = impact.impacted_node_ids
    assert order.index(rev_a.node_id) < order.index(plan.node_id)
    assert order.index(rev_b.node_id) < order.index(plan.node_id)
    assert order.index(plan.node_id) < order.index(art.node_id)
    assert impact.impact_depths[rev_a.node_id] == 1
    assert impact.impact_depths[plan.node_id] == 2
    assert impact.impact_depths[art.node_id] == 3
    best_mid = min(rev_a.node_id, rev_b.node_id)
    assert impact.causal_paths[plan.node_id] == (intent.node_id, best_mid, plan.node_id)
    assert impact.to_dict()["affected_count"] == 4

    # Filtered by relation stops at revision layer
    filtered = graph.compute_causal_impact_set(
        intent.node_id, relations=("INTENT_COMPILED_TO_REVISION",)
    )
    assert set(filtered.impacted_node_ids) == {rev_a.node_id, rev_b.node_id}

    # Include inactive surfaces revoked leaf too
    with_inactive = graph.compute_causal_impact_set(intent.node_id, include_inactive=True)
    assert inactive_art.node_id in with_inactive.impacted_node_ids

    # Add direct shortcut intent -> plan (visited at depth 1, seen again via rev_a at depth 2)
    graph.put_edge("DECISION_AFFECTS", intent.node_id, plan.node_id)
    shortcut_impact = graph.compute_causal_impact_set(intent.node_id)
    assert shortcut_impact.impact_depths[plan.node_id] == 1
    assert shortcut_impact.causal_paths[plan.node_id] == (intent.node_id, plan.node_id)
    assert shortcut_impact.impact_depths[art.node_id] == 2

    # Preview propagation does not mutate statuses
    preview = graph.propagate_invalidation(intent.node_id, reason="prompt revised", apply=False)
    assert preview.impact_digest == shortcut_impact.impact_digest
    art_before = graph.get_node(art.node_id)
    assert art_before is not None and art_before.status == "active"

    # Apply propagation atomically invalidates origin + downstream, leaving root & sibling active
    applied = graph.propagate_invalidation(intent.node_id, reason="prompt revised", apply=True)
    assert applied.impact_digest == shortcut_impact.impact_digest
    for nid in (intent.node_id, rev_a.node_id, rev_b.node_id, plan.node_id, art.node_id):
        node = graph.get_node(nid)
        assert node is not None and node.status == "invalidated"
    root_after = graph.get_node(root.node_id)
    sib_after = graph.get_node(sibling.node_id)
    assert root_after is not None and root_after.status == "active"
    assert sib_after is not None and sib_after.status == "active"

    with pytest.raises(GraphStateError, match="unknown origin"):
        graph.compute_causal_impact_set("ARTIFACT:sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="non-empty"):
        graph.propagate_invalidation(root.node_id, reason="   ", apply=False)


# --------------------------------------------------------------------------- #
# 5. Epistemology of Proof & Monotonic Promotion Gates
# --------------------------------------------------------------------------- #
def test_epistemic_proof_gates_on_nodes_and_edges(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "epistemic.sqlite")
    c_digest = _sha256(b"measured-frame")
    ev_digest = _sha256(b"telemetry-log")
    nid = node_identity("ARTIFACT", {"id": "e1"})

    # MEASURED_EVIDENCE without evidence_digest fails closed
    with pytest.raises(EpistemicProofError, match="MEASURED_EVIDENCE"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
        )

    # VERIFIED_COMMIT without proof fails closed
    with pytest.raises(EpistemicProofError, match="VERIFIED_COMMIT"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
        )

    # Forged proof signature fails closed
    forged_proof = EpistemicProof(
        verifier_id="veritas",
        evidence_digest=ev_digest,
        chain_signature=_sha256(b"forged"),
        verified_at="2026-11-24T00:00:00Z",
    )
    assert verify_epistemic_proof(forged_proof, subject_id=nid, subject_digest=c_digest) is False
    with pytest.raises(EpistemicProofError, match="chain_signature"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=forged_proof,
        )

    # Conflicting evidence_digest vs proof.evidence_digest fails closed
    valid_proof = create_epistemic_proof(
        subject_id=nid,
        subject_digest=c_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
    )
    assert verify_epistemic_proof(valid_proof, subject_id=nid, subject_digest=c_digest) is True
    with pytest.raises(EpistemicProofError, match="conflicts"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=valid_proof,
            evidence_digest=_sha256(b"other-evidence"),
        )

    # Invalid epistemic status string & malformed evidence_digest fail closed
    with pytest.raises(EpistemicProofError, match="unknown epistemic status"):
        graph.put_node("ARTIFACT", {"id": "e1"}, epistemic_status="SUPER_TRUTH")
    with pytest.raises(EpistemicProofError, match="evidence_digest"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
            evidence_digest="not-a-sha",
        )

    # Monotonic promotion: HYPOTHESIS -> MEASURED_EVIDENCE -> VERIFIED_COMMIT
    created = graph.put_node("ARTIFACT", {"id": "e1"}, content_digest=c_digest).node
    assert created is not None and created.epistemic_status is EpistemicStatus.HYPOTHESIS

    measured = graph.promote_epistemic_status(
        nid, EpistemicStatus.MEASURED_EVIDENCE, evidence_digest=ev_digest
    )
    assert measured.epistemic_status is EpistemicStatus.MEASURED_EVIDENCE

    verified = graph.promote_epistemic_status(
        nid, EpistemicStatus.VERIFIED_COMMIT, proof=valid_proof
    )
    assert verified.epistemic_status is EpistemicStatus.VERIFIED_COMMIT

    # Demotion refused via both promote_epistemic_status and put_node
    with pytest.raises(EpistemicProofError, match="cannot demote"):
        graph.promote_epistemic_status(nid, EpistemicStatus.HYPOTHESIS)
    with pytest.raises(EpistemicProofError, match="cannot downgrade"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            {"v": 2},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.HYPOTHESIS,
        )

    # Edge epistemic promotion & refusal
    n2 = graph.put_node("ARTIFACT", {"id": "e2"}).node
    assert n2 is not None
    edge_res = graph.put_edge("ARTIFACT_DERIVED_FROM", nid, n2.node_id, {"k": 1})
    assert edge_res.edge is not None
    eid = edge_res.edge.edge_id
    e_proof = create_epistemic_proof(
        subject_id=eid,
        subject_digest=edge_res.edge.payload_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
    )
    with pytest.raises(EpistemicProofError, match="VERIFIED_COMMIT"):
        graph.promote_epistemic_status(eid, EpistemicStatus.VERIFIED_COMMIT, is_edge=True)
    promoted_edge = graph.promote_epistemic_status(
        eid, EpistemicStatus.VERIFIED_COMMIT, proof=e_proof, is_edge=True
    )
    assert promoted_edge.epistemic_status is EpistemicStatus.VERIFIED_COMMIT
    with pytest.raises(EpistemicProofError, match="cannot demote"):
        graph.promote_epistemic_status(eid, EpistemicStatus.MEASURED_EVIDENCE, is_edge=True)
    with pytest.raises(EpistemicProofError, match="cannot downgrade"):
        graph.put_edge(
            "ARTIFACT_DERIVED_FROM",
            nid,
            n2.node_id,
            {"k": 2},
            epistemic_status=EpistemicStatus.HYPOTHESIS,
        )
    with pytest.raises(GraphStateError, match="unknown node"):
        graph.promote_epistemic_status("ARTIFACT:sha256:" + "1" * 64, EpistemicStatus.HYPOTHESIS)
    with pytest.raises(GraphStateError, match="unknown edge"):
        graph.promote_epistemic_status(
            "ARTIFACT_DERIVED_FROM:a->b", EpistemicStatus.HYPOTHESIS, is_edge=True
        )
    assert graph.integrity_check().ok


# --------------------------------------------------------------------------- #
# 6. Lifecycle, History, Lineage, Queries & SQL-Injection Refusal
# --------------------------------------------------------------------------- #
def test_lifecycle_idempotency_history_and_revocation(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "lifecycle.sqlite")
    r1 = graph.put_node("PROJECT", {"id": "p1"}, {"v": 1})
    assert r1.created and r1.node is not None
    pid = r1.node.node_id

    # Idempotent repeat write
    r2 = graph.put_node("PROJECT", {"id": "p1"}, {"v": 1})
    assert r2.unchanged and not r2.created

    # Payload advance appends history and increments revision_count
    r3 = graph.put_node("PROJECT", {"id": "p1"}, {"v": 2})
    assert r3.superseded_previous and r3.node is not None
    assert r3.node.revision_count == 2
    assert len(graph.history(pid)) == 2
    assert graph.find("PROJECT", {"id": "p1"}) == r3.node

    # Terminal status cannot be silently overwritten
    sup = graph.set_status(pid, "superseded", superseded_by="PROJECT:next")
    assert sup.status == "superseded" and sup.superseded_by == "PROJECT:next"
    with pytest.raises(GraphStateError, match="superseded"):
        graph.put_node("PROJECT", {"id": "p1"}, {"v": 3})

    with pytest.raises(ValueError, match="unknown status"):
        graph.set_status(pid, "deleted")
    with pytest.raises(GraphStateError, match="unknown node"):
        graph.set_status("PROJECT:sha256:" + "0" * 64, "invalidated")

    # Edges: missing endpoint, idempotency, update, revoke refusal
    a = graph.put_node("ARTIFACT", {"id": "a"}).node
    b = graph.put_node("ARTIFACT", {"id": "b"}).node
    assert a and b
    with pytest.raises(GraphStateError, match="endpoint"):
        graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, "ARTIFACT:sha256:" + "f" * 64)

    e1 = graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id, {"w": 1})
    assert e1.created and e1.edge is not None
    e2 = graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id, {"w": 1})
    assert e2.unchanged
    e3 = graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id, {"w": 2})
    assert not e3.created and not e3.unchanged and e3.edge is not None
    assert graph.get_edge(e3.edge.edge_id) == e3.edge
    assert graph.get_edge("nonexistent") is None
    assert e3.edge.to_dict()["payload"] == {"w": 2}

    revoked = graph.revoke_edge(e3.edge.edge_id)
    assert revoked.status == "revoked"
    with pytest.raises(GraphStateError, match="revoked"):
        graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id, {"w": 3})
    with pytest.raises(GraphStateError, match="unknown edge"):
        graph.revoke_edge("ARTIFACT_DERIVED_FROM:missing->edge")


def test_lineage_traversal_and_malformed_direction_sql_safety(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "lineage.sqlite")
    ids = _build_vertical_slice(graph)

    lin = graph.lineage(ids["ARTIFACT"])
    assert lin["complete"] is True
    assert lin["deepest_kind"] == "PROJECT"
    assert len(lin["attachments"]["ARTIFACT_VERIFIED_BY"]) == 1
    assert len(lin["attachments"]["ARTIFACT_HAS_PASSPORT"]) == 1
    assert len(graph.project_history(ids["PROJECT"])) == 8
    assert len(graph.artifact_history(ids["ARTIFACT"])) == 2
    assert graph.invalidation_candidates(ids["INTENT"])["affected_count"] == 7

    # max_depth & relations filtering (including deep edge filtering at depth >= 1)
    depth1 = graph.descendants(ids["PROJECT"], max_depth=1)
    assert [row["node"]["node_id"] for row in depth1] == [ids["INTENT"]]
    only_project_intent = graph.descendants(ids["PROJECT"], relations=("PROJECT_HAS_INTENT",))
    assert [row["node"]["node_id"] for row in only_project_intent] == [ids["INTENT"]]
    only_ver = graph.descendants(ids["ARTIFACT"], relations=("ARTIFACT_VERIFIED_BY",))
    assert [row["node"]["node_id"] for row in only_ver] == [ids["VERIFICATION"]]

    # Diamond upstream parents deduplicate cleanly in lineage()
    cmd2 = graph.put_node("COMMAND", {"command_id": "cmd-2"}).node
    assert cmd2 is not None
    graph.put_edge("PLAN_EMITS_COMMAND", ids["PLAN"], cmd2.node_id)
    graph.put_edge("COMMAND_EXECUTED_AS", cmd2.node_id, ids["EXECUTION"])
    diamond_lin = graph.lineage(ids["ARTIFACT"])
    plan_level = next(lv for lv in diamond_lin["levels"] if lv["relation"] == "PLAN_EMITS_COMMAND")
    assert [n["node_id"] for n in plan_level["found"]] == [ids["PLAN"]]

    # Inactive attachment is excluded from active lineage attachments
    graph.set_status(ids["PASSPORT"], "revoked")
    assert graph.lineage(ids["ARTIFACT"])["attachments"]["ARTIFACT_HAS_PASSPORT"] == []

    # Malformed direction (including SQL injection string) refused fail-closed
    injection = "src; DROP TABLE nexus_graph_edge; --"
    with pytest.raises(ValueError, match="direction must be 'in' or 'out'"):
        graph.edges(ids["PROJECT"], direction=injection)
    with pytest.raises(ValueError, match="direction must be 'in' or 'out'"):
        graph.traverse(ids["PROJECT"], direction=injection)
    assert len(graph.edges(ids["PROJECT"], direction="out")) == 1

    # Broken chain stops honestly at break; traverse skips inactive unless requested
    graph.set_status(ids["REVISION"], "invalidated")
    assert all(
        row["node"]["node_id"] != ids["REVISION"]
        for row in graph.descendants(ids["PROJECT"], include_inactive=False)
    )
    broken = graph.lineage(ids["ARTIFACT"])
    assert broken["complete"] is False
    assert broken["deepest_kind"] == "PLAN"
    with_inactive = graph.descendants(ids["PROJECT"], include_inactive=True)
    assert any(row["node"]["node_id"] == ids["REVISION"] for row in with_inactive)

    with pytest.raises(GraphStateError, match="unknown artifact"):
        graph.lineage("ARTIFACT:sha256:" + "0" * 64)
    with pytest.raises(GraphStateError, match="unknown node"):
        graph.lineage_digest("ARTIFACT:sha256:" + "0" * 64)


# --------------------------------------------------------------------------- #
# 7. Restart Persistence, Byte-Level Tamper Detection, Snapshots & Backup
# --------------------------------------------------------------------------- #
def test_restart_persistence_in_fresh_subprocess_and_sealed_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "durable.sqlite"
    graph = CreativeGraph(db_path)
    ids = _build_vertical_slice(graph)
    expected_digest = graph.graph_digest()
    expected_lineage_digest = graph.lineage_digest(ids["ARTIFACT"])
    # In-process reopen with verify_on_open=True succeeds cleanly
    reopened_ok = CreativeGraph(db_path, verify_on_open=True)
    assert reopened_ok.graph_digest() == expected_digest
    snap_path = graph.export_sealed_snapshot(tmp_path / "snapshot.json")

    verified_snap = CreativeGraph.verify_sealed_snapshot(snap_path)
    assert verified_snap["graph_digest"] == expected_digest

    # Tamper a single byte in the sealed snapshot -> detected fail-closed
    raw = snap_path.read_text(encoding="utf-8")
    tampered_snap = tmp_path / "tampered.json"
    tampered_snap.write_text(raw.replace("Alpha Cinema", "Alpha Cinemb"), encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="seal mismatch"):
        CreativeGraph.verify_sealed_snapshot(tampered_snap)

    # Malformed snapshot files refused
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="unreadable snapshot"):
        CreativeGraph.verify_sealed_snapshot(bad_json)
    bad_env = tmp_path / "bad_env.json"
    bad_env.write_text('{"only": 1}', encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="malformed snapshot envelope"):
        CreativeGraph.verify_sealed_snapshot(bad_env)
    bad_body = tmp_path / "bad_body.json"
    bad_body.write_text('{"body": "not-dict", "snapshot_seal": "x"}', encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="malformed snapshot body"):
        CreativeGraph.verify_sealed_snapshot(bad_body)

    # Fresh subprocess restart verifies persistence & integrity_on_open
    probe = (
        "import json\n"
        "from nexus_ai_agent.provenance import CreativeGraph\n"
        f"g = CreativeGraph({str(db_path)!r}, verify_on_open=True)\n"
        f"print(json.dumps({{'digest': g.graph_digest(), "
        f"'lineage': g.lineage_digest({ids['ARTIFACT']!r}), "
        f"'complete': g.lineage({ids['ARTIFACT']!r})['complete'], "
        f"'ok': g.integrity_check().ok}}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(proc.stdout)
    assert observed == {
        "digest": expected_digest,
        "lineage": expected_lineage_digest,
        "complete": True,
        "ok": True,
    }


def test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "tamper.sqlite"
    graph = CreativeGraph(db_path)
    ids = _build_vertical_slice(graph)
    assert graph.integrity_check().ok
    assert graph.integrity_check().to_dict()["ok"] is True

    # 1. Tamper node payload digest + bad kind + bad status + bad ontology + bad epistemic
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE nexus_graph_node SET kind = 'BOGUS', status = 'bogus',"
            " payload_json = '{\"tampered\":true}', identity_json = '{bad',"
            " content_digest = 'not-sha', epistemic_status = 'BOGUS' WHERE node_id = ?",
            (ids["PROJECT"],),
        )
        conn.execute(
            "UPDATE nexus_graph_node SET payload_json = '{bad', identity_json = '{\"x\":1}',"
            " proof_json = '{bad' WHERE node_id = ?",
            (ids["INTENT"],),
        )
        conn.execute(
            'UPDATE nexus_graph_node SET proof_json = \'{"verifier_id":"v"}\' WHERE node_id = ?',
            (ids["REVISION"],),
        )
        conn.execute(
            "UPDATE nexus_graph_node SET content_digest = ? WHERE node_id = ?",
            (_sha256(b"swapped-artifact-bytes"), ids["ARTIFACT"]),
        )
        # Tamper edge relation, dangling endpoint, payload_json, digest, epistemic
        conn.execute(
            "INSERT INTO nexus_graph_edge (edge_id, relation, src, dst, payload_json,"
            " payload_digest, epistemic_status, evidence_digest, proof_json, status,"
            " source, source_seq, created_at) VALUES"
            " ('e_bad1', 'BOGUS_REL', ?, 'MISSING_NODE', '{bad', 'sha256:" + "0" * 64 + "',"
            " 'VERIFIED_COMMIT', NULL, NULL, 'active', 'direct', NULL, '2026-11-24T00:00:00Z')",
            (ids["PLAN"],),
        )
        conn.execute(
            "UPDATE nexus_graph_edge SET payload_json = '{\"tampered\":1}'"
            " WHERE src = ? AND dst = ?",
            (ids["PLAN"], ids["COMMAND"]),
        )
        # Insert a syntactically valid dangling edge to test defensive skip in traversal/impact
        conn.execute(
            "INSERT INTO nexus_graph_edge (edge_id, relation, src, dst, payload_json,"
            " payload_digest, epistemic_status, evidence_digest, proof_json, status,"
            " source, source_seq, created_at) VALUES"
            " ('e_dangle', 'DECISION_AFFECTS', ?, 'MISSING_NODE', '{}', ?,"
            " 'HYPOTHESIS', NULL, NULL, 'active', 'direct', NULL, '2026-11-24T00:00:00Z')",
            (ids["COMMAND"], canonical_graph_digest({})),
        )
        # Inject raw-SQL cycle (ARTIFACT -> EXECUTION) + delete history row + tamper chain
        conn.execute(
            "INSERT INTO nexus_graph_edge (edge_id, relation, src, dst, payload_json,"
            " payload_digest, epistemic_status, evidence_digest, proof_json, status,"
            " source, source_seq, created_at) VALUES"
            " ('e_cycle', 'ARTIFACT_DERIVED_FROM', ?, ?, '{}', ?,"
            " 'HYPOTHESIS', NULL, NULL, 'active', 'direct', NULL, '2026-11-24T00:00:00Z')",
            (ids["ARTIFACT"], ids["EXECUTION"], canonical_graph_digest({})),
        )
        conn.execute("DELETE FROM nexus_graph_node_history WHERE node_id = ?", (ids["PASSPORT"],))
        conn.execute(
            "UPDATE nexus_graph_chain SET prev_hash = ?, entry_hash = ? WHERE seq = 1",
            (_sha256(b"bad-prev"), _sha256(b"bad-entry")),
        )
        conn.commit()

    reopened = CreativeGraph(db_path)
    report = reopened.integrity_check()
    assert not report.ok
    codes = {f.code for f in report.findings}
    assert {
        "GRAPH010",
        "GRAPH011",
        "GRAPH012",
        "GRAPH013",
        "GRAPH014",
        "GRAPH015",
        "GRAPH020",
        "GRAPH021",
        "GRAPH022",
        "GRAPH023",
        "GRAPH030",
        "GRAPH040",
        "GRAPH050",
        "GRAPH051",
    }.issubset(codes)
    assert reopened.verify_acyclic(include_inactive=True) is False
    # Traversal and impact analysis safely skip dangling endpoints & survive raw-SQL cycles
    assert len(reopened.descendants(ids["EXECUTION"])) >= 1
    assert all(
        row["node"]["node_id"] != "MISSING_NODE" for row in reopened.descendants(ids["COMMAND"])
    )
    assert (
        "MISSING_NODE"
        not in reopened.compute_causal_impact_set(
            ids["COMMAND"], relations=("DECISION_AFFECTS",)
        ).impacted_node_ids
    )

    with pytest.raises(GraphIntegrityError, match="failed integrity verification on open"):
        CreativeGraph(db_path, verify_on_open=True)
    with pytest.raises(GraphIntegrityError, match="unhealthy graph"):
        reopened.export_sealed_snapshot(tmp_path / "should_fail.json")


def test_backup_restore_scope_guard_and_memory_rollback(tmp_path: Path) -> None:
    db_path = tmp_path / "graph.sqlite"
    backup_path = tmp_path / "graph.bak.sqlite"
    graph = CreativeGraph(db_path)
    ids = _build_vertical_slice(graph)
    expected_counts = graph.counts()
    graph.backup(backup_path)

    # Restore into clean file succeeds
    target = CreativeGraph(tmp_path / "restored.sqlite")
    target.restore(backup_path)
    assert target.counts() == expected_counts
    assert target.integrity_check().ok
    assert target.lineage(ids["ARTIFACT"])["complete"] is True

    # Restore refuses foreign table unless whole_database=True (file-backed)
    shared_path = tmp_path / "shared.sqlite"
    journal = CausalJournal(shared_path)
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id="j1",
            job_type="render",
            idempotency_key="k1",
            attempt=None,
            status="pending",
            occurred_at="2026-11-24T00:00:00Z",
        )
    )
    shared_graph = CreativeGraph(shared_path)
    with pytest.raises(GraphStateError, match="whole_database=True"):
        shared_graph.restore(backup_path)
    shared_graph.restore(backup_path, whole_database=True)
    assert shared_graph.counts() == expected_counts

    # Restore scope guard & write path for :memory:
    mem_graph = CreativeGraph(":memory:")
    mem_node = mem_graph.put_node("PROJECT", {"id": "mem-p"}).node
    assert mem_node is not None
    with mem_graph._connection() as conn:
        conn.execute("CREATE TABLE foreign_authority (id INTEGER PRIMARY KEY)")
    with pytest.raises(GraphStateError, match="whole_database=True"):
        mem_graph.restore(backup_path)
    mem_graph.restore(backup_path, whole_database=True)
    assert mem_graph.counts() == expected_counts

    # Rollback on failure in :memory:
    before_mem = mem_graph.counts()
    with pytest.raises(RuntimeError):
        with mem_graph._connection() as conn:
            conn.execute("DELETE FROM nexus_graph_node")
            raise RuntimeError("boom")
    assert mem_graph.counts() == before_mem

    # Corrupt, missing-table, and failed-pragma backup sources are refused
    not_db = tmp_path / "not_db.bin"
    not_db.write_bytes(b"definitely not a sqlite database")
    with pytest.raises(GraphStateError, match="not a readable database"):
        target.restore(not_db)

    empty_db = tmp_path / "empty.sqlite"
    sqlite3.connect(str(empty_db)).close()
    with pytest.raises(GraphStateError, match="missing table"):
        target.restore(empty_db)

    # Corrupt a single indexed node_id byte on disk in a secondary index page so
    # SQLite opens the schema cleanly yet PRAGMA integrity_check returns a non-'ok' error.
    corrupt_bak = tmp_path / "corrupt_page.sqlite"
    corrupt_bytes = bytearray(backup_path.read_bytes())
    marker = ids["PROJECT"].encode("utf-8")
    first_pos = corrupt_bytes.find(marker)
    assert first_pos != -1
    second_pos = corrupt_bytes.find(marker, first_pos + len(marker))
    assert second_pos != -1
    corrupt_bytes[second_pos] ^= 0x01
    corrupt_bak.write_bytes(bytes(corrupt_bytes))
    with pytest.raises(GraphStateError, match="PRAGMA integrity_check"):
        target.restore(corrupt_bak)


# --------------------------------------------------------------------------- #
# 8. Journal Rebuild & Concurrency
# --------------------------------------------------------------------------- #
def test_rebuild_from_journal_store_idempotency_and_terminal_preservation(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    art_digest = _sha256(b"rendered-movie")
    for kind, attempt, status, res_digest in (
        (EventKind.JOB_ENQUEUED, None, "pending", None),
        (EventKind.JOB_RESERVED, 1, "processing", None),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying", None),
        (EventKind.JOB_COMPLETED, 1, "completed", art_digest),
    ):
        journal.append(
            CausalEvent(
                kind=kind,
                job_id="job-42",
                job_type="creative.render",
                idempotency_key="k-42",
                attempt=attempt,
                status=status,
                result_digest=res_digest,
                occurred_at="2026-11-24T00:00:00Z",
            )
        )

    graph = CreativeGraph(tmp_path / "projected.sqlite")
    first = graph.rebuild_from_journal_store(journal)
    assert first["projected_nodes"] == {
        "EXECUTION": 2,
        "ARTIFACT": 1,
        "VERIFICATION": 1,
        "PASSPORT": 1,
    }
    assert first["projected_edges"] == 3
    assert "PROJECT" in first["unmigrated_levels"]
    assert graph.integrity_check().ok

    # Idempotent second rebuild creates zero new nodes/edges
    second = graph.rebuild_from_journal_store(journal)
    assert second["projected_nodes"] == {
        "EXECUTION": 0,
        "ARTIFACT": 0,
        "VERIFICATION": 0,
        "PASSPORT": 0,
    }
    assert second["projected_edges"] == 0

    # Mark passport terminal, then replace=True rebuild preserves terminal & restores edge
    art_id = node_identity("ARTIFACT", {"job_id": "job-42", "result_digest": art_digest})
    pass_id = node_identity("PASSPORT", {"job_id": "job-42", "result_digest": art_digest})
    graph.set_status(pass_id, "revoked")

    rebuilt = graph.rebuild_from_journal_store(journal, replace=True)
    assert any(item["node_id"] == pass_id for item in rebuilt["skipped_terminal"])
    passport_node = graph.get_node(pass_id)
    assert passport_node is not None and passport_node.status == "revoked"
    # Edge ARTIFACT_HAS_PASSPORT must still exist even though passport was terminal
    assert len(graph.edges(art_id, direction="out", relation="ARTIFACT_HAS_PASSPORT")) == 1
    assert graph.integrity_check().ok

    # Job completed without an explicit verification-started event projects cleanly
    journal_no_ver = CausalJournal(tmp_path / "causal_no_ver.sqlite")
    journal_no_ver.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id="job-no-ver",
            job_type="creative.render",
            idempotency_key="k-no-ver",
            attempt=1,
            status="completed",
            result_digest=_sha256(b"no-ver-artifact"),
            occurred_at="2026-11-24T00:00:00Z",
        )
    )
    no_ver_graph = CreativeGraph(tmp_path / "no_ver_graph.sqlite")
    no_ver_res = no_ver_graph.rebuild_from_journal_store(journal_no_ver)
    assert no_ver_res["projected_nodes"]["VERIFICATION"] == 0
    assert no_ver_res["projected_edges"] == 2
    assert no_ver_graph.integrity_check().ok

    # Records missing job_id are safely ignored
    blank = LedgerRecord(
        seq=99,
        record_hash="a" * 64,
        prev_hash=GRAPH_CHAIN_GENESIS,
        record_json='{"kind":"job_enqueued"}',
        dedupe_key=None,
        created_at="2026-11-24T00:00:00Z",
    )
    blank_res = graph.rebuild_from_journal([blank])
    assert blank_res["records_read"] == 1
    assert blank_res["projected_edges"] == 0


def test_concurrent_writers_preserve_chain_and_dag_integrity(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "concurrent.sqlite")
    root = graph.put_node("PROJECT", {"id": "root"}).node
    assert root is not None
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            res = graph.put_node("INTENT", {"worker": idx}, {"idx": idx})
            assert res.node is not None
            graph.put_edge("PROJECT_HAS_INTENT", root.node_id, res.node.node_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert graph.counts()["nodes"] == 13
    assert graph.counts()["edges"] == 12
    assert graph.integrity_check().ok
    assert compute_proof_signature(
        subject_id="n",
        subject_digest=_sha256(b"a"),
        verifier_id="v",
        evidence_digest=_sha256(b"e"),
    ).startswith("sha256:")
