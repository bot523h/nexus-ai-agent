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
    ALLOWED_NODE_STATUS_TRANSITIONS,
    CANONICAL_ENCODING_VERSION,
    GRAPH_CHAIN_GENESIS,
    TERMINAL_STATUSES,
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
    GraphStateTransitionError,
    JobFacts,
    LedgerRecord,
    canonical_graph_digest,
    canonical_graph_json,
    compute_proof_signature,
    create_epistemic_proof,
    edge_identity,
    node_identity,
    normalize_canonical_value,
    validate_hmac_sha256_signature,
    validate_sha256_digest,
    verify_epistemic_proof,
)

_TEST_KEY_ID = "veritas-key-2026"
_TEST_SECRET_KEY = b"nexus-stage2a-veritas-hmac-secret-32b"
_TEST_ALT_SECRET_KEY = b"nexus-stage2a-different-hmac-secret-9"
_TEST_REVOKED_KEY_ID = "veritas-revoked-2025"
_TEST_KEY_RING: dict[str, bytes] = {
    _TEST_KEY_ID: _TEST_SECRET_KEY,
    _TEST_REVOKED_KEY_ID: _TEST_ALT_SECRET_KEY,
}


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
        secret_key=_TEST_SECRET_KEY,
        key_id=_TEST_KEY_ID,
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
# 5. Epistemology of Proof & Keyed HMAC-SHA256 Authentication Gates (F08)
# --------------------------------------------------------------------------- #
def test_epistemic_proof_gates_on_nodes_and_edges(tmp_path: Path) -> None:
    db_path = tmp_path / "epistemic.sqlite"
    graph = CreativeGraph(
        db_path,
        proof_verification_keys=_TEST_KEY_RING,
        revoked_proof_key_ids={_TEST_REVOKED_KEY_ID},
    )
    unconfigured_graph = CreativeGraph(tmp_path / "no_keys.sqlite")
    c_digest = _sha256(b"measured-frame")
    ev_digest = _sha256(b"telemetry-log")
    nid = node_identity("ARTIFACT", {"id": "e1"})

    # Unkeyed sha256 signature rejected by validator & constructor
    with pytest.raises(ValueError, match="hmac-sha256"):
        validate_hmac_sha256_signature(_sha256(b"unkeyed"))
    with pytest.raises(ValueError, match="hmac-sha256"):
        EpistemicProof(
            key_id=_TEST_KEY_ID,
            verifier_id="veritas",
            evidence_digest=ev_digest,
            chain_signature=_sha256(b"unkeyed"),
            issued_at_utc="2026-11-24T00:00:00Z",
        )

    # Weak/short HMAC key rejected fail-closed
    with pytest.raises(EpistemicProofError, match="secret_key"):
        create_epistemic_proof(
            secret_key=b"short",
            key_id=_TEST_KEY_ID,
            subject_id=nid,
            subject_digest=c_digest,
            verifier_id="veritas",
            evidence_digest=ev_digest,
        )

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

    # Valid keyed proof minted with trusted HMAC key
    valid_proof = create_epistemic_proof(
        secret_key=_TEST_SECRET_KEY,
        key_id=_TEST_KEY_ID,
        subject_id=nid,
        subject_digest=c_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
    )
    assert valid_proof.verified_at == valid_proof.issued_at_utc
    assert valid_proof.canonical_version == CANONICAL_ENCODING_VERSION
    assert (
        verify_epistemic_proof(
            valid_proof,
            subject_id=nid,
            subject_digest=c_digest,
            proof_verification_keys=_TEST_KEY_RING,
        )
        is True
    )

    # Callable key resolver works and rejects weak/missing keys fail-closed
    assert (
        verify_epistemic_proof(
            valid_proof,
            subject_id=nid,
            subject_digest=c_digest,
            proof_verification_keys=lambda kid: _TEST_KEY_RING.get(kid),
        )
        is True
    )
    assert (
        verify_epistemic_proof(
            valid_proof,
            subject_id=nid,
            subject_digest=c_digest,
            proof_verification_keys=lambda _kid: b"too-short",
        )
        is False
    )

    # Graph with NO configured key ring refuses VERIFIED_COMMIT even with valid proof
    with pytest.raises(EpistemicProofError, match="HMAC-SHA256 authentication failed"):
        unconfigured_graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=valid_proof,
        )

    # Wrong HMAC key fails closed
    wrong_key_proof = create_epistemic_proof(
        secret_key=_TEST_ALT_SECRET_KEY,
        key_id=_TEST_KEY_ID,
        subject_id=nid,
        subject_digest=c_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
    )
    assert (
        verify_epistemic_proof(
            wrong_key_proof,
            subject_id=nid,
            subject_digest=c_digest,
            proof_verification_keys=_TEST_KEY_RING,
        )
        is False
    )
    with pytest.raises(EpistemicProofError, match="HMAC-SHA256 authentication failed"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=wrong_key_proof,
        )

    # Revoked key_id fails closed even when signed with the exact key bytes
    revoked_proof = create_epistemic_proof(
        secret_key=_TEST_ALT_SECRET_KEY,
        key_id=_TEST_REVOKED_KEY_ID,
        subject_id=nid,
        subject_digest=c_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
    )
    with pytest.raises(EpistemicProofError, match="HMAC-SHA256 authentication failed"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=c_digest,
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=revoked_proof,
        )

    # Unknown canonical_version fails closed
    bad_ver_proof = create_epistemic_proof(
        secret_key=_TEST_SECRET_KEY,
        key_id=_TEST_KEY_ID,
        subject_id=nid,
        subject_digest=c_digest,
        verifier_id="veritas",
        evidence_digest=ev_digest,
        canonical_version="NEXUS_GRAPH_CANONICAL_V999",
    )
    assert (
        verify_epistemic_proof(
            bad_ver_proof,
            subject_id=nid,
            subject_digest=c_digest,
            proof_verification_keys=_TEST_KEY_RING,
        )
        is False
    )

    # Payload/content_digest swap with valid proof fails closed
    with pytest.raises(EpistemicProofError, match="HMAC-SHA256 authentication failed"):
        graph.put_node(
            "ARTIFACT",
            {"id": "e1"},
            content_digest=_sha256(b"swapped-bytes"),
            epistemic_status=EpistemicStatus.VERIFIED_COMMIT,
            proof=valid_proof,
        )

    # Conflicting evidence_digest vs proof.evidence_digest fails closed
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

    # Reopening without trusted keys or with revoked key fails on open by default (F10)
    with pytest.raises(GraphIntegrityError, match="failed integrity verification on open"):
        CreativeGraph(db_path)
    with pytest.raises(GraphIntegrityError, match="failed integrity verification on open"):
        CreativeGraph(
            db_path,
            proof_verification_keys=_TEST_KEY_RING,
            revoked_proof_key_ids={_TEST_KEY_ID},
        )
    assert CreativeGraph(db_path, verify_on_open=False).integrity_check().ok is False
    assert (
        CreativeGraph(
            db_path,
            verify_on_open=False,
            proof_verification_keys=_TEST_KEY_RING,
            revoked_proof_key_ids={_TEST_KEY_ID},
        )
        .integrity_check()
        .ok
        is False
    )
    assert CreativeGraph(db_path, proof_verification_keys=_TEST_KEY_RING).integrity_check().ok

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
        secret_key=_TEST_SECRET_KEY,
        key_id=_TEST_KEY_ID,
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
    graph = CreativeGraph(tmp_path / "lineage.sqlite", proof_verification_keys=_TEST_KEY_RING)
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
    graph = CreativeGraph(db_path, proof_verification_keys=_TEST_KEY_RING)
    ids = _build_vertical_slice(graph)
    expected_digest = graph.graph_digest()
    expected_lineage_digest = graph.lineage_digest(ids["ARTIFACT"])
    # In-process reopen with verify_on_open=True succeeds cleanly
    reopened_ok = CreativeGraph(
        db_path, verify_on_open=True, proof_verification_keys=_TEST_KEY_RING
    )
    assert reopened_ok.graph_digest() == expected_digest
    snap_path = graph.export_sealed_snapshot(tmp_path / "snapshot.json")

    verified_snap = CreativeGraph.verify_sealed_snapshot(snap_path)
    assert verified_snap["graph_digest"] == expected_digest
    assert verified_snap["canonical_version"] == CANONICAL_ENCODING_VERSION

    raw = snap_path.read_text(encoding="utf-8")

    # Unsupported canonical_version in snapshot envelope is rejected fail-closed (F07)
    bad_ver_snap = tmp_path / "bad_ver_snap.json"
    bad_ver_snap.write_text(
        raw.replace(CANONICAL_ENCODING_VERSION, "NEXUS_GRAPH_CANONICAL_V999"),
        encoding="utf-8",
    )
    with pytest.raises(GraphIntegrityError, match="unsupported snapshot canonical_version"):
        CreativeGraph.verify_sealed_snapshot(bad_ver_snap)

    # Tamper a single byte in the sealed snapshot -> detected fail-closed
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
        f"g = CreativeGraph({str(db_path)!r}, verify_on_open=True, "
        f"proof_verification_keys={_TEST_KEY_RING!r})\n"
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
    graph = CreativeGraph(db_path, proof_verification_keys=_TEST_KEY_RING)
    ids = _build_vertical_slice(graph)
    assert graph.integrity_check().ok
    assert graph.verify_chain() == []
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
            "UPDATE nexus_graph_node SET kind = 'BOGUS', proof_json = '{\"verifier_id\":\"v\"}'"
            " WHERE node_id = ?",
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

    # Default verify_on_open=True immediately rejects tampered DB on open (F10)
    with pytest.raises(GraphIntegrityError, match="failed integrity verification on open"):
        CreativeGraph(db_path, proof_verification_keys=_TEST_KEY_RING)

    # Explicit verify_on_open=False permits forensic inspection of every finding code
    reopened = CreativeGraph(db_path, verify_on_open=False, proof_verification_keys=_TEST_KEY_RING)
    assert len(reopened.verify_chain()) >= 2
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
        "GRAPH016",
        "GRAPH020",
        "GRAPH021",
        "GRAPH022",
        "GRAPH023",
        "GRAPH024",
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
        CreativeGraph(db_path, verify_on_open=True, proof_verification_keys=_TEST_KEY_RING)
    with pytest.raises(GraphIntegrityError, match="unhealthy graph"):
        reopened.export_sealed_snapshot(tmp_path / "should_fail.json")


