"""Durable creative graph: the queryable past of the creative chain (law R21).

The repository already had three durable authorities, and this module replaces
none of them:

* ``nexus_job_queue`` — the sole job-state/outcome authority (law R14);
* ``nexus_causal_journal`` — the append-only hash-chained ledger of committed
  transitions, *evidence and never authority* (D-0024);
* ``ArtifactPassport`` — the read-only, fail-closed projection that reconciles
  the chain against the queue row and re-measures artifacts.

What was missing is the question those three cannot answer directly: **"where
did this artifact come from, and what else would be affected if its intent
changed?"** The ledger can answer it only by scanning every record; the queue
row knows one job; the passport knows one artifact. This module is the durable,
indexed, **rebuildable projection** that makes the answer a query.

Authority rules, enforced by ``tests/architecture/test_creative_graph_boundary.py``:

* the graph is a projection. It is never the source of truth for job state,
  command state, passports or git, and it owns no execution path;
* it is rebuildable from the canonical sources — :meth:`CreativeGraph.rebuild_from_journal`
  reconstructs everything the ledger can witness and reports, honestly, what it
  cannot (``UNMIGRATED``, never guessed);
* identity is deterministic: creating the same logical state twice yields the
  same node, so two runs cannot produce two contradictory truths;
* history is never rewritten: a changed payload appends a revision row and marks
  the previous state ``superseded``. ``invalidate`` / ``revoke`` / ``supersede``
  are explicit transitions, not ``UPDATE history = new_truth``.

Persian note: گراف خلاق جای هیچ authority دیگری را نمی‌گیرد؛ projection
بازساختنی است. هویت deterministic است و تاریخچه هرگز بازنویسی نمی‌شود —
تغییر یعنی append + supersede، نه UPDATE.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_ai_agent.provenance.models import (
    LedgerRecord,
    canonical_json,
    digest_of,
)

#: Node kinds the creative domain actually needs. Deliberately no more: a kind
#: nobody writes is a kind nobody can verify.
NODE_KINDS: tuple[str, ...] = (
    "PROJECT",
    "INTENT",
    "REVISION",
    "DECISION",
    "PLAN",
    "COMMAND",
    "EXECUTION",
    "ARTIFACT",
    "EVIDENCE",
    "VERIFICATION",
    "PASSPORT",
    "ACTOR",
)

#: Relations that carry real domain semantics. ``ARTIFACT_DEPENDS_ON`` is
#: deliberately absent — ``ARTIFACT_DERIVED_FROM`` already expresses it, and two
#: names for one fact is how a graph acquires contradictory truths.
RELATIONS: tuple[str, ...] = (
    "PROJECT_HAS_INTENT",
    "INTENT_COMPILED_TO_REVISION",
    "REVISION_HAS_PLAN",
    "PLAN_EMITS_COMMAND",
    "COMMAND_EXECUTED_AS",
    "EXECUTION_PRODUCED_ARTIFACT",
    "ARTIFACT_VERIFIED_BY",
    "ARTIFACT_HAS_PASSPORT",
    "ARTIFACT_DERIVED_FROM",
    "DECISION_AFFECTS",
    "ACTOR_AUTHORIZED",
)

#: The upward chain that answers "where did this artifact come from?", as
#: ``(relation, direction_from_the_frontier)``.  Direction matters: the domain
#: relations point *downstream* (an execution produces an artifact), so walking
#: from an artifact to its execution follows an **incoming** edge.
LINEAGE_CHAIN: tuple[tuple[str, str], ...] = (
    ("EXECUTION_PRODUCED_ARTIFACT", "in"),
    ("COMMAND_EXECUTED_AS", "in"),
    ("PLAN_EMITS_COMMAND", "in"),
    ("REVISION_HAS_PLAN", "in"),
    ("INTENT_COMPILED_TO_REVISION", "in"),
    ("PROJECT_HAS_INTENT", "in"),
)

#: Facts attached to the artifact itself rather than upstream of it.
LINEAGE_ATTACHMENTS: tuple[str, ...] = ("ARTIFACT_VERIFIED_BY", "ARTIFACT_HAS_PASSPORT")

#: Node lifecycle states. ``active`` is the only one traversal treats as truth;
#: the rest are recorded so the past stays readable.
STATUSES: tuple[str, ...] = ("active", "superseded", "invalidated", "revoked")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nexus_graph_node (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_by TEXT,
    source TEXT NOT NULL,
    source_seq INTEGER,
    revision_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EDGES = """
