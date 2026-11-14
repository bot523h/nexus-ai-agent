"""Multi-track TimelineDAG, topological scheduling, time-warp and J/L cuts.

The DAG is a directed acyclic graph of typed nodes (tracks, clips, transitions,
effects, audio buses) with typed edges. Scheduling is a Kahn topological sort
that *reports the actual cycle nodes* when one is injected, rather than raising
an opaque error.

``time_warp`` maps a normalised destination time ``t in [0, 1]`` to a source
media time ``tau in [0, 1]`` through a uniform cubic Bezier whose two control
points are ``(t1, tau1)`` and ``(t2, tau2)``. The inverse (given ``tau``, find
``t``) is solved by Newton-Raphson with a bisection fallback to a ``1e-9``
tolerance. The curve must be non-decreasing (non-negative instantaneous rate)
unless reverse playback is explicitly declared, and it preserves exact duration.

``compile_atlas_plan_to_dag`` adapts an ATLAS ``PlanResult`` into a DAG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import CyclicGraphError, TimeWarpError, UnknownNodeError

__all__ = [
    "NodeKind",
    "Node",
    "Edge",
    "TimelineDAG",
    "TimeWarp",
    "JLCut",
    "crossfade_gains",
    "compile_atlas_plan_to_dag",
    "WARP_TOLERANCE",
]

WARP_TOLERANCE = 1e-9


class NodeKind(str, Enum):
    TRACK = "track"
    CLIP = "clip"
    TRANSITION = "transition"
    EFFECT = "effect"
    AUDIO_BUS = "audio_bus"


@dataclass(frozen=True)
class Node:
    id: str
    kind: NodeKind
    label: str = ""
    duration_frames: int = 0


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    label: str = ""


@dataclass
class TimelineDAG:
    """A multi-track directed acyclic composition graph."""

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.src not in self.nodes:
            raise UnknownNodeError(f"edge src {edge.src!r} is not a node")
        if edge.dst not in self.nodes:
            raise UnknownNodeError(f"edge dst {edge.dst!r} is not a node")
        self.edges.append(edge)

    def _adjacency(self) -> tuple[dict[str, list[str]], dict[str, int]]:
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        indeg: dict[str, int] = {n: 0 for n in self.nodes}
        for e in self.edges:
            adj[e.src].append(e.dst)
            indeg[e.dst] += 1
        return adj, indeg

    def topological_order(self) -> tuple[str, ...]:
        """Kahn sort; raise :class:`CyclicGraphError` with the cycle nodes."""
        adj, indeg = self._adjacency()
        # deterministic: ready nodes processed in id order
        ready = sorted(n for n, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for m in sorted(adj[n]):
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
                    ready.sort()
        if len(order) != len(self.nodes):
            remaining = sorted(set(self.nodes) - set(order))
            raise CyclicGraphError(f"cycle among nodes: {remaining}")
        return tuple(order)

    @property
    def total_frames(self) -> int:
        """The critical-path length in frames (0 for an empty graph)."""
        if not self.nodes:
            return 0
        order = self.topological_order()
        dist: dict[str, int] = {n: self.nodes[n].duration_frames for n in self.nodes}
        for n in order:
            for e in self.edges:
                if e.src == n:
                    cand = dist[n] + self.nodes[e.dst].duration_frames
                    if cand > dist[e.dst]:
                        dist[e.dst] = cand
        return max(dist.values())


class TimeWarp:
    """A monotone uniform cubic-Bezier time warp on ``[0, 1] -> [0, 1]``.

    The curve is ``P0=(0,0)``, ``P1=(t1, tau1)``, ``P2=(t2, tau2)``, ``P3=(1,1)``.
    ``tau(t)`` solves the x-coordinate ``x(s) = t`` for the Bezier parameter
    ``s`` and returns the y-coordinate; ``t_of_tau`` is its inverse. Both use
    Newton-Raphson with an analytic derivative and a bisection fallback to
    ``WARP_TOLERANCE``.
    """

    def __init__(self, t1: float, tau1: float, t2: float, tau2: float, *, reverse: bool = False):
        for name, v in (("t1", t1), ("tau1", tau1), ("t2", t2), ("tau2", tau2)):
            if not 0.0 <= v <= 1.0:
                raise TimeWarpError(f"{name}={v} is outside [0, 1]")
        if t2 < t1:
            raise TimeWarpError("control-point t must be non-decreasing (single-valued x)")
        self.t1, self.tau1, self.t2, self.tau2 = t1, tau1, t2, tau2
        self.reverse = reverse
        # non-decreasing in tau unless reverse playback is declared
        if not reverse and tau2 < tau1:
            raise TimeWarpError("control-point tau must be non-decreasing (monotone warp)")

    @staticmethod
    def _bezier(s: float, p1: float, p2: float) -> float:
        b1 = 3 * (1 - s) ** 2 * s
        b2 = 3 * (1 - s) * s**2
        return b1 * p1 + b2 * p2 + s**3

    @staticmethod
    def _bezier_d(s: float, p1: float, p2: float) -> float:
        return 3 * (1 - s) ** 2 * p1 + 6 * (1 - s) * s * (p2 - p1) + 3 * s**2 * (1 - p2)

    def _solve(self, target: float, p1: float, p2: float) -> float:
        """Solve ``_bezier(s, p1, p2) = target`` for ``s`` in [0, 1]."""
        lo, hi = 0.0, 1.0
        s = target
        for _ in range(100):
            f = self._bezier(s, p1, p2) - target
            if abs(f) < WARP_TOLERANCE:
                return s
            df = self._bezier_d(s, p1, p2)
            if df <= 0:
                break
            s_next = s - f / df
            if not lo <= s_next <= hi:
                s_next = (lo + hi) / 2
            if f > 0:
                hi = s
            else:
                lo = s
            s = s_next
        lo, hi = 0.0, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if self._bezier(mid, p1, p2) < target:
                lo = mid
            else:
                hi = mid
            if hi - lo < WARP_TOLERANCE:
                break
        return (lo + hi) / 2

    def tau(self, t: float) -> float:
        """Source media time for destination time ``t`` (cubic Bezier)."""
        if not 0.0 <= t <= 1.0:
            raise TimeWarpError(f"t={t} outside [0, 1]")
        s = self._solve(t, self.t1, self.t2)
        return self._bezier(s, self.tau1, self.tau2)

    def t_of_tau(self, tau: float) -> float:
        """Inverse map: destination time for source time ``tau``."""
        if not 0.0 <= tau <= 1.0:
            raise TimeWarpError(f"tau={tau} outside [0, 1]")
        s = self._solve(tau, self.tau1, self.tau2)
        return self._bezier(s, self.t1, self.t2)

    def rate(self, t: float, *, dt: float = 1e-6) -> float:
        """Instantaneous playback rate ``d tau / dt`` (non-negative)."""
        lo = max(0.0, t - dt)
        hi = min(1.0, t + dt)
        if hi == lo:
            return 0.0
        return (self.tau(hi) - self.tau(lo)) / (hi - lo)


def crossfade_gains(alpha: float) -> tuple[float, float]:
    """Constant-power audio crossfade gains at mix fraction ``alpha`` in [0, 1]."""
    if not 0.0 <= alpha <= 1.0:
        raise TimeWarpError(f"alpha={alpha} outside [0, 1]")
    import math

    return (math.cos(alpha * math.pi / 2), math.sin(alpha * math.pi / 2))


@dataclass(frozen=True)
class JLCut:
    """Independent audio/video pre-lap offsets with a constant-power crossfade."""

    video_start_frame: int
    audio_start_frame: int
    overlap_frames: int

    @property
    def audio_offset(self) -> int:
        """Positive when audio pre-laps the video (a J-cut)."""
        return self.video_start_frame - self.audio_start_frame

    @property
    def is_j_cut(self) -> bool:
        return self.audio_start_frame < self.video_start_frame

    @property
    def is_l_cut(self) -> bool:
        return self.audio_start_frame > self.video_start_frame

    def gains_at(self, frame: int) -> tuple[float, float]:
        """(outgoing, incoming) constant-power gains at an overlap frame."""
        if self.overlap_frames <= 0:
            return (0.0, 1.0) if frame >= self.video_start_frame else (1.0, 0.0)
        alpha = min(1.0, max(0.0, frame / self.overlap_frames))
        return crossfade_gains(alpha)


def compile_atlas_plan_to_dag(plan: object, *, track_id: str = "v1") -> TimelineDAG:
    """Adapt an ATLAS ``PlanResult`` into a linear TimelineDAG.

    Each ``PlanStep`` becomes a clip node; consecutive clips are chained with
    FINISH_TO_START edges. The function reads only the documented ``steps`` and
    ``total_frames`` attributes, so it never depends on ATLAS internals.
    """
    dag = TimelineDAG()
    dag.add_node(Node(id=track_id, kind=NodeKind.TRACK, label="primary video"))
    steps = getattr(plan, "steps", ())
    prev: str | None = None
    for i, step in enumerate(steps):
        clip_id = f"{track_id}_clip_{i}"
        frames = int(getattr(step, "frames", 0))
        dag.add_node(
            Node(
                id=clip_id,
                kind=NodeKind.CLIP,
                label=str(getattr(step, "candidate_id", "")),
                duration_frames=frames,
            )
        )
        dag.add_edge(Edge(track_id, clip_id, "contains"))
        if prev is not None:
            dag.add_edge(Edge(prev, clip_id, "finish_to_start"))
        prev = clip_id
    return dag