def test_integrity_full_history_row_and_canonical_id_derivation_audit(tmp_path: Path) -> None:
    """F09: Every historical row digest, continuity, chain link, and canonical ID is audited."""
    db_path = tmp_path / "hist_audit.sqlite"
    graph = CreativeGraph(db_path)
    p = graph.put_node("PROJECT", {"id": "p1"}, {"v": 1}).node
    c = graph.put_node("INTENT", {"id": "i1"}, {"v": 1}).node
    assert p is not None and c is not None
    graph.put_node("PROJECT", {"id": "p1"}, {"v": 2})
    edge = graph.put_edge("PROJECT_HAS_INTENT", p.node_id, c.node_id, {"w": 1}).edge
    assert edge is not None
    assert graph.integrity_check().ok

    # 1. Tamper historical payload_json on row 1 (leaving live node untouched) -> GRAPH031
    h1_path = tmp_path / "h1.sqlite"
    graph.backup(h1_path)
    with sqlite3.connect(str(h1_path)) as conn:
        conn.execute(
            "UPDATE nexus_graph_node_history SET payload_json = '{\"v\":999}'"
            " WHERE node_id = ? AND history_seq = (SELECT MIN(history_seq)"
            " FROM nexus_graph_node_history WHERE node_id = ?)",
            (p.node_id, p.node_id),
        )
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="GRAPH031"):
        CreativeGraph(h1_path)
    r_h1 = CreativeGraph(h1_path, verify_on_open=False).integrity_check()
    assert "GRAPH031" in {f.code for f in r_h1.findings}

    # 2. Tamper historical metadata (bad epistemic + terminal resurrection in history) -> GRAPH032
    #    and inject extra rogue history row without chain entry -> GRAPH033
    h2_path = tmp_path / "h2.sqlite"
    graph.backup(h2_path)
    with sqlite3.connect(str(h2_path)) as conn:
        conn.execute(
            "UPDATE nexus_graph_node_history SET epistemic_status = 'BOGUS', status = 'revoked'"
            " WHERE node_id = ? AND history_seq = (SELECT MIN(history_seq)"
            " FROM nexus_graph_node_history WHERE node_id = ?)",
            (p.node_id, p.node_id),
        )
        conn.execute(
            "INSERT INTO nexus_graph_node_history (node_id, revision_id, content_digest,"
            " payload_json, payload_digest, epistemic_status, proof_json, status, recorded_at)"
            " VALUES (?, 'rev:rogue', ?, '{\"v\":2}', ?, 'HYPOTHESIS', NULL, 'active',"
            " '2026-11-24T00:00:00Z')",
            (p.node_id, p.content_digest, canonical_graph_digest({"v": 2})),
        )
        conn.commit()
    r_h2 = CreativeGraph(h2_path, verify_on_open=False).integrity_check()
    h2_codes = {f.code for f in r_h2.findings}
    assert {"GRAPH032", "GRAPH033"}.issubset(h2_codes)

    # 3. Direct-SQL tamper of canonical node_id and edge_id columns -> GRAPH016 & GRAPH024
    h3_path = tmp_path / "h3.sqlite"
    graph.backup(h3_path)
    with sqlite3.connect(str(h3_path)) as conn:
        conn.execute(
            "UPDATE nexus_graph_node SET node_id = 'PROJECT:sha256:" + "f" * 64 + "'"
            " WHERE node_id = ?",
            (p.node_id,),
        )
        conn.execute(
            "UPDATE nexus_graph_edge SET edge_id = 'PROJECT_HAS_INTENT:forged->id'"
            " WHERE edge_id = ?",
            (edge.edge_id,),
        )
        conn.commit()
    r_h3 = CreativeGraph(h3_path, verify_on_open=False).integrity_check()
    h3_codes = {f.code for f in r_h3.findings}
    assert {"GRAPH016", "GRAPH024"}.issubset(h3_codes)


