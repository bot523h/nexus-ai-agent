"""Intent, creative-graph, plan and lineage/evidence models (D-0025 backbone).

These are the *data* half of the creative execution spine. They are pure
pydantic models plus one in-memory append-only graph; nothing here executes a
command or touches I/O.

Vocabulary (matches ``docs/architecture/CREATIVE_DIRECTION.md``):

* :class:`Intent` -- a structured statement of what the user wants. Free text
  is a *source*, never the executed artifact; a resolver turns it into an Intent.
* :class:`CompiledIntent` / :class:`PlannedOperation` -- the compiler's mapping
  from intent to the registry operations that satisfy it.
* :class:`CreativeGraph` -- the durable memory: intent, capability, artifact,
  evidence and reference nodes joined by parent edges, so a later change can be
  traced to the artifacts it invalidates.
* :class:`ArtifactRecord` / :class:`EvidenceRecord` -- what was produced and the
  measured proof that it was produced by that transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class SpineError(Exception):
    """Base error for the creative execution spine."""


class IntentError(SpineError):
    """A free-text request could not be resolved into a structured intent."""


class CompilationError(SpineError):
    """An intent could not be compiled into registry operations."""


class GraphError(SpineError):
    """The creative graph was asked for a node it does not contain."""


class SpineRollbackError(SpineError):
    """A failed multi-step run could not be rolled back through the bus.

    The spine is transactional: when a later step of a plan is refused it undoes
    the steps already applied *through the bus's own* ``system.undo`` path. If
    that undo itself fails, the state may be partially applied, so the failure is
    raised loudly rather than masked by the original error.
    """


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


class Intent(BaseModel):
    """A structured statement of user intent (the spine's only input)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(default_factory=lambda: _new_id("int"))
    project_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2000)
    constraints: tuple[str, ...] = ()
    source_text: str | None = Field(default=None, max_length=4000)
    source: Literal["user", "reference", "service"] = "user"


@runtime_checkable
class IntentResolver(Protocol):
    """Port: free text -> :class:`Intent`. An LLM implementation is a drop-in."""

    def resolve(self, text: str, *, project_id: str) -> Intent: ...


class RulesIntentResolver:
    """Deterministic resolver: no LLM, no network.

    It keeps the whole free-text request as ``goal`` and extracts a small set of
    explicit constraints. It is deliberately conservative: it does not invent a
    plan, it only structures the request. Intent *understanding* beyond this is
    the boundary where an LLM resolver plugs in behind :class:`IntentResolver`.
    """

    _CONSTRAINT_MARKERS = ("بدون", "حذف", "ممنوع", "no ", "without ", "do not ")

    def resolve(self, text: str, *, project_id: str) -> Intent:
        goal = " ".join(text.split())
        if not goal:
            raise IntentError("cannot resolve an empty request")
        constraints: list[str] = []
        lowered = goal.lower()
        for marker in self._CONSTRAINT_MARKERS:
            if marker in lowered:
                constraints.append(f"respect constraint near {marker!r}")
        return Intent(
            project_id=project_id,
            goal=goal,
            constraints=tuple(constraints),
            source_text=text,
        )


# ---------------------------------------------------------------------------
# Compiled plan
# ---------------------------------------------------------------------------


class PlannedOperation(BaseModel):
    """One registry operation the compiler decided an intent needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)


class CompiledIntent(BaseModel):
    """The compiler output: an ordered, registry-checked list of operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    operations: tuple[str, ...]
    plan: tuple[PlannedOperation, ...] = ()
    rationale: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# Artifact / evidence
# ---------------------------------------------------------------------------


class ArtifactRecord(BaseModel):
    """A produced artifact and its content identity (measured, not assumed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(default_factory=lambda: _new_id("art"))
    kind: str = Field(min_length=1, max_length=128)
    content_hash: str
    produced_by_transaction: str
    state_revision: int = Field(ge=0)
    node_id: str
    output: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(BaseModel):
    """The proof that binds an artifact to the command/transaction that made it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(default_factory=lambda: _new_id("ev"))
    artifact_id: str
    command_id: str
    operation: str
    transaction_id: str
    state_hash: str
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Creative graph (append-only memory)
# ---------------------------------------------------------------------------


NodeKind = Literal[
    "intent",
    "creative_ir",
    "compiled_plan",
    "command_dag",
    "capability",
    "artifact",
    "evidence",
    "reference",
    "decision",
]


class GraphNode(BaseModel):
    """One node of the creative graph; edges point from a node to its parents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(default_factory=lambda: _new_id("node"))
    kind: NodeKind
    label: str = Field(default="", max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)
    parents: tuple[str, ...] = ()


class ProvenanceNodeSpec(BaseModel):
    """Immutable bridge DTO mapping a planning node to durable CreativeGraph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spine_node_id: str
    project_id: str
    kind: NodeKind
    label: str
    logical_entity_id: str
    revision_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_spine_ids: tuple[str, ...] = ()
    epistemic_status: Literal["HYPOTHESIS", "MEASURED_EVIDENCE", "VERIFIED_COMMIT"] = "HYPOTHESIS"


