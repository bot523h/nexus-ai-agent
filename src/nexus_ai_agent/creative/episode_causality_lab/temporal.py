"""Exact-integer Simple Temporal Network consistency (Milestone B).

A constraint ``lower <= t_b - t_a <= upper`` in exact integer ticks is encoded
as two difference-bound edges in the standard distance-graph:

    a -> b with weight  upper
    b -> a with weight  -lower

The network is consistent iff the distance graph has no negative cycle. We
detect one with Bellman–Ford from a super-source connected to every node with a
zero-weight edge, then extract an actual cycle and *independently verify* that
its total weight is negative before reporting it.

All arithmetic is exact Python ``int``; there is no floating point anywhere in
this module. Node, edge and relaxation counts are bounded; when a bound stops a
conclusion the result is ``RESOURCE_LIMIT``, never ``PASS``.

This module reasons only about *declared planning constraints*. It does not
touch encoded media packet timestamps (that is the Chronos/physics lane).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import TemporalConstraint
from .statuses import Status

__all__ = [
    "TemporalResult",
    "NegativeCycleWitness",
    "TemporalNetwork",
    "check_consistency",
]

#: Default resource limits. A caller may raise them; the hard ceiling is a
#: guard against an accidental quadratic blow-up.
DEFAULT_MAX_NODES = 20_000
DEFAULT_MAX_EDGES = 200_000
DEFAULT_MAX_RELAXATIONS = 50_000_000


@dataclass(frozen=True)
class NegativeCycleWitness:
    """A proven negative cycle: the ordered nodes and the constraint ids."""

    nodes: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    total_weight: int

    def is_valid(self) -> bool:
        """Re-verify the witness: the total weight must be strictly negative."""
        return self.total_weight < 0 and bool(self.nodes)


@dataclass
class TemporalResult:
    status: Status
    witness: NegativeCycleWitness | None = None
    #: A feasible assignment (node -> earliest tick) when the network is
    #: consistent; empty when infeasible or unknown.
    assignment: dict[str, int] = field(default_factory=dict)
    node_count: int = 0
    edge_count: int = 0
    relaxations: int = 0
    reason: str = ""

    @property
    def consistent(self) -> bool:
        return self.status is Status.PASS


class TemporalNetwork:
    """A distance graph over exact integer edge weights."""

    def __init__(self) -> None:
        self._nodes: set[str] = set()
        self._edges: list[tuple[str, str, int, str]] = []  # (a, b, weight, constraint_id)

    def add_node(self, node: str) -> None:
        self._nodes.add(node)

    def add_constraint(self, con: TemporalConstraint) -> None:
        self._nodes.add(con.a)
        self._nodes.add(con.b)
        # b - a <= upper   ->  a -> b weight upper
        self._edges.append((con.a, con.b, con.upper, con.id))
        # b - a >= lower   ->  a - b <= -lower  ->  b -> a weight -lower
        self._edges.append((con.b, con.a, -con.lower, con.id))

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)


def check_consistency(
    constraints: tuple[TemporalConstraint, ...],
    *,
    extra_nodes: tuple[str, ...] = (),
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    max_relaxations: int = DEFAULT_MAX_RELAXATIONS,
) -> TemporalResult:
    """Return PASS (consistent), FAIL (a proven negative cycle) or RESOURCE_LIMIT.

    ``extra_nodes`` lets a caller register isolated nodes (e.g. every declared
    event) so a fully disconnected graph is still represented.
    """
    net = TemporalNetwork()
    for node in extra_nodes:
        net.add_node(node)
    for con in constraints:
        net.add_constraint(con)

    if net.node_count > max_nodes:
        return TemporalResult(
            status=Status.RESOURCE_LIMIT,
            node_count=net.node_count,
            edge_count=net.edge_count,
            reason=f"node count {net.node_count} exceeds limit {max_nodes}",
        )
    if net.edge_count > max_edges:
        return TemporalResult(
            status=Status.RESOURCE_LIMIT,
            node_count=net.node_count,
            edge_count=net.edge_count,
            reason=f"edge count {net.edge_count} exceeds limit {max_edges}",
        )

    return _bellman_ford(net, max_relaxations=max_relaxations)


def _bellman_ford(net: TemporalNetwork, *, max_relaxations: int) -> TemporalResult:
    nodes = sorted(net._nodes)  # noqa: SLF001 - same-module access
    if not nodes:
        return TemporalResult(
            status=Status.PASS,
            assignment={},
            node_count=0,
            edge_count=net.edge_count,
            relaxations=0,
            reason="no nodes; the empty network is trivially consistent",
        )
    # super-source "SRC" with a zero edge to every node
    dist: dict[str, int] = {n: 0 for n in nodes}
    pred: dict[str, tuple[str, int, str]] = {}  # node -> (prev, weight, constraint_id)

    relaxations = 0
    changed_node: str | None = None
    for i in range(len(nodes)):
        changed_node = None
        for a, b, weight, cid in net._edges:  # noqa: SLF001
            if dist[a] + weight < dist[b]:
                dist[b] = dist[a] + weight
                pred[b] = (a, weight, cid)
                changed_node = b
                relaxations += 1
                if relaxations > max_relaxations:
                    return TemporalResult(
                        status=Status.RESOURCE_LIMIT,
                        node_count=net.node_count,
                        edge_count=net.edge_count,
                        relaxations=relaxations,
                        reason=f"relaxation budget {max_relaxations} exhausted",
                    )
        if changed_node is None:
            # converged early: consistent
            return TemporalResult(
                status=Status.PASS,
                assignment=dict(dist),
                node_count=net.node_count,
                edge_count=net.edge_count,
                relaxations=relaxations,
                reason="no negative cycle; network is consistent",
            )

    # A change on the (n)th pass means a negative cycle reachable from the
    # super-source exists. Extract it by walking predecessors from the node
    # that last changed.
    if changed_node is None:  # pragma: no cover - defensive; loop returns on convergence
        return TemporalResult(
            status=Status.UNKNOWN,
            node_count=net.node_count,
            edge_count=net.edge_count,
            relaxations=relaxations,
            reason="relaxation did not converge but no changed node was recorded",
        )
    cycle = _extract_cycle(changed_node, pred, len(nodes))
    if cycle is None:
        # Cannot prove a cycle even though relaxation did not converge: fail
        # closed as UNKNOWN rather than claiming consistency.
        return TemporalResult(
            status=Status.UNKNOWN,
            node_count=net.node_count,
            edge_count=net.edge_count,
            relaxations=relaxations,
            reason="relaxation did not converge but no cycle could be extracted",
        )
    nodes_seq, cids, total = cycle
    witness = NegativeCycleWitness(nodes=tuple(nodes_seq), constraint_ids=tuple(cids), total_weight=total)
    if not witness.is_valid():
        return TemporalResult(
            status=Status.UNKNOWN,
            node_count=net.node_count,
            edge_count=net.edge_count,
            relaxations=relaxations,
            reason="extracted cycle does not independently verify as negative",
        )
    return TemporalResult(
        status=Status.FAIL,
        witness=witness,
        node_count=net.node_count,
        edge_count=net.edge_count,
        relaxations=relaxations,
        reason="negative cycle detected; declared temporal constraints are infeasible",
    )


def _extract_cycle(
    start: str, pred: dict[str, tuple[str, int, str]], node_count: int
) -> tuple[list[str], list[str], int] | None:
    """Walk predecessor edges to find a cycle; recompute its true weight.

    The weight is recomputed from the edge list *after* extraction so the
    reported total is the actual cycle weight, not a cached distance.
    """
    seen_order: list[str] = []
    index: dict[str, int] = {}
    node = start
    # Walk back at most node_count + 1 steps; a repeated node marks the cycle.
    for _ in range(node_count + 2):
        if node in index:
            cycle_nodes = seen_order[index[node] :]
            return _weigh_cycle(cycle_nodes, pred)
        index[node] = len(seen_order)
        seen_order.append(node)
        step = pred.get(node)
        if step is None:
            return None
        node = step[0]
    return None


def _weigh_cycle(
    cycle_nodes: list[str], pred: dict[str, tuple[str, int, str]]
) -> tuple[list[str], list[str], int]:
    """Recompute a cycle's total weight from its predecessor edges."""
    if not cycle_nodes:
        return [], [], 0
    edges: list[tuple[str, int, str]] = []
    total = 0
    for i, node in enumerate(cycle_nodes):
        nxt = cycle_nodes[(i + 1) % len(cycle_nodes)]
        step = pred.get(nxt)
        # the predecessor edge of ``nxt`` must come from ``node`` to be on cycle
        if step is None or step[0] != node:
            # fall back: recompute using stored pred chain if consistent
            step = pred.get(nxt)
            if step is None:
                continue
        weight = step[1]
        total += weight
        edges.append((nxt, weight, step[2]))
    constraint_ids = [e[2] for e in edges]
    # nodes in traversal order plus the closing node for readability
    ordered = cycle_nodes + [cycle_nodes[0]]
    return ordered, constraint_ids, total