def test_canonical_versioned_contract_and_golden_vectors(tmp_path: Path) -> None:
    """F07: Golden vectors for Layer-1 NEXUS_GRAPH_CANONICAL_V1 + Layer-2 JCS_RFC8785_V2."""
    assert CANONICAL_ENCODING_VERSION == "NEXUS_GRAPH_CANONICAL_V1"

    # Layer-1 reduced rational canonical forms (den > 0, gcd(num, den) == 1)
    assert normalize_canonical_value(Fraction(6, -9)) == {"__rational__": [-2, 3]}
    assert normalize_canonical_value(Fraction(0, 5)) == {"__rational__": [0, 1]}
    assert normalize_canonical_value(Fraction(4, 2)) == {"__rational__": [2, 1]}

    # Golden vector: Layer-1 JSON + Layer-2 RFC 8785 JCS SHA-256 digest
    sample = {
        "z": Fraction(24000, 1001),
        "a": [1, Fraction(-6, 9), True, None],
        "m": {"nested": Fraction(16, 9)},
    }
    expected_json = (
        '{"a":[1,{"__rational__":[-2,3]},true,null],'
        '"m":{"nested":{"__rational__":[16,9]}},'
        '"z":{"__rational__":[24000,1001]}}'
    )
    assert canonical_graph_json(sample) == expected_json
    expected_digest = _sha256(expected_json.encode("utf-8"))
    assert canonical_graph_digest(sample) == expected_digest

    # Graph digest, lineage digest, impact digest & snapshot all bind canonical_version
    g = CreativeGraph(tmp_path / "golden.sqlite")
    p = g.put_node("PROJECT", {"id": "p"}, {"fps": Fraction(24000, 1001)}).node
    i = g.put_node("INTENT", {"id": "i"}, {"gain": Fraction(3, 2)}).node
    assert p is not None and i is not None
    g.put_edge("PROJECT_HAS_INTENT", p.node_id, i.node_id)
    snap = g.export_sealed_snapshot(tmp_path / "golden_snap.json")
    body = CreativeGraph.verify_sealed_snapshot(snap)
    assert body["canonical_version"] == CANONICAL_ENCODING_VERSION
    assert body["graph_digest"] == g.graph_digest()


