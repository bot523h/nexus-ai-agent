"""Canonical Agent Intent, typed plan, and request-local lineage models.

These are pure validated data structures plus one in-memory graph. Nothing
here executes a command, opens storage, or grants authority. Persisted snapshots
and execution evidence remain owned by the existing job queue.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpineError(Exception):
    """Base error for the creative execution spine."""


class IntentError(SpineError):
    """A free-text request could not be resolved into a structured intent."""


class CompilationError(SpineError):
    """An intent could not be compiled into registry operations."""


class GraphError(SpineError):
    """The creative graph was asked for a node it does not contain."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


class IntentSourceRange(BaseModel):
    """An explicit source-media span resolved from the user's request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_point_us: int = Field(ge=0)
    out_point_us: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> IntentSourceRange:
        if self.out_point_us <= self.in_point_us:
            raise ValueError("out_point_us must be greater than in_point_us")
        return self


class Intent(BaseModel):
    """The one canonical, validated statement of user meaning.

    ``IntentProposal`` at an LLM boundary is only a wire schema.  The trusted
    application stamps project/request identity and constructs this model; model
    output never supplies actor, authorization, operation IDs, or command IDs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    intent_id: str = Field(default="", max_length=128)
    request_id: str = Field(default="", max_length=256)
    project_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2000)
    objective: Literal["video_trim", "other", "unsupported"] = "other"
    source_range: IntentSourceRange | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    ambiguity_state: Literal["clear", "clarify", "unsupported"] = "clear"
    unresolved_requirements: tuple[str, ...] = ()
    semantic_intents: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    source_text: str | None = Field(default=None, max_length=4000)
    source: Literal["user", "reference", "service"] = "user"

    @model_validator(mode="after")
    def _stable_identity(self) -> Intent:
        if not self.intent_id:
            payload = self.model_dump(mode="json", exclude={"intent_id"})
            object.__setattr__(
                self, "intent_id", "int_" + _content_hash(payload).removeprefix("sha256:")
            )
        return self


# ---------------------------------------------------------------------------
# Compiled plan
# ---------------------------------------------------------------------------


class PlanProvenance(BaseModel):
    """A typed link from a compiled plan back to meaning-bearing evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["user", "reference", "strategy", "recipe", "system"]
    detail: str = Field(default="", max_length=500)
    reference_id: str | None = Field(default=None, max_length=256)


class AgentLineageRef(BaseModel):
    """Queue-persisted, non-authorizing references for an Agent render job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    intent_id: str = Field(min_length=1, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=128)
    work_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=256)
    source_asset_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_duration_us: int = Field(gt=0)
    expected_output_duration_us: int = Field(gt=0)
    intent_snapshot_json: str = Field(min_length=2, max_length=16_384)
    strategy_snapshot_json: str = Field(min_length=2, max_length=16_384)
    work_snapshot_json: str = Field(min_length=2, max_length=131_072)
    plan_snapshot_json: str = Field(min_length=2, max_length=65_536)
    max_replans: Literal[0] = 0


class PlannedOperation(BaseModel):
    """One registry operation with stable identity and explicit dependencies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(default="", max_length=128)
    command_id: str = Field(default="", max_length=256)
    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    provenance_refs: tuple[str, ...] = ()
    description: str = Field(default="", max_length=500)


class CompiledIntent(BaseModel):
    """Deterministic plan compiled from a sealed CreativeWork."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    operations: tuple[str, ...]
    plan: tuple[PlannedOperation, ...] = ()
    work_id: str = ""
    plan_id: str = ""
    constraint_ids: tuple[str, ...] = ()
    provenance: tuple[PlanProvenance, ...] = ()
    max_replans: int = Field(default=0, ge=0, le=3)
    rationale: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _consistent_dag(self) -> CompiledIntent:
        if self.plan and self.operations != tuple(step.operation for step in self.plan):
            raise ValueError("operations must exactly match the ordered plan steps")
        step_ids = [step.step_id for step in self.plan if step.step_id]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step IDs must be unique")
        step_id_set = set(step_ids)
        dependencies = {step.step_id: step.depends_on for step in self.plan if step.step_id}
        for step_id, parents in dependencies.items():
            unknown = set(parents) - step_id_set
            if unknown:
                raise ValueError(f"step {step_id!r} depends on unknown step(s): {sorted(unknown)}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("compiled plan dependency graph must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for parent in dependencies.get(step_id, ()):
                visit(parent)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in dependencies:
            visit(step_id)
        return self


# ---------------------------------------------------------------------------
# Request-local lineage graph (not a persistence authority)
# ---------------------------------------------------------------------------


NodeKind = Literal[
    "intent",
    "strategy",
    "creative_work",
    "plan",
    "capability",
    "job",
    "artifact",
    "verification",
    "receipt",
]


class GraphNode(BaseModel):
    """One node of the creative graph; edges point from a node to its parents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(default_factory=lambda: _new_id("node"))
    kind: NodeKind
    label: str = Field(default="", max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)
    parents: tuple[str, ...] = ()


class CreativeGraph:
    """Request-local append-only lineage graph, not a durable project store.

    Nodes record intent, strategy, work, plan, job, artifact, verification and
    receipt facts and point at their parents. They are immutable for this runtime
    only; the existing durable job queue owns persisted snapshots and execution
    evidence. The graph never opens storage or grants execution authority.
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
        """Retract a leaf from this in-memory graph before it is returned.

        This only removes graph metadata; it never compensates a queue job or a
        CommandBus operation. Only a childless node may be removed, so callers
        remove newest-first and keep every removal a leaf removal.
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