CREATE TABLE IF NOT EXISTS nexus_graph_edge (
    edge_id TEXT PRIMARY KEY,
    relation TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    source_seq INTEGER,
    created_at TEXT NOT NULL
)
"""

_HISTORY = """
CREATE TABLE IF NOT EXISTS nexus_graph_node_history (
    history_seq INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_src ON nexus_graph_edge (src, relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_dst ON nexus_graph_edge (dst, relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_rel ON nexus_graph_edge (relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_node_kind ON nexus_graph_node (kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_graph_history_node ON nexus_graph_node_history (node_id)",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def node_identity(kind: str, identity: dict[str, Any]) -> str:
    """The deterministic, scoped, auditable identity of a node.

    Scoped by kind so a PROJECT and an ARTIFACT that happen to share a name are
    different nodes; stable because it is a digest of the canonical identity
    payload; auditable because ``identity_json`` is stored alongside it.
    """
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown node kind {kind!r}; expected one of {NODE_KINDS}")
    if not identity:
        raise ValueError(f"a {kind} node needs a non-empty identity payload")
    return f"{kind}:{digest_of(identity)}"


def edge_identity(relation: str, src: str, dst: str) -> str:
    if relation not in RELATIONS:
        raise ValueError(f"unknown relation {relation!r}; expected one of {RELATIONS}")
    return f"{relation}:{src}->{dst}"


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    kind: str
    identity: dict[str, Any]
    payload: dict[str, Any]
    payload_digest: str
    status: str
    superseded_by: str | None
    source: str
    source_seq: int | None
    revision_count: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "identity": self.identity,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "status": self.status,
            "superseded_by": self.superseded_by,
            "source": self.source,
            "source_seq": self.source_seq,
            "revision_count": self.revision_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    relation: str
    src: str
    dst: str
    payload: dict[str, Any]
    status: str
    source: str
    source_seq: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "relation": self.relation,
            "src": self.src,
            "dst": self.dst,
            "payload": self.payload,
            "status": self.status,
            "source": self.source,
            "source_seq": self.source_seq,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WriteResult:
    """What a write actually did — idempotency made visible."""

    node: GraphNode | None = None
    edge: GraphEdge | None = None
    created: bool = False
    unchanged: bool = False
    superseded_previous: bool = False


@dataclass(frozen=True)
class IntegrityFinding:
    code: str
    severity: str  # error | warning
    message: str
    witness: str


@dataclass
class IntegrityReport:
    findings: list[IntegrityFinding] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    history_count: int = 0

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "history_count": self.history_count,
            "findings": [f.__dict__ for f in self.findings],
        }


class CreativeGraph:
    """The durable creative graph: typed nodes/edges in one SQLite sidecar.

    Shares the ``CausalJournal``'s connection discipline (single writer,
    ``BEGIN IMMEDIATE``, busy timeout, ``:memory:`` support) so the projection
    can live in the *same* file as the ledger it is rebuilt from — one backup,
    one restore, one integrity story.
    """

    def __init__(self, db_path: Path | str, *, connect_timeout: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        self._connect_timeout = connect_timeout
        self._lock = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(_SCHEMA)
            connection.execute(_EDGES)
            connection.execute(_HISTORY)
            for index in _INDEXES:
                connection.execute(index)

    # ------------------------------------------------------------------ #
    # writes — transactional, idempotent, history-preserving
    # ------------------------------------------------------------------ #
    def put_node(
        self,
        kind: str,
        identity: dict[str, Any],
        payload: dict[str, Any] | None = None,
        *,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """Create or advance one node. Never rewrites history.

        * same identity, same payload digest → ``unchanged`` (idempotent);
        * same identity, different payload → the previous payload is archived in
          ``nexus_graph_node_history`` and marked ``superseded``;
        * a node already ``invalidated``/``revoked`` is never silently revived.
        """
        with self._lock, self._write_connection() as connection:
            return self._put_node_tx(
                connection, kind, identity, payload, source=source, source_seq=source_seq
            )

    def _put_node_tx(
        self,
        connection: sqlite3.Connection,
        kind: str,
        identity: dict[str, Any],
        payload: dict[str, Any] | None = None,
        *,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """The node write itself, on a caller-owned transaction.

        ``rebuild_from_journal`` drives many of these inside one
        ``BEGIN IMMEDIATE`` so a failed rebuild cannot publish half a
        projection.  Every write path reachable from a rebuild must go through
        here rather than re-opening its own transaction."""
        node_id = node_identity(kind, identity)
        body = dict(payload or {})
        body_digest = digest_of(body)
        stamp = _now()
        row = connection.execute(
            "SELECT payload_digest, status, revision_count, created_at FROM nexus_graph_node"
            " WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is not None:
            previous_digest, previous_status = str(row[0]), str(row[1])
            if previous_digest == body_digest and previous_status == "active":
                return WriteResult(node=self._node(connection, node_id), unchanged=True)
            if previous_status in ("invalidated", "revoked"):
                raise GraphStateError(
                    f"node {node_id} is {previous_status}; a write must supersede it "
                    "explicitly, never overwrite it"
                )
            connection.execute(
                "UPDATE nexus_graph_node SET payload_json = ?, payload_digest = ?,"
                " status = 'active', superseded_by = NULL, source = ?, source_seq = ?,"
                " revision_count = revision_count + 1, updated_at = ? WHERE node_id = ?",
                (
                    canonical_json(body),
                    body_digest,
                    source,
                    source_seq,
                    stamp,
                    node_id,
                ),
            )
            # The history log records every state the node has *established*,
            # oldest first: the create row is still there, so the previous
            # truth stays readable and the new one is appended after it.
            connection.execute(
                "INSERT INTO nexus_graph_node_history"
                " (node_id, payload_json, payload_digest, status, recorded_at)"
                " VALUES (?, ?, ?, 'active', ?)",
                (node_id, canonical_json(body), body_digest, stamp),
            )
            return WriteResult(node=self._node(connection, node_id), superseded_previous=True)
        connection.execute(
            "INSERT INTO nexus_graph_node (node_id, kind, identity_json, identity_digest,"
            " payload_json, payload_digest, status, superseded_by, source, source_seq,"
            " revision_count, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, 1, ?, ?)",
            (
                node_id,
                kind,
                canonical_json(identity),
                digest_of(identity),
                canonical_json(body),
                body_digest,
                source,
                source_seq,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO nexus_graph_node_history"
            " (node_id, payload_json, payload_digest, status, recorded_at)"
            " VALUES (?, ?, ?, 'active', ?)",
            (node_id, canonical_json(body), body_digest, stamp),
        )
        return WriteResult(node=self._node(connection, node_id), created=True)

    def put_edge(
        self,
        relation: str,
        src: str,
        dst: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """Create one edge. Both endpoints must exist; an edge is idempotent."""
        with self._lock, self._write_connection() as connection:
            return self._put_edge_tx(
                connection, relation, src, dst, payload, source=source, source_seq=source_seq
            )

    def _put_edge_tx(
        self,
        connection: sqlite3.Connection,
        relation: str,
        src: str,
        dst: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """The edge write itself, on a caller-owned transaction.

        A rebuild reaches edges through here too, so the terminal-edge guard
        (a revoked edge is never silently re-asserted) holds inside it."""
        edge_id = edge_identity(relation, src, dst)
        body = dict(payload or {})
        stamp = _now()
        for endpoint in (src, dst):
            if (
                connection.execute(
                    "SELECT 1 FROM nexus_graph_node WHERE node_id = ?", (endpoint,)
                ).fetchone()
                is None
            ):
                raise GraphStateError(f"cannot link {relation}: endpoint {endpoint} does not exist")
        existing = connection.execute(
            "SELECT payload_digest, status FROM nexus_graph_edge WHERE edge_id = ?",
            (edge_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) == digest_of(body) and str(existing[1]) == "active":
                return WriteResult(edge=self._edge(connection, edge_id), unchanged=True)
            if str(existing[1]) == "revoked":
                raise GraphStateError(
                    f"edge {edge_id} is revoked; re-asserting it needs an explicit revoke"
                    " reversal, not a silent write"
                )
            connection.execute(
                "UPDATE nexus_graph_edge SET payload_json = ?, payload_digest = ?,"
                " status = 'active', source = ?, source_seq = ? WHERE edge_id = ?",
                (canonical_json(body), digest_of(body), source, source_seq, edge_id),
            )
            return WriteResult(edge=self._edge(connection, edge_id))
        connection.execute(
            "INSERT INTO nexus_graph_edge (edge_id, relation, src, dst, payload_json,"
            " payload_digest, status, source, source_seq, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                edge_id,
                relation,
                src,
                dst,
                canonical_json(body),
                digest_of(body),
                source,
                source_seq,
                stamp,
            ),
        )
        return WriteResult(edge=self._edge(connection, edge_id), created=True)

    def set_status(
        self, node_id: str, status: str, *, superseded_by: str | None = None
    ) -> GraphNode:
        """An explicit lifecycle transition — the only way history changes state."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        stamp = _now()
        with self._lock, self._write_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM nexus_graph_node WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise GraphStateError(f"unknown node {node_id}")
            connection.execute(
                "INSERT INTO nexus_graph_node_history"
                " (node_id, payload_json, payload_digest, status, recorded_at)"
                " SELECT node_id, payload_json, payload_digest, ?, ?"
                " FROM nexus_graph_node WHERE node_id = ?",
                (status, stamp, node_id),
            )
            connection.execute(
                "UPDATE nexus_graph_node SET status = ?, superseded_by = ?, updated_at = ?"
                " WHERE node_id = ?",
                (status, superseded_by, stamp, node_id),
            )
            node = self._node(connection, node_id)
        if node is None:  # pragma: no cover - the row was just read above
            raise GraphStateError(f"node {node_id} vanished during the status transition")
        return node

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def get_node(self, node_id: str) -> GraphNode | None:
        with self._connection() as connection:
            return self._node(connection, node_id)

    def find(self, kind: str, identity: dict[str, Any]) -> GraphNode | None:
        return self.get_node(node_identity(kind, identity))

    def history(self, node_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT history_seq, payload_json, payload_digest, status, recorded_at"
                " FROM nexus_graph_node_history WHERE node_id = ? ORDER BY history_seq",
                (node_id,),
            ).fetchall()
        return [
            {
                "history_seq": int(row[0]),
                "payload": json.loads(row[1]),
                "payload_digest": str(row[2]),
                "status": str(row[3]),
                "recorded_at": str(row[4]),
            }
            for row in rows
        ]

    def edges(
        self, node_id: str, *, direction: str = "out", relation: str | None = None
    ) -> list[GraphEdge]:
        column = "src" if direction == "out" else "dst"
        sql = f"SELECT edge_id FROM nexus_graph_edge WHERE {column} = ? AND status = 'active'"
        params: list[Any] = [node_id]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation)
        sql += " ORDER BY edge_id"
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self._edge(connection, str(row[0])) for row in rows]  # type: ignore[misc]

    def traverse(
        self,
        start: str,
        *,
        direction: str = "out",
        relations: tuple[str, ...] | None = None,
        max_depth: int = 32,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Deterministic breadth-first traversal with a cycle guard.

        Returns ``{node, depth, via}`` rows ordered by ``(depth, node_id)``, so
        two runs over the same graph produce the same answer.
        """
        allowed = set(relations) if relations else None
        seen: dict[str, int] = {start: 0}
        queue: deque[tuple[str, int, str]] = deque()
        for edge in self.edges(start, direction=direction):
            if allowed and edge.relation not in allowed:
                continue
            queue.append((edge.dst if direction == "out" else edge.src, 1, edge.relation))
        out: list[dict[str, Any]] = []
        while queue:
            current, depth, via = queue.popleft()
            if depth > max_depth or current in seen:
                continue
            seen[current] = depth
            node = self.get_node(current)
            if node is None:
                continue
            if node.status != "active" and not include_inactive:
                continue
            out.append({"node": node.to_dict(), "depth": depth, "via": via})
            if depth >= max_depth:
                continue
            for edge in self.edges(current, direction=direction):
                if allowed and edge.relation not in allowed:
                    continue
                nxt = edge.dst if direction == "out" else edge.src
                if nxt not in seen:
                    queue.append((nxt, depth + 1, edge.relation))
        out.sort(key=lambda row: (int(row["depth"]), str(row["node"]["node_id"])))
        return out

    def ancestors(self, node_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.traverse(node_id, direction="in", **kwargs)

    def descendants(self, node_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.traverse(node_id, direction="out", **kwargs)

    def lineage(self, artifact_node_id: str) -> dict[str, Any]:
        """The documented answer to "where did this artifact come from?".

        Walks the declared upward chain and reports, per level, what was found —
        and, just as importantly, where the chain *stops*. A missing level is
        reported as an empty ``found`` list, never invented, and ``complete``
        says whether the walk reached a PROJECT.
        """
        node = self.get_node(artifact_node_id)
        if node is None:
            raise GraphStateError(f"unknown artifact node {artifact_node_id}")

        attachments: dict[str, list[dict[str, Any]]] = {}
        for relation in LINEAGE_ATTACHMENTS:
            found: list[dict[str, Any]] = []
            for edge in self.edges(artifact_node_id, direction="out", relation=relation):
                attached = self.get_node(edge.dst)
                if attached is not None and attached.status == "active":
                    found.append(attached.to_dict())
            attachments[relation] = sorted(found, key=lambda n: str(n["node_id"]))

        levels: list[dict[str, Any]] = []
        frontier = [artifact_node_id]
        reached_project = False
        for relation, direction in LINEAGE_CHAIN:
            nxt: list[str] = []
            found = []
            for current in frontier:
                for edge in self.edges(current, direction=direction, relation=relation):
                    parent_id = edge.src if direction == "in" else edge.dst
                    parent = self.get_node(parent_id)
                    if parent is None or parent.status != "active":
                        continue
                    if parent.node_id not in nxt:
                        nxt.append(parent.node_id)
                        found.append(parent.to_dict())
                        reached_project = reached_project or parent.kind == "PROJECT"
            levels.append(
                {"relation": relation, "found": sorted(found, key=lambda n: str(n["node_id"]))}
            )
            frontier = nxt
            if not nxt:
                break
        return {
            "artifact": node.to_dict(),
            "attachments": attachments,
            "levels": levels,
            "complete": reached_project,
            # the deepest level that actually resolved — an empty trailing level
            # is where the walk stopped, not where the truth ends
            "deepest_kind": next(
                (level["found"][0]["kind"] for level in reversed(levels) if level["found"]),
                None,
            ),
        }

    def project_history(self, project_node_id: str) -> list[dict[str, Any]]:
        return self.descendants(project_node_id)

    def artifact_history(self, artifact_node_id: str) -> list[dict[str, Any]]:
        return self.descendants(artifact_node_id)

    def invalidation_candidates(self, node_id: str) -> dict[str, Any]:
        """What is affected when this node changes?

        Downstream reachability over the forward relations, grouped by kind, so
        "if intent A changes, which revisions/plans/commands/artifacts are
        affected?" is a query and not a reading exercise.
        """
        rows = self.descendants(node_id)
        by_kind: dict[str, list[str]] = {}
        for row in rows:
            by_kind.setdefault(str(row["node"]["kind"]), []).append(str(row["node"]["node_id"]))
        return {
            "origin": node_id,
            "affected_count": len(rows),
            "by_kind": {kind: sorted(ids) for kind, ids in sorted(by_kind.items())},
            "affected": rows,
        }

    # ------------------------------------------------------------------ #
    # integrity, recovery, rebuild
    # ------------------------------------------------------------------ #
    def integrity_check(self) -> IntegrityReport:
        """Every edge endpoint exists, every digest matches, history is ordered."""
        report = IntegrityReport()
        with self._connection() as connection:
            nodes = connection.execute(
                "SELECT node_id, kind, payload_json, payload_digest, status, identity_json,"
                " identity_digest, revision_count FROM nexus_graph_node"
            ).fetchall()
            edges = connection.execute(
                "SELECT edge_id, relation, src, dst, payload_json, payload_digest FROM"
                " nexus_graph_edge"
            ).fetchall()
            history = connection.execute(
                "SELECT node_id, payload_digest, status FROM nexus_graph_node_history"
            ).fetchall()
        report.node_count = len(nodes)
        report.edge_count = len(edges)
        report.history_count = len(history)
        known = {str(row[0]) for row in nodes}
        for row in nodes:
            node_id, kind = str(row[0]), str(row[1])
            if kind not in NODE_KINDS:
                report.findings.append(
                    IntegrityFinding("GRAPH010", "error", "unknown node kind", node_id)
                )
            if str(row[4]) not in STATUSES:
                report.findings.append(
                    IntegrityFinding("GRAPH011", "error", "unknown node status", node_id)
                )
            if digest_of(json.loads(str(row[2]))) != str(row[3]):
                report.findings.append(
                    IntegrityFinding("GRAPH012", "error", "payload digest mismatch", node_id)
                )
            if digest_of(json.loads(str(row[5]))) != str(row[6]):
                report.findings.append(
                    IntegrityFinding("GRAPH013", "error", "identity digest mismatch", node_id)
                )
        for row in edges:
            edge_id, relation, src, dst = str(row[0]), str(row[1]), str(row[2]), str(row[3])
            if relation not in RELATIONS:
                report.findings.append(
                    IntegrityFinding("GRAPH020", "error", "unknown relation", edge_id)
                )
            for endpoint in (src, dst):
                if endpoint not in known:
                    report.findings.append(
                        IntegrityFinding(
                            "GRAPH021",
                            "error",
                            "edge endpoint does not exist",
                            f"{edge_id}->{endpoint}",
                        )
                    )
            if digest_of(json.loads(str(row[4]))) != str(row[5]):
                report.findings.append(
                    IntegrityFinding("GRAPH022", "error", "edge digest mismatch", edge_id)
                )
        for node_id in known:
            rows = [h for h in history if str(h[0]) == node_id]
            if not rows:
                report.findings.append(
                    IntegrityFinding("GRAPH030", "error", "node has no history row", node_id)
                )
        return report

    def backup(self, target: Path | str) -> Path:
        """A consistent online backup (SQLite backup API, not a file copy)."""
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output = sqlite3.connect(str(destination))
        try:
            with self._lock, self._connection() as connection:
                connection.backup(output)
        finally:
            output.close()
        return destination

    def restore(self, source: Path | str) -> None:
        """Replace this graph's contents from a backup.

        ``Connection.backup`` overwrites the destination wholesale, but it
        cannot target a connection that is inside an open transaction, so this
        takes the in-process writer lock and uses a dedicated connection rather
        than the ``BEGIN IMMEDIATE`` wrapper.
        """
        origin = sqlite3.connect(str(Path(source)))
        try:
            with self._lock:
                if self._memory_connection is not None:
                    origin.backup(self._memory_connection)
                    return
                connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
                try:
                    origin.backup(connection)
                    connection.commit()
                finally:
                    connection.close()
        finally:
            origin.close()

    def rebuild_from_journal(
        self, records: list[LedgerRecord], *, replace: bool = False
    ) -> dict[str, Any]:
        """Reconstruct everything the ledger can witness — and name what it cannot.

        The canonical source for execution facts is the ledger; for intent,
        revision, plan and command it is the spine, which the ledger never
        observes. Those levels are therefore reported ``UNMIGRATED`` rather than
        guessed: no historical datum is invented here.

        Idempotent by construction: the transitions belong to the ledger, so the
        projection collapses each node identity to the state the *last* record
        establishes and writes it once. Replaying the same ledger therefore
        yields the same graph, byte for byte, instead of re-archiving every
        intermediate transition. Pass ``replace=True`` to drop the previously
        projected rows first (a true from-scratch rebuild).
        """
        executions: dict[str, dict[str, Any]] = {}
        artifacts: dict[str, dict[str, Any]] = {}
        verifications: dict[str, dict[str, Any]] = {}
        passports: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for record in sorted(records, key=lambda r: r.seq):
            payload = record.payload
            job_id = str(payload.get("job_id") or "")
            if not job_id:
                continue
            attempt = payload.get("attempt")
            kind = str(payload.get("kind") or "")
            seq = int(record.seq)
            exe_key = canonical_json({"job_id": job_id, "attempt": attempt})
            executions[exe_key] = {
                "identity": {"job_id": job_id, "attempt": attempt},
                "payload": {
                    "job_type": payload.get("job_type"),
                    "status": payload.get("status"),
                    "ledger_kind": kind,
                    "ledger_seq": seq,
                    "ledger_record_hash": record.record_hash,
                },
                "seq": seq,
            }
            order.append(exe_key)
            if kind == "job_verification_started":
                key = canonical_json({"job_id": job_id, "attempt": attempt})
                verifications[key] = {
                    "identity": {"job_id": job_id, "attempt": attempt},
                    "payload": {"ledger_seq": seq, "ledger_record_hash": record.record_hash},
                    "seq": seq,
                }
            if kind == "job_completed" and payload.get("result_digest"):
                digest = payload.get("result_digest")
                a_key = canonical_json({"job_id": job_id, "result_digest": digest})
                body = {
                    "result_digest": digest,
                    "job_type": payload.get("job_type"),
                    "ledger_seq": seq,
                }
                artifacts[a_key] = {
                    "identity": {"job_id": job_id, "result_digest": digest},
                    "payload": body,
                    "seq": seq,
                    "execution": exe_key,
                    "verification": canonical_json({"job_id": job_id, "attempt": attempt}),
                }
                passports[a_key] = {
                    "identity": {"job_id": job_id, "result_digest": digest},
                    "payload": {"ledger_seq": seq, "ledger_record_hash": record.record_hash},
                    "seq": seq,
                }

        projected = {"EXECUTION": 0, "ARTIFACT": 0, "VERIFICATION": 0, "PASSPORT": 0}
        edges = 0
        skipped_terminal: list[dict[str, str]] = []

        # One transaction for the whole rebuild.  The previous version deleted the
        # old projection in one transaction and then wrote every node and edge in
        # its own, so a crash in between left an observer looking at a half-built
        # graph that looked healthy (reproduced: 5 nodes/3 edges became 1 node/0
        # edges).  Nothing is observable until this commits: either the previous
        # projection stands, or the new generation stands whole.
        with self._lock, self._write_connection() as connection:

            def terminal(node_id: str) -> str | None:
                row = connection.execute(
                    "SELECT status FROM nexus_graph_node WHERE node_id = ?", (node_id,)
                ).fetchone()
                status = str(row[0]) if row is not None else ""
                return status if status in ("invalidated", "revoked") else None

            def project(kind: str, spec: dict[str, Any]) -> WriteResult | None:
                """Write one projected node, unless it is already terminal.

                A caller's explicit invalidation or revocation is a decision the
                ledger does not contain, so a rebuild must not overwrite it.  It
                is skipped, reported, and left exactly as the caller left it.
                """
                node_id = node_identity(kind, spec["identity"])
                status = terminal(node_id)
                if status is not None:
                    skipped_terminal.append({"kind": kind, "node_id": node_id, "status": status})
                    return None
                return self._put_node_tx(
                    connection,
                    kind,
                    spec["identity"],
                    spec["payload"],
                    source="causal_journal",
                    source_seq=spec["seq"],
                )

            if replace:
                # Order matters: the history purge reads the node rows it is about
                # to drop.  A rebuild that kept the old history would grow the log
                # on every run, which is not a rebuild — it is an append in
                # disguise.  Terminal nodes are kept: a from-scratch rebuild of
                # the *projection* must not undo an explicit lifecycle decision.
                connection.execute(
                    "DELETE FROM nexus_graph_node_history WHERE node_id IN"
                    " (SELECT node_id FROM nexus_graph_node WHERE source = 'causal_journal'"
                    " AND status NOT IN ('invalidated', 'revoked'))"
                )
                connection.execute("DELETE FROM nexus_graph_edge WHERE source = 'causal_journal'")
                connection.execute(
                    "DELETE FROM nexus_graph_node WHERE source = 'causal_journal'"
                    " AND status NOT IN ('invalidated', 'revoked')"
                )

            for key in sorted(set(order), key=lambda k: executions[k]["seq"]):
                result = project("EXECUTION", executions[key])
                if result is not None and result.created:
                    projected["EXECUTION"] += 1
            for key in sorted(verifications, key=lambda k: verifications[k]["seq"]):
                result = project("VERIFICATION", verifications[key])
                if result is not None and result.created:
                    projected["VERIFICATION"] += 1
            for key in sorted(artifacts, key=lambda k: artifacts[k]["seq"]):
                artifact = artifacts[key]
                node = project("ARTIFACT", artifact)
                if node is not None and node.created:
                    projected["ARTIFACT"] += 1
                artifact_id = node_identity("ARTIFACT", artifact["identity"])
                execution_id = node_identity(
                    "EXECUTION", executions[artifact["execution"]]["identity"]
                )
                if self._put_edge_tx(
                    connection,
                    "EXECUTION_PRODUCED_ARTIFACT",
                    execution_id,
                    artifact_id,
                    {"ledger_seq": artifact["seq"]},
                    source="causal_journal",
                    source_seq=artifact["seq"],
                ).created:
                    edges += 1
                if artifact["verification"] in verifications:
                    verification_id = node_identity(
                        "VERIFICATION", verifications[artifact["verification"]]["identity"]
                    )
                    if self._put_edge_tx(
                        connection,
                        "ARTIFACT_VERIFIED_BY",
                        artifact_id,
                        verification_id,
                        {"ledger_seq": artifact["seq"]},
                        source="causal_journal",
                        source_seq=artifact["seq"],
                    ).created:
                        edges += 1
                passport = passports[key]
                passport_result = project("PASSPORT", passport)
                if passport_result is not None and passport_result.created:
                    projected["PASSPORT"] += 1
                if passport_result is not None and passport_result.node is not None:
                    if self._put_edge_tx(
                        connection,
                        "ARTIFACT_HAS_PASSPORT",
                        artifact_id,
                        passport_result.node.node_id,
                        {"ledger_seq": passport["seq"]},
                        source="causal_journal",
                        source_seq=passport["seq"],
                    ).created:
                        edges += 1

        return {
            "skipped_terminal": skipped_terminal,
            "records_read": len(records),
            "projected_nodes": projected,
            "projected_edges": edges,
            "unmigrated_levels": {
                "PROJECT": "UNMIGRATED — the ledger never observes projects",
                "INTENT": "UNMIGRATED — the ledger never observes intent",
                "REVISION": "UNMIGRATED — spine state, not a ledger fact",
                "PLAN": "UNMIGRATED — spine state, not a ledger fact",
                "COMMAND": "UNMIGRATED — CommandBus state, not a ledger fact",
                "DECISION": "UNMIGRATED — not recorded by any current source",
                "ACTOR": "UNMIGRATED — not recorded by any current source",
            },
            "canonical_source": (
                "nexus_causal_journal (execution facts) + nexus_job_queue (authority)"
            ),
        }

    @staticmethod
    def _artifact_id(job_id: str, payload: dict[str, Any]) -> str:
        digest = payload.get("result_digest")
        return node_identity("ARTIFACT", {"job_id": job_id, "result_digest": digest})

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nexus_graph_node").fetchone()
            edges = connection.execute("SELECT COUNT(*) FROM nexus_graph_edge").fetchone()
            history = connection.execute("SELECT COUNT(*) FROM nexus_graph_node_history").fetchone()
        return {
            "nodes": int(nodes[0]) if nodes else 0,
            "edges": int(edges[0]) if edges else 0,
            "history": int(history[0]) if history else 0,
        }

    # ------------------------------------------------------------------ #
    # connection plumbing (mirrors CausalJournal exactly)
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """One committed-or-rolled-back connection (mirrors ``CausalJournal``)."""
        if self._memory_connection is not None:
            yield self._memory_connection
            return
        connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` so SQLite's write lock serialises appenders.

        Cross-process safety comes from the database lock, not the in-process
        mutex: a second process waits out the busy timeout and then sees the
        committed state.
        """
        with self._connection() as connection:
            if self._memory_connection is None:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _node(self, connection: sqlite3.Connection, node_id: str) -> GraphNode | None:
        row = connection.execute(
            "SELECT node_id, kind, identity_json, payload_json, payload_digest, status,"
            " superseded_by, source, source_seq, revision_count, created_at, updated_at"
            " FROM nexus_graph_node WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return GraphNode(
            node_id=str(row[0]),
            kind=str(row[1]),
            identity=json.loads(str(row[2])),
            payload=json.loads(str(row[3])),
            payload_digest=str(row[4]),
            status=str(row[5]),
            superseded_by=None if row[6] is None else str(row[6]),
            source=str(row[7]),
            source_seq=None if row[8] is None else int(row[8]),
            revision_count=int(row[9]),
            created_at=str(row[10]),
            updated_at=str(row[11]),
        )

    def _edge(self, connection: sqlite3.Connection, edge_id: str) -> GraphEdge | None:
        row = connection.execute(
            "SELECT edge_id, relation, src, dst, payload_json, status, source, source_seq,"
            " created_at FROM nexus_graph_edge WHERE edge_id = ?",
            (edge_id,),
        ).fetchone()
        if row is None:
            return None
        return GraphEdge(
            edge_id=str(row[0]),
            relation=str(row[1]),
            src=str(row[2]),
            dst=str(row[3]),
            payload=json.loads(str(row[4])),
            status=str(row[5]),
            source=str(row[6]),
            source_seq=None if row[7] is None else int(row[7]),
            created_at=str(row[8]),
        )


class GraphStateError(RuntimeError):
    """A write that would destroy truth, refused instead."""