def test_backup_restore_scope_guard_and_memory_rollback(tmp_path: Path) -> None:
    db_path = tmp_path / "graph.sqlite"
    backup_path = tmp_path / "graph.bak.sqlite"
    graph = CreativeGraph(db_path, proof_verification_keys=_TEST_KEY_RING)
    ids = _build_vertical_slice(graph)
    expected_counts = graph.counts()
    graph.backup(backup_path)

    # Restore into clean file succeeds
    target = CreativeGraph(tmp_path / "restored.sqlite", proof_verification_keys=_TEST_KEY_RING)
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
    shared_graph = CreativeGraph(shared_path, proof_verification_keys=_TEST_KEY_RING)
    with pytest.raises(GraphStateError, match="whole_database=True"):
        shared_graph.restore(backup_path)
    shared_graph.restore(backup_path, whole_database=True)
    assert shared_graph.counts() == expected_counts

    # Restore scope guard & write path for :memory:
    mem_graph = CreativeGraph(":memory:", proof_verification_keys=_TEST_KEY_RING)
    mem_node = mem_graph.put_node("PROJECT", {"id": "mem-p"}).node
    assert mem_node is not None
    with mem_graph._connection() as conn:
        conn.execute("CREATE TABLE foreign_authority (id INTEGER PRIMARY KEY)")
    with pytest.raises(GraphStateError, match="whole_database=True"):
        mem_graph.restore(backup_path)
    mem_graph.restore(backup_path, whole_database=True)
    assert mem_graph.counts() == expected_counts

    # F05: Physically valid SQLite backup (PRAGMA integrity_check == ok) with logical
    # payload tampering or broken hash chain is rejected BEFORE modifying live target!
    live_target = CreativeGraph(
        tmp_path / "live_target.sqlite", proof_verification_keys=_TEST_KEY_RING
    )
    live_node = live_target.put_node("PROJECT", {"id": "untouched-live"}).node
    assert live_node is not None
    live_digest_before = live_target.graph_digest()
    live_counts_before = live_target.counts()

    tampered_payload_bak = tmp_path / "logical_tamper_payload.sqlite"
    tampered_payload_bak.write_bytes(backup_path.read_bytes())
    with sqlite3.connect(str(tampered_payload_bak)) as conn:
        conn.execute(
            'UPDATE nexus_graph_node SET payload_json = \'{"title":"Tampered"}\' WHERE node_id = ?',
            (ids["PROJECT"],),
        )
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="failed logical integrity check"):
        live_target.restore(tampered_payload_bak)
    assert live_target.counts() == live_counts_before
    assert live_target.graph_digest() == live_digest_before

    tampered_chain_bak = tmp_path / "logical_tamper_chain.sqlite"
    tampered_chain_bak.write_bytes(backup_path.read_bytes())
    with sqlite3.connect(str(tampered_chain_bak)) as conn:
        conn.execute("UPDATE nexus_graph_chain SET entry_hash = ? WHERE seq = 1", (_sha256(b"x"),))
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="failed logical integrity check"):
        live_target.restore(tampered_chain_bak)
    assert live_target.counts() == live_counts_before
    assert live_target.graph_digest() == live_digest_before

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
# 8. Journal Rebuild, Honest Semantics (F03/F04), State Machine (F01) & Races (F02)
# --------------------------------------------------------------------------- #
def _sample_job_facts(
    job_id: str,
    *,
    result_digest: str | None = None,
    verification: dict[str, Any] | None = None,
    artifact_passport: dict[str, Any] | None = None,
    artifact_passport_known: bool = True,
) -> JobFacts:
    return JobFacts(
        job_id=job_id,
        job_type="creative.render",
        idempotency_key=f"k-{job_id}",
        status="completed",
        attempt=1,
        error=None,
        created_at="2026-11-24T00:00:00Z",
        started_at="2026-11-24T00:00:01Z",
        finished_at="2026-11-24T00:00:02Z",
        payload={"scene": 1},
        payload_digest=_sha256(b"scene-1"),
        result={"artifact": "out.mov"},
        result_digest=result_digest,
        verification=verification,
        artifact_passport=artifact_passport,
        artifact_passport_known=artifact_passport_known,
    )


def test_terminal_state_resurrection_and_reassignment_refused(tmp_path: Path) -> None:
    """F01: Terminal statuses (superseded/invalidated/revoked) are strictly absorbing."""
    assert ALLOWED_NODE_STATUS_TRANSITIONS["revoked"] == frozenset({"revoked"})
    assert ALLOWED_NODE_STATUS_TRANSITIONS["invalidated"] == frozenset({"invalidated"})
    assert ALLOWED_NODE_STATUS_TRANSITIONS["superseded"] == frozenset({"superseded"})

    graph = CreativeGraph(tmp_path / "fsm.sqlite")
    nodes: dict[str, str] = {}
    for term in ("revoked", "invalidated", "superseded"):
        n = graph.put_node("ARTIFACT", {"term": term}).node
        assert n is not None
        graph.set_status(n.node_id, term)
        nodes[term] = n.node_id

    child = graph.put_node("ARTIFACT", {"term": "child"}).node
    assert child is not None

    for term, nid in nodes.items():
        chain_before = graph.chain_entries()
        hist_before = graph.history(nid)

        # Resurrection to active & reassignment to other terminal statuses refused fail-closed
        for forbidden_target in ("active", "revoked", "invalidated", "superseded"):
            if forbidden_target == term:
                continue
            with pytest.raises(GraphStateTransitionError, match="illegal status transition"):
                graph.set_status(nid, forbidden_target)
            current = graph.get_node(nid)
            assert current is not None and current.status == term
            assert graph.chain_entries() == chain_before
            assert graph.history(nid) == hist_before

        # Idempotent re-assertion of the exact same terminal status is a clean no-op
        same = graph.set_status(nid, term)
        assert same.status == term
        assert graph.chain_entries() == chain_before
        assert graph.history(nid) == hist_before

        # Attaching a new outgoing causal edge from a terminal node is refused fail-closed
        with pytest.raises(GraphStateTransitionError, match="terminal node"):
            graph.put_edge("ARTIFACT_DERIVED_FROM", nid, child.node_id)

    assert graph.integrity_check().ok