class CreativePlanGraph(BaseModel):
    """Immutable compile-time planning DTO (non-authoritative projection)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    nodes: tuple[GraphNode, ...] = ()
    intent: Intent | None = None
    compiled_intent: CompiledIntent | None = None
    plan_digest: str


@runtime_checkable
class DurableGraphPort(Protocol):
    """Structural protocol satisfied by ``provenance.graph.CreativeGraph``."""

    def put_node(
        self,
        *,
        kind: str,
        identity: dict[str, Any],
        payload: dict[str, Any],
        logical_entity_id: str | None = None,
        revision_id: str | None = None,
        source: str = "creative_spine",
    ) -> Any: ...

    def put_edge(
        self,
        *,
        relation: str,
        src: str,
        dst: str,
        payload: dict[str, Any] | None = None,
        source: str = "creative_spine",
    ) -> Any: ...

    def nodes_for_project(self, project_id: str) -> list[Any]: ...


class CreativeGraph:
    """Compile-time planning graph DTO; durable authority lives in provenance.graph.

    A node records planning lineage (intent, typed creative IR, compiled plan,
    command DAG, capability, artifact, evidence) and points at its parents.
    Use :meth:`to_provenance_records` and :meth:`sync_to_durable_graph` to
    persist into the single durable :class:`provenance.graph.CreativeGraph`.
    """

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id
        self._nodes: dict[str, GraphNode] = {}
        self._children: dict[str, list[str]] = {}

    @property
    def project_id(self) -> str:
        return self._project_id

    def add_node(
        self,
        kind: NodeKind,
        *,
        label: str = "",
        data: dict[str, Any] | None = None,
        parents: tuple[str, ...] = (),
    ) -> GraphNode:
        for parent in parents:
            if parent not in self._nodes:
                raise GraphError(f"unknown parent node: {parent!r}")
        node = GraphNode(kind=kind, label=label, data=data or {}, parents=tuple(parents))
        self._nodes[node.node_id] = node
        for parent in parents:
            self._children.setdefault(parent, []).append(node.node_id)
        return node

    def node(self, node_id: str) -> GraphNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise GraphError(f"unknown node: {node_id!r}") from None

    def remove_node(self, node_id: str) -> None:
        """Retract a leaf node that was never committed.

        Used by the spine to retract artifact nodes written during a run whose
        commands the bus then rolled back, so a failed run leaves no artifact in
        the graph. Only a childless node may be removed; the caller removes
        newest-first, which keeps every removal a leaf removal.
        """
        node = self.node(node_id)
        if self._children.get(node_id):
            raise GraphError(f"cannot remove a node that still has children: {node_id!r}")
        for parent in node.parents:
            siblings = self._children.get(parent)
            if siblings is not None:
                self._children[parent] = [child for child in siblings if child != node_id]
        del self._nodes[node_id]

    def nodes(self) -> tuple[GraphNode, ...]:
        return tuple(self._nodes.values())

    def parents(self, node_id: str) -> tuple[str, ...]:
        return self.node(node_id).parents

    def children(self, node_id: str) -> tuple[str, ...]:
        return tuple(self._children.get(node_id, ()))

    def descendants(self, node_id: str) -> tuple[str, ...]:
        """Every node reachable from *node_id* following child edges (BFS, stable)."""
        self.node(node_id)  # existence check
        seen: list[str] = []
        queue = list(self._children.get(node_id, ()))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.append(current)
            queue.extend(self._children.get(current, ()))
        return tuple(seen)

    def to_plan_graph(
        self,
        *,
        intent: Intent | None = None,
        compiled_intent: CompiledIntent | None = None,
    ) -> CreativePlanGraph:
        """Freeze this planning graph into an immutable compile-time DTO."""
        nodes = tuple(self._nodes.values())
        digest = _content_hash(
            {
                "project_id": self._project_id,
                "nodes": [n.model_dump(mode="json") for n in nodes],
                "intent": intent.model_dump(mode="json") if intent is not None else None,
                "compiled_intent": (
                    compiled_intent.model_dump(mode="json") if compiled_intent is not None else None
                ),
            }
        )
        return CreativePlanGraph(
            project_id=self._project_id,
            nodes=nodes,
            intent=intent,
            compiled_intent=compiled_intent,
            plan_digest=digest,
        )

    def to_provenance_records(self) -> tuple[ProvenanceNodeSpec, ...]:
        """Convert planning nodes into deterministic provenance-ready records."""
        records: list[ProvenanceNodeSpec] = []
        for node in self._nodes.values():
            payload = dict(node.data)
            payload["project_id"] = self._project_id
            payload["label"] = node.label
            payload["spine_node_id"] = node.node_id
            records.append(
                ProvenanceNodeSpec(
                    spine_node_id=node.node_id,
                    project_id=self._project_id,
                    kind=node.kind,
                    label=node.label,
                    logical_entity_id=f"{node.kind}:{self._project_id}:{node.node_id}",
                    revision_id=node.node_id,
                    payload=payload,
                    parent_spine_ids=node.parents,
                    epistemic_status="HYPOTHESIS",
                )
            )
        return tuple(records)

    def sync_to_durable_graph(
        self,
        durable_graph: DurableGraphPort,
        *,
        source: str = "creative_spine",
    ) -> dict[str, str]:
        """Persist planning nodes and parent edges into the durable CreativeGraph."""
        kind_map: dict[str, str] = {
            "intent": "INTENT",
            "creative_ir": "REVISION",
            "compiled_plan": "PLAN",
            "capability": "PLAN",
            "command_dag": "COMMAND",
            "artifact": "ARTIFACT",
            "evidence": "EVIDENCE",
            "decision": "DECISION",
            "reference": "DECISION",
        }
        relation_map: dict[tuple[str, str], str] = {
            ("INTENT", "REVISION"): "INTENT_COMPILED_TO_REVISION",
            ("REVISION", "PLAN"): "REVISION_HAS_PLAN",
            ("PLAN", "COMMAND"): "PLAN_EMITS_COMMAND",
            ("COMMAND", "ARTIFACT"): "EXECUTION_PRODUCED_ARTIFACT",
            ("ARTIFACT", "EVIDENCE"): "ARTIFACT_VERIFIED_BY",
            ("DECISION", "ARTIFACT"): "DECISION_AFFECTS",
        }
        id_map: dict[str, str] = {}
        durable_kinds: dict[str, str] = {}
        for spec in self.to_provenance_records():
            durable_kind = kind_map.get(spec.kind, "DECISION")
            payload = dict(spec.payload)
            payload["spine_kind"] = spec.kind
            result = durable_graph.put_node(
                kind=durable_kind,
                identity={
                    "project_id": spec.project_id,
                    "spine_node_id": spec.spine_node_id,
                },
                payload=payload,
                logical_entity_id=spec.logical_entity_id,
                revision_id=spec.revision_id,
                source=source,
            )
            node_obj = (
                result.node if hasattr(result, "node") and result.node is not None else result
            )
            durable_id = str(node_obj.node_id)
            id_map[spec.spine_node_id] = durable_id
            durable_kinds[spec.spine_node_id] = durable_kind
        for spec in self.to_provenance_records():
            dst_id = id_map[spec.spine_node_id]
            dst_kind = durable_kinds[spec.spine_node_id]
            for parent_spine_id in spec.parent_spine_ids:
                src_id = id_map[parent_spine_id]
                src_kind = durable_kinds[parent_spine_id]
                relation = relation_map.get((src_kind, dst_kind), "ARTIFACT_DERIVED_FROM")
                durable_graph.put_edge(
                    relation=relation,
                    src=src_id,
                    dst=dst_id,
                    payload={"project_id": self._project_id},
                    source=source,
                )
        return id_map

    @classmethod
    def from_durable_graph(
        cls, durable_graph: DurableGraphPort, *, project_id: str
    ) -> CreativeGraph:
        """Project durable CreativeGraph nodes for ``project_id`` as a planning DTO."""
        graph = cls(project_id)
        allowed_kinds = {
            "intent",
            "creative_ir",
            "compiled_plan",
            "command_dag",
            "capability",
            "artifact",
            "evidence",
            "reference",
            "decision",
        }
        reverse_kind_map = {
            "INTENT": "intent",
            "REVISION": "creative_ir",
            "PLAN": "compiled_plan",
            "COMMAND": "command_dag",
            "ARTIFACT": "artifact",
            "EVIDENCE": "evidence",
            "DECISION": "decision",
        }
        for durable_node in durable_graph.nodes_for_project(project_id):
            raw_payload = getattr(durable_node, "payload", {})
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            raw_kind = str(
                payload.get("spine_kind")
                or reverse_kind_map.get(str(getattr(durable_node, "kind", "")), "decision")
            )
            kind: NodeKind = raw_kind if raw_kind in allowed_kinds else "decision"  # type: ignore[assignment]
            label = str(payload.get("label") or getattr(durable_node, "logical_entity_id", ""))
            node = GraphNode(
                node_id=str(durable_node.node_id),
                kind=kind,
                label=label[:500],
                data=payload,
                parents=(),
            )
            graph._nodes[node.node_id] = node
        return graph
