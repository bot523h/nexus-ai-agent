"""Typed append-only causal graph projection for Nagar P0.

Persistence is delegated to the existing provenance journal; this module enforces
node/edge shape, scope, stable IDs, and fail-closed cause queries before recording.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(str, Enum):
    USER_INTENT = "UserIntent"
    EVIDENCE = "Evidence"
    SEMANTIC_SEGMENT = "SemanticSegment"
    PROPOSAL = "Proposal"
    POLICY_DECISION = "PolicyDecision"
    OPERATION = "Operation"
    JOB = "Job"
    ARTIFACT = "Artifact"
    VERIFICATION = "Verification"
    UNDO = "Undo/Compensation"
    USER_FEEDBACK = "UserFeedback"


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    kind: NodeKind
    project_id: str
    tenant_id: str
    schema_version: str = "nagar.causal.v1"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    provenance: str
    parent_id: str | None = None
    retention: str = "derived_reference"
    payload: dict[str, Any] = {}


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    edge_id: str
    relation: str = Field(pattern=r"^[a-z][a-z_]{2,31}$")
    source_id: str
    target_id: str
    project_id: str
    tenant_id: str
    schema_version: str = "nagar.causal.v1"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CausalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "nagar.receipt.v1"
    intent_id: str
    operation_id: str
    job_id: str | None = None
    artifact_id: str | None = None
    verification_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    status: str
    compensation_id: str | None = None


class CausalGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}

    @staticmethod
    def stable_id(kind: str, value: object) -> str:
        raw = json.dumps(
            {"kind": kind, "value": value},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return f"{kind.lower()}_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    def add_node(self, node: GraphNode) -> GraphNode:
        prior = self._nodes.get(node.node_id)
        if prior is not None and prior != node:
            raise ValueError("node_id_conflict")
        self._nodes[node.node_id] = node
        return node

    def add_edge(self, edge: GraphEdge) -> GraphEdge:
        source, target = self._nodes.get(edge.source_id), self._nodes.get(edge.target_id)
        if source is None or target is None:
            raise ValueError("edge_endpoint_missing")
        if (
            source.tenant_id != edge.tenant_id
            or target.tenant_id != edge.tenant_id
            or source.project_id != edge.project_id
            or target.project_id != edge.project_id
        ):
            raise PermissionError("cross_scope_edge")
        prior = self._edges.get(edge.edge_id)
        if prior is not None and prior != edge:
            raise ValueError("edge_id_conflict")
        self._edges[edge.edge_id] = edge
        return edge

    def nodes(self) -> tuple[GraphNode, ...]:
        return tuple(self._nodes.values())

    def edges(self) -> tuple[GraphEdge, ...]:
        return tuple(self._edges.values())

    def compensation_targets(self, *, intent_id: str, tenant_id: str) -> tuple[GraphNode, ...]:
        if intent_id not in self._nodes:
            return ()
        if self._nodes[intent_id].tenant_id != tenant_id:
            raise PermissionError("cross_tenant_query")
        target_ids = {
            edge.target_id
            for edge in self._edges.values()
            if edge.source_id == intent_id
            and edge.relation in {"motivated_by", "targets", "executed_as"}
        }
        return tuple(
            self._nodes[item] for item in target_ids if self._nodes[item].kind is NodeKind.OPERATION
        )

    def receipt(
        self,
        *,
        intent_id: str,
        operation_id: str,
        job_id: str | None,
        artifact_id: str | None,
        verification_id: str | None,
        evidence_refs: tuple[str, ...],
        status: str,
        artifact_evidence: dict[str, Any] | None = None,
        verifier_evidence: dict[str, Any] | None = None,
    ) -> CausalReceipt:
        if status == "VERIFIED_SUCCESS":
            if not artifact_id or not verification_id or not evidence_refs:
                raise ValueError("verified_receipt_requires_artifact_verification_evidence")
            if not artifact_evidence or not verifier_evidence:
                raise ValueError("verified_receipt_requires_live_evidence")
            artifact_sha = artifact_evidence.get("sha256")
            verifier_sha = verifier_evidence.get("sha256")
            if (
                artifact_evidence.get("status") != "present"
                or verifier_evidence.get("status") != "verified"
                or not isinstance(artifact_sha, str)
                or not artifact_sha.startswith("sha256:")
                or artifact_sha != verifier_sha
            ):
                raise ValueError("verified_receipt_evidence_mismatch")
        return CausalReceipt(
            intent_id=intent_id,
            operation_id=operation_id,
            job_id=job_id,
            artifact_id=artifact_id,
            verification_id=verification_id,
            evidence_refs=evidence_refs,
            status=status,
        )