def test_two_process_concurrent_invalidation_and_edge_attachment_race(tmp_path: Path) -> None:
    """F02: BEGIN IMMEDIATE serializes cross-process invalidation vs downstream edge insert."""
    for trial in range(4):
        db_path = tmp_path / f"race_{trial}.sqlite"
        graph = CreativeGraph(db_path)
        a = graph.put_node("INTENT", {"id": f"a-{trial}"}).node
        b = graph.put_node("PLAN", {"id": f"b-{trial}"}).node
        c = graph.put_node("EXECUTION", {"id": f"c-{trial}"}).node
        d = graph.put_node("ARTIFACT", {"id": f"d-{trial}"}).node
        assert a is not None and b is not None and c is not None and d is not None
        graph.put_edge("INTENT_COMPILED_TO_REVISION", a.node_id, b.node_id)
        graph.put_edge("REVISION_HAS_PLAN", b.node_id, c.node_id)

        worker_code = (
            "import json, sys\n"
            "from nexus_ai_agent.provenance import CreativeGraph, GraphStateTransitionError\n"
            f"g = CreativeGraph({str(db_path)!r})\n"
            "try:\n"
            f"    g.put_edge('EXECUTION_PRODUCED_ARTIFACT', {c.node_id!r}, {d.node_id!r})\n"
            "    print(json.dumps({'outcome': 'attached'}))\n"
            "except GraphStateTransitionError:\n"
            "    print(json.dumps({'outcome': 'rejected_terminal'}))\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            if trial % 2 == 1:
                proc.wait(timeout=30)
            graph.propagate_invalidation(a.node_id, reason=f"race-{trial}", apply=True)
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:  # pragma: no cover
                proc.kill()
        assert proc.returncode == 0, f"worker failed: {stderr}"
        worker_outcome = json.loads(stdout)["outcome"]
        assert worker_outcome in {"attached", "rejected_terminal"}

        c_after = graph.get_node(c.node_id)
        d_after = graph.get_node(d.node_id)
        assert c_after is not None and c_after.status == "invalidated"
        assert d_after is not None
        c_out_edges = graph.edges(c.node_id, direction="out")
        if worker_outcome == "attached":
            assert len(c_out_edges) == 1
            assert d_after.status == "invalidated"
        else:
            assert c_out_edges == []
            assert d_after.status == "active"
        assert graph.integrity_check().ok


def test_rebuild_from_journal_refuses_tampered_or_malformed_source_chain(tmp_path: Path) -> None:
    """F03: Source journal hash chain & column agreement enforced BEFORE any graph mutation."""
    from nexus_ai_agent.provenance.models import compute_record_hash

    art_digest = _sha256(b"source-art")
    journal = CausalJournal(tmp_path / "valid_causal.sqlite")
    for kind, attempt, status, res in (
        (EventKind.JOB_ENQUEUED, None, "pending", None),
        (EventKind.JOB_RESERVED, 1, "processing", None),
        (EventKind.JOB_COMPLETED, 1, "completed", art_digest),
    ):
        journal.append(
            CausalEvent(
                kind=kind,
                job_id="job-src",
                job_type="creative.render",
                idempotency_key="k-src",
                attempt=attempt,
                status=status,
                result_digest=res,
                occurred_at="2026-11-24T00:00:00Z",
            )
        )
    valid_records = journal.all_records()
    assert len(valid_records) == 3

    def _assert_refused(bad_records: list[LedgerRecord], match: str, tag: str) -> None:
        g = CreativeGraph(tmp_path / f"refuse_{tag}.sqlite")
        with pytest.raises(GraphIntegrityError, match=match):
            g.rebuild_from_journal(bad_records)
        assert g.counts() == {"nodes": 0, "edges": 0, "history": 0, "chain": 0}

    # 1. Tampered record_hash
    r0 = valid_records[0]
    _assert_refused(
        [
            LedgerRecord(
                seq=r0.seq,
                record_hash=_sha256(b"tampered-hash"),
                prev_hash=r0.prev_hash,
                record_json=r0.record_json,
                dedupe_key=r0.dedupe_key,
                created_at=r0.created_at,
            ),
            *valid_records[1:],
        ],
        "record_hash",
        "rec_hash",
    )

    # 2. Broken prev_hash linkage
    r1 = valid_records[1]
    _assert_refused(
        [
            valid_records[0],
            LedgerRecord(
                seq=r1.seq,
                record_hash=r1.record_hash,
                prev_hash=_sha256(b"wrong-prev"),
                record_json=r1.record_json,
                dedupe_key=r1.dedupe_key,
                created_at=r1.created_at,
            ),
            valid_records[2],
        ],
        "prev_hash",
        "prev_hash",
    )

    # 3. Deleted middle record (sequence gap 1, 3)
    _assert_refused([valid_records[0], valid_records[2]], "sequence gap", "seq_gap")

    # 4. Malformed JSON in record_json
    _assert_refused(
        [
            LedgerRecord(
                seq=1,
                record_hash=compute_record_hash("{not-json"),
                prev_hash=GRAPH_CHAIN_GENESIS,
                record_json="{not-json",
                dedupe_key=None,
                created_at="2026-11-24T00:00:00Z",
            )
        ],
        "malformed",
        "bad_json",
    )

    # 5. Valid hash over non-object / invalid kind / missing job_id / invalid attempt / bad dedupe
    def _signed_rec(raw_json: str, dedupe: str | None) -> list[LedgerRecord]:
        return [
            LedgerRecord(
                seq=1,
                record_hash=compute_record_hash(raw_json),
                prev_hash=GRAPH_CHAIN_GENESIS,
                record_json=raw_json,
                dedupe_key=dedupe,
                created_at="2026-11-24T00:00:00Z",
            )
        ]

    _assert_refused(_signed_rec("[]", None), "malformed", "non_obj")
    _assert_refused(
        _signed_rec(
            json.dumps({"seq": 1, "prev_hash": GRAPH_CHAIN_GENESIS, "kind": "bogus_kind"}),
            None,
        ),
        "invalid kind",
        "bad_kind",
    )
    _assert_refused(
        _signed_rec(
            json.dumps(
                {
                    "seq": 1,
                    "prev_hash": GRAPH_CHAIN_GENESIS,
                    "kind": "job_enqueued",
                    "job_id": "",
                }
            ),
            None,
        ),
        "missing a valid job_id",
        "no_job_id",
    )
    _assert_refused(
        _signed_rec(
            json.dumps(
                {
                    "seq": 1,
                    "prev_hash": GRAPH_CHAIN_GENESIS,
                    "kind": "job_reserved",
                    "job_id": "j1",
                    "attempt": True,
                }
            ),
            None,
        ),
        "invalid attempt",
        "bad_attempt",
    )
    _assert_refused(
        _signed_rec(
            json.dumps(
                {
                    "seq": 1,
                    "prev_hash": GRAPH_CHAIN_GENESIS,
                    "kind": "job_enqueued",
                    "job_id": "j1",
                    "attempt": None,
                }
            ),
            "wrong:dedupe:key",
        ),
        "dedupe_key mismatch",
        "bad_dedupe",
    )

    # 6. Direct-SQL column/JSON disagreement in CausalJournal SQLite store
    bad_store_path = tmp_path / "tampered_store.sqlite"
    bad_journal = CausalJournal(bad_store_path)
    bad_journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id="job-real",
            job_type="render",
            idempotency_key="k-real",
            attempt=None,
            status="pending",
            occurred_at="2026-11-24T00:00:00Z",
        )
    )
    with sqlite3.connect(str(bad_store_path)) as conn:
        conn.execute("UPDATE nexus_causal_journal SET job_id = 'job-spoofed' WHERE seq = 1")
        conn.commit()
    target_g = CreativeGraph(tmp_path / "target_store_refuse.sqlite")
    with pytest.raises(GraphIntegrityError, match="indexed columns disagree"):
        target_g.rebuild_from_journal_store(bad_journal)
    assert target_g.counts() == {"nodes": 0, "edges": 0, "history": 0, "chain": 0}

    # Unreadable or corrupt-JSON store refused too
    corrupt_json_path = tmp_path / "corrupt_json_store.sqlite"
    corrupt_journal = CausalJournal(corrupt_json_path)
    corrupt_journal.append(
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
    with sqlite3.connect(str(corrupt_json_path)) as conn:
        conn.execute("UPDATE nexus_causal_journal SET record_json = '{bad' WHERE seq = 1")
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="corrupt record_json"):
        target_g.rebuild_from_journal_store(corrupt_journal)

    with sqlite3.connect(str(corrupt_json_path)) as conn:
        conn.execute("UPDATE nexus_causal_journal SET record_json = '[1,2]' WHERE seq = 1")
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="not an object"):
        target_g.rebuild_from_journal_store(corrupt_journal)

    not_journal_path = tmp_path / "not_journal.sqlite"
    not_journal = CausalJournal(not_journal_path)
    with sqlite3.connect(str(not_journal_path)) as conn:
        conn.execute("DROP TABLE nexus_causal_journal")
        conn.commit()
    with pytest.raises(GraphIntegrityError, match="unreadable causal journal"):
        target_g.rebuild_from_journal_store(not_journal)


