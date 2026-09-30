"""Intent, creative-graph, plan and lineage/evidence models (D-0024 backbone).

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


NodeKind = Literal["intent", "capability", "artifact", "evidence", "reference", "decision"]


class GraphNode(BaseModel):
    """One node of the creative graph; edges point from a node to its parents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(default_factory=lambda: _new_id("node"))
    kind: NodeKind
    label: str = Field(default="", max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)
    parents: tuple[str, ...] = ()


class CreativeGraph:
    """Append-only creative graph: the project's durable memory.

    A node records what happened (an intent, the capability chosen, the artifact
    produced, its evidence) and points at its parents. Nothing is ever mutated
    in place, so the lineage of any artifact is the transitive closure of its
    parent edges -- the basis for "which artifacts does this change invalidate".
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