def test_rebuild_from_journal_honest_verification_and_passport_semantics(tmp_path: Path) -> None:
    """F04: Honest verification initiation vs authoritative verification & passport facts."""
    # (a) Job with JOB_VERIFICATION_STARTED only -> HYPOTHESIS verification, no verified link
    j_started = CausalJournal(tmp_path / "started.sqlite")
    for kind, attempt, status in (
        (EventKind.JOB_ENQUEUED, None, "pending"),
        (EventKind.JOB_RESERVED, 1, "processing"),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying"),
    ):
        j_started.append(
            CausalEvent(
                kind=kind,
                job_id="job-started",
                job_type="creative.render",
                idempotency_key="k-started",
                attempt=attempt,
                status=status,
                occurred_at="2026-11-24T00:00:00Z",
            )
        )
    g_started = CreativeGraph(tmp_path / "g_started.sqlite")
    res_a = g_started.rebuild_from_journal_store(j_started)
    assert res_a["projected_nodes"] == {
        "EXECUTION": 2,
        "ARTIFACT": 0,
        "VERIFICATION": 1,
        "PASSPORT": 0,
    }
    assert res_a["projected_edges"] == 0
    ver_id_a = node_identity("VERIFICATION", {"job_id": "job-started", "attempt": 1})
    ver_node_a = g_started.get_node(ver_id_a)
    assert ver_node_a is not None
    assert ver_node_a.epistemic_status is EpistemicStatus.HYPOTHESIS
    assert ver_node_a.payload["verification_state"] == "started"
    assert ver_node_a.payload["verified"] is False
    assert g_started.integrity_check().ok

    # (b) JOB_COMPLETED without authoritative passport/verification -> NO fabricated PASSPORT
    j_unverified = CausalJournal(tmp_path / "unverified.sqlite")
    art_digest_b = _sha256(b"unverified-art")
    for kind, attempt, status, res_d in (
        (EventKind.JOB_ENQUEUED, None, "pending", None),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying", None),
        (EventKind.JOB_COMPLETED, 1, "completed", art_digest_b),
    ):
        j_unverified.append(
            CausalEvent(
                kind=kind,
                job_id="job-unver",
                job_type="creative.render",
                idempotency_key="k-unver",
                attempt=attempt,
                status=status,
                result_digest=res_d,
                occurred_at="2026-11-24T00:00:00Z",
            )
        )
    bad_facts = {
        "job-unver": _sample_job_facts(
            "job-unver",
            result_digest=art_digest_b,
            verification={"passed": False, "status": "failed"},
            artifact_passport={"status": "compromised"},
            artifact_passport_known=True,
        )
    }
    g_unver = CreativeGraph(tmp_path / "g_unver.sqlite")
    res_b = g_unver.rebuild_from_journal_store(j_unverified, job_facts=bad_facts)
    assert res_b["projected_nodes"] == {
        "EXECUTION": 2,
        "ARTIFACT": 1,
        "VERIFICATION": 1,
        "PASSPORT": 0,
    }
    assert res_b["projected_edges"] == 1
    art_id_b = node_identity("ARTIFACT", {"job_id": "job-unver", "result_digest": art_digest_b})
    art_node_b = g_unver.get_node(art_id_b)
    assert art_node_b is not None
    assert art_node_b.epistemic_status is EpistemicStatus.HYPOTHESIS
    assert art_node_b.payload["verification_state"] == "unverified"
    assert art_node_b.payload["passport_present"] is False
    assert g_unver.edges(art_id_b, direction="out", relation="ARTIFACT_VERIFIED_BY") == []
    assert g_unver.edges(art_id_b, direction="out", relation="ARTIFACT_HAS_PASSPORT") == []
    assert g_unver.integrity_check().ok

    # (c) Full authoritative verification + passport JobFacts -> creates verified nodes & edges
    j_full = CausalJournal(tmp_path / "full.sqlite")
    art_digest_c = _sha256(b"rendered-movie")
    for kind, attempt, status, res_d in (
        (EventKind.JOB_ENQUEUED, None, "pending", None),
        (EventKind.JOB_RESERVED, 1, "processing", None),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying", None),
        (EventKind.JOB_COMPLETED, 1, "completed", art_digest_c),
    ):
        j_full.append(
            CausalEvent(
                kind=kind,
                job_id="job-42",
                job_type="creative.render",
                idempotency_key="k-42",
                attempt=attempt,
                status=status,
                result_digest=res_d,
                occurred_at="2026-11-24T00:00:00Z",
            )
        )
    auth_facts = {
        "job-42": _sample_job_facts(
            "job-42",
            result_digest=art_digest_c,
            verification={"passed": True, "status": "verified", "verifier": "veritas"},
            artifact_passport={"status": "verified", "artifact_digest": art_digest_c},
            artifact_passport_known=True,
        )
    }

    graph = CreativeGraph(tmp_path / "projected.sqlite")
    first = graph.rebuild_from_journal_store(j_full, job_facts=auth_facts)
    assert first["projected_nodes"] == {
        "EXECUTION": 2,
        "ARTIFACT": 1,
        "VERIFICATION": 1,
        "PASSPORT": 1,
    }
    assert first["projected_edges"] == 3
    assert "PROJECT" in first["unmigrated_levels"]
    ver_id_c = node_identity("VERIFICATION", {"job_id": "job-42", "attempt": 1})
    ver_node_c = graph.get_node(ver_id_c)
    assert ver_node_c is not None
    assert ver_node_c.epistemic_status is EpistemicStatus.MEASURED_EVIDENCE
    assert ver_node_c.payload["verified"] is True
    assert graph.integrity_check().ok

    # Idempotent second rebuild creates zero new nodes/edges
    second = graph.rebuild_from_journal_store(j_full, job_facts=auth_facts)
    assert second["projected_nodes"] == {
        "EXECUTION": 0,
        "ARTIFACT": 0,
        "VERIFICATION": 0,
        "PASSPORT": 0,
    }
    assert second["projected_edges"] == 0

    # Mark passport terminal, then replace=True rebuild preserves terminal & restores edge
    art_id = node_identity("ARTIFACT", {"job_id": "job-42", "result_digest": art_digest_c})
    pass_id = node_identity("PASSPORT", {"job_id": "job-42", "result_digest": art_digest_c})
    graph.set_status(pass_id, "revoked")

    rebuilt = graph.rebuild_from_journal_store(j_full, replace=True, job_facts=auth_facts)
    assert any(item["node_id"] == pass_id for item in rebuilt["skipped_terminal"])
    passport_node = graph.get_node(pass_id)
    assert passport_node is not None and passport_node.status == "revoked"
    assert len(graph.edges(art_id, direction="out", relation="ARTIFACT_HAS_PASSPORT")) == 1
    assert graph.integrity_check().ok

    # Passport-only authoritative facts -> passport_attested without verified link
    passport_only_facts = {
        "job-42": _sample_job_facts(
            "job-42",
            result_digest=art_digest_c,
            verification=None,
            artifact_passport={"status": "verified", "artifact_digest": art_digest_c},
            artifact_passport_known=True,
        )
    }
    g_pass_only = CreativeGraph(tmp_path / "pass_only.sqlite")
    res_po = g_pass_only.rebuild_from_journal_store(j_full, job_facts=passport_only_facts)
    assert res_po["projected_nodes"]["PASSPORT"] == 1
    assert res_po["projected_edges"] == 2
    po_art = g_pass_only.get_node(art_id)
    assert po_art is not None and po_art.payload["verification_state"] == "passport_attested"

    # Status-only verification ({"status": "verified"} without "passed" boolean) + unknown passport
    status_only_facts = {
        "job-42": _sample_job_facts(
            "job-42",
            result_digest=art_digest_c,
            verification={"status": "verified"},
            artifact_passport={"status": "verified"},
            artifact_passport_known=False,
        )
    }
    g_status_only = CreativeGraph(tmp_path / "status_only.sqlite")
    res_so = g_status_only.rebuild_from_journal_store(j_full, job_facts=status_only_facts)
    assert res_so["projected_nodes"]["PASSPORT"] == 0
    assert res_so["projected_edges"] == 2  # EXECUTION_PRODUCED_ARTIFACT + ARTIFACT_VERIFIED_BY

    # Empty journal rebuild + nonexistent journal file path succeed cleanly
    empty_graph = CreativeGraph(tmp_path / "empty_rebuild.sqlite")
    empty_res = empty_graph.rebuild_from_journal([])
    assert empty_res["records_read"] == 0
    assert empty_res["projected_edges"] == 0
    uncreated_journal = CausalJournal(tmp_path / "unlinked_journal.sqlite")
    uncreated_journal.db_path.unlink()
    CreativeGraph._validate_journal_store_columns(uncreated_journal)


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
        secret_key=_TEST_SECRET_KEY,
        key_id=_TEST_KEY_ID,
        subject_id="n",
        subject_digest=_sha256(b"a"),
        verifier_id="v",
        evidence_digest=_sha256(b"e"),
        issued_at_utc="2026-11-24T00:00:00Z",
    ).startswith("hmac-sha256:")


def test_p3_restore_uses_readonly_mode_without_mutating_backup_or_sidecars(
    tmp_path: Path,
) -> None:
    """Refinement 1A: restore() opens source via SQLite mode=ro URI without sidecars."""
    src_graph = CreativeGraph(tmp_path / "src.sqlite")
    proj = src_graph.put_node("PROJECT", {"id": "p-ro"}).node
    assert proj is not None
    backup_path = tmp_path / "readonly_backup.sqlite"
    src_graph.backup(backup_path)
    with sqlite3.connect(backup_path) as bak_conn:
        bak_conn.execute("PRAGMA journal_mode = DELETE")
        bak_conn.commit()
    before_bytes = backup_path.read_bytes()
    backup_path.chmod(0o444)
    try:
        dst_graph = CreativeGraph(tmp_path / "dst.sqlite")
        dst_graph.restore(backup_path)
        assert dst_graph.get_node(proj.node_id) is not None
        assert backup_path.read_bytes() == before_bytes
        assert not (tmp_path / "readonly_backup.sqlite-wal").exists()
        assert not (tmp_path / "readonly_backup.sqlite-shm").exists()
    finally:
        backup_path.chmod(0o644)

    missing = tmp_path / "does_not_exist_ro.sqlite"
    with pytest.raises(GraphStateError, match="not a readable database"):
        dst_graph.restore(missing)
    assert not missing.exists()


def test_p3_promote_epistemic_status_rejects_terminal_nodes_and_edges(
    tmp_path: Path,
) -> None:
    """Refinement 1B: promote_epistemic_status refuses nodes/edges in terminal status."""
    graph = CreativeGraph(
        tmp_path / "promote_terminal.sqlite", proof_verification_keys=_TEST_KEY_RING
    )
    for idx, terminal_status in enumerate(("superseded", "invalidated", "revoked")):
        res = graph.put_node(
            "INTENT",
            {"id": f"intent-{idx}"},
            {"goal": f"g-{idx}"},
            epistemic_status=EpistemicStatus.HYPOTHESIS,
        )
        assert res.node is not None
        graph.set_status(res.node.node_id, terminal_status)
        with pytest.raises(GraphStateTransitionError, match="terminal is absorbing"):
            graph.promote_epistemic_status(
                res.node.node_id,
                EpistemicStatus.MEASURED_EVIDENCE,
                evidence_digest=_sha256(b"ev"),
            )

    p = graph.put_node("PROJECT", {"id": "p-edge"}).node
    i = graph.put_node("INTENT", {"id": "i-edge"}).node
    assert p is not None and i is not None
    edge = graph.put_edge(
        "PROJECT_HAS_INTENT",
        p.node_id,
        i.node_id,
        epistemic_status=EpistemicStatus.HYPOTHESIS,
    ).edge
    assert edge is not None
    graph.revoke_edge(edge.edge_id)
    with pytest.raises(GraphStateTransitionError, match="terminal is absorbing"):
        graph.promote_epistemic_status(
            edge.edge_id,
            EpistemicStatus.MEASURED_EVIDENCE,
            evidence_digest=_sha256(b"ev"),
            is_edge=True,
        )


# --------------------------------------------------------------------------- #
# 12. W11X CP-1 hardening: read-only restore, terminal epistemic guard,
#     and revision_count <-> chain parity (GRAPH034)
# --------------------------------------------------------------------------- #
def test_restore_opens_backup_source_read_only(tmp_path: Path) -> None:
    source = CreativeGraph(tmp_path / "restore_source.sqlite")
    source.put_node("PROJECT", {"id": "p1"}, {"v": 1})
    backup_path = source.backup(tmp_path / "restore_backup.sqlite")
    before_digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    before_mtime = backup_path.stat().st_mtime_ns

    target = CreativeGraph(tmp_path / "restore_target.sqlite")
    target.restore(backup_path)
    assert target.counts()["nodes"] == 1

    # 1A: the backup file itself is never mutated by a restore. SQLite may attach
    # read-only -shm/-wal companions because the backup keeps its WAL header, but
    # the database bytes and mtime must be byte-for-byte unchanged.
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == before_digest
    assert backup_path.stat().st_mtime_ns == before_mtime

    # A write-protected backup still restores: the source is opened with mode=ro.
    backup_path.chmod(0o444)
    try:
        other = CreativeGraph(tmp_path / "restore_target_ro.sqlite")
        other.restore(backup_path)
        assert other.counts()["nodes"] == 1
    finally:
        backup_path.chmod(0o644)

    # A read-write connection would CREATE a missing source as an empty database.
    # Read-only mode refuses instead, so a typo'd path can never leave a stub file.
    missing = tmp_path / "does_not_exist.sqlite"
    with pytest.raises(GraphStateError, match="not a readable database"):
        target.restore(missing)
    assert not missing.exists()


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_epistemic_promotion_refused_on_terminal_node(tmp_path: Path, terminal_status: str) -> None:
    graph = CreativeGraph(tmp_path / f"terminal_{terminal_status}.sqlite")
    node = graph.put_node("PROJECT", {"id": "p1"}, {"v": 1}).node
    assert node is not None
    graph.set_status(node.node_id, terminal_status)

    with pytest.raises(GraphStateTransitionError, match="terminal is absorbing"):
        graph.promote_epistemic_status(
            node.node_id,
            EpistemicStatus.MEASURED_EVIDENCE,
            evidence_digest=_sha256(b"evidence"),
        )
    # The refusal is fail-closed: the node keeps its epistemic status and the
    # append-only chain gained no entry.
    reloaded = graph.get_node(node.node_id)
    assert reloaded is not None
    assert reloaded.epistemic_status is EpistemicStatus.HYPOTHESIS
    assert graph.verify_chain() == []
    assert graph.integrity_check().ok


def test_epistemic_promotion_refused_on_revoked_edge(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "revoked_edge.sqlite")
    src = graph.put_node("PROJECT", {"id": "p1"}).node
    dst = graph.put_node("INTENT", {"id": "i1"}).node
    assert src is not None and dst is not None
    edge = graph.put_edge("PROJECT_HAS_INTENT", src.node_id, dst.node_id, {"w": 1}).edge
    assert edge is not None
    graph.revoke_edge(edge.edge_id)

    with pytest.raises(GraphStateTransitionError, match="terminal is absorbing"):
        graph.promote_epistemic_status(
            edge.edge_id, EpistemicStatus.MEASURED_EVIDENCE, is_edge=True
        )
    assert graph.integrity_check().ok


def test_revision_count_chain_parity_is_enforced_graph034(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "parity.sqlite")
    node = graph.put_node("PROJECT", {"id": "p1"}, {"v": 1}).node
    assert node is not None
    graph.put_node("PROJECT", {"id": "p1"}, {"v": 2})
    graph.put_node("PROJECT", {"id": "p1"}, {"v": 3})
    reloaded = graph.get_node(node.node_id)
    assert reloaded is not None
    assert reloaded.revision_count == 3

    # A consistent graph reports no parity finding.
    clean = graph.integrity_check()
    assert clean.ok
    assert "GRAPH034" not in {f.code for f in clean.findings}

    # 1C: revision_count is not covered by the hash chain, so a direct-SQL drift
    # is invisible to chain verification and must be caught by parity alone.
    for delta in (1, -1):
        tampered = tmp_path / f"parity_tampered_{delta}.sqlite"
        graph.backup(tampered)
        with sqlite3.connect(str(tampered)) as conn:
            conn.execute(
                "UPDATE nexus_graph_node SET revision_count = revision_count + ? WHERE node_id = ?",
                (delta, node.node_id),
            )
            conn.commit()
        report = CreativeGraph(tampered, verify_on_open=False).integrity_check()
        codes = {f.code for f in report.findings}
        assert "GRAPH034" in codes
        assert report.ok is False
