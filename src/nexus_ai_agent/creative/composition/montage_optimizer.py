"""Rhythmic-montage optimizer and render-spec reducer (AETHEL Wave 5).

A bounded, deterministic Viterbi dynamic program that chooses a frame-exact
duration for every clip in an ordered sequence so the resulting cut times track
a set of acoustic beat anchors, follow a narrative pacing curve, and respect a
pairwise jump-cut continuity cost derived from
:mod:`nexus_ai_agent.creative.director.shot_planner`.

Costs are exact rationals (:class:`fractions.Fraction`), so schedules that tie do
so exactly and the tie-break is deterministic. The DP state is
``(clip_index, cumulative_frames)`` and every reachable cumulative frame count is
explored, so a returned ``PROVEN_OPTIMAL`` means the whole applicable finite
search actually completed. When the state budget is reached the result is an
honest ``FEASIBLE_NOT_PROVEN_OPTIMAL`` / ``RESOURCE_LIMIT`` instead.

The module renders nothing and owns no project state; the render-spec reducer is
an inert, JSON-serialisable handoff for
:mod:`nexus_ai_agent.creative.rendering.compiler`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from nexus_ai_agent.creative.episode_causality_lab.statuses import SolverStatus

from .dag import Edge, Node, NodeKind, TimelineDAG, crossfade_gains
from .errors import CompositionError

__all__ = [
    "DEFAULT_MAX_STATES",
    "FEASIBLE_BOUNDED",
    "BeatAnchor",
    "ClipCandidate",
    "MontageRequest",
    "MontageSchedule",
    "MontageWeights",
    "compile_dag_to_render_spec",
    "compile_montage_to_timeline_dag",
    "tension_fraction",
    "jump_cost_from_setups",
    "optimize_rhythmic_montage",
]

#: A hard cap on the number of ``(clip, cumulative_frames)`` states explored.
DEFAULT_MAX_STATES = 2_000_000

#: ``FEASIBLE_BOUNDED`` is the honest name for a best-so-far plan whose search
#: was cut short by a resource bound. It maps onto the shared solver vocabulary.
FEASIBLE_BOUNDED = SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL

#: The tension curve is supplied as floats; converting through this denominator
#: keeps the rational arithmetic exact and reproducible.
_TENSION_DENOM = 10**6


class MontageError(CompositionError):
    """A malformed montage-optimization request."""


@dataclass(frozen=True)
class ClipCandidate:
    """One ordered clip with an inclusive allowed duration window, in frames."""

    clip_id: str
    min_frames: int
    max_frames: int

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise MontageError("clip_id must be non-empty")
        if self.min_frames < 1:
            raise MontageError("min_frames must be >= 1")
        if self.max_frames < self.min_frames:
            raise MontageError("max_frames must be >= min_frames")


@dataclass(frozen=True)
class BeatAnchor:
    """An acoustic beat: a frame position and a salience in ``(0, 1]``."""

    frame: int
    salience: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise MontageError("beat frame must be non-negative")
        if not (0 < self.salience <= 1):
            raise MontageError("beat salience must be in (0, 1]")


@dataclass(frozen=True)
class MontageWeights:
    """Relative weights of the three cost terms."""

    beat: Fraction = Fraction(1)
    pace: Fraction = Fraction(1)
    continuity: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        for name, value in (
            ("beat", self.beat),
            ("pace", self.pace),
            ("continuity", self.continuity),
        ):
            if value < 0:
                raise MontageError(f"{name} weight must be non-negative")


@dataclass(frozen=True)
class MontageSchedule:
    """The optimizer's typed result. ``cut_frames[i]`` is the cumulative frame
    count *after* clip ``i`` (the formula's ``t_cut,i``)."""

    status: SolverStatus
    clip_ids: tuple[str, ...] = ()
    durations: tuple[int, ...] = ()
    cut_frames: tuple[int, ...] = ()
    beat_residuals: tuple[tuple[int, int], ...] = ()
    total_cost: Fraction = Fraction(0)
    total_frames: int = 0
    optimal: bool = False
    states_examined: int = 0
    transitions_examined: int = 0
    explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "clip_ids": list(self.clip_ids),
            "durations": list(self.durations),
            "cut_frames": list(self.cut_frames),
            "beat_residuals": [list(r) for r in self.beat_residuals],
            "total_cost": str(self.total_cost),
            "total_frames": self.total_frames,
            "optimal": self.optimal,
            "states_examined": self.states_examined,
            "transitions_examined": self.transitions_examined,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class MontageRequest:
    """All optimizer inputs are explicit; nothing is inferred."""

    clips: tuple[ClipCandidate, ...]
    beats: tuple[BeatAnchor, ...] = ()
    #: Narrative tension in ``[0, 1]``, one entry per clip (``T_i``).
    tension: tuple[Fraction, ...] = ()
    #: ``(fast_frames, slow_frames)``: the target duration at ``T=0`` and ``T=1``.
    pace_target: tuple[int, int] = (24, 120)
    #: ``C_jump[i]`` is the cost of cutting from clip ``i-1`` into clip ``i``.
    jump_cost: tuple[Fraction, ...] = ()
    weights: MontageWeights = field(default_factory=MontageWeights)
    max_states: int = DEFAULT_MAX_STATES

    def __post_init__(self) -> None:
        if not self.clips:
            raise MontageError("at least one clip is required")
        if self.tension and len(self.tension) != len(self.clips):
            raise MontageError("tension must be empty or one entry per clip")
        if self.jump_cost and len(self.jump_cost) != len(self.clips):
            raise MontageError("jump_cost must be empty or one entry per clip")
        if self.max_states < 1:
            raise MontageError("max_states must be >= 1")
        fast, slow = self.pace_target
        if fast < 1 or slow < 1:
            raise MontageError("pace_target frames must be >= 1")
        for t in self.tension:
            if not 0 <= t <= 1:
                raise MontageError("tension entries must lie in [0, 1]")
        for c in self.jump_cost:
            if c < 0:
                raise MontageError("jump_cost entries must be non-negative")


def _target_frames(tension: Fraction, fast: int, slow: int) -> Fraction:
    """``dt*(T) = slow - T*(slow - fast)`` as an exact rational."""
    return Fraction(slow) - tension * Fraction(slow - fast)


def tension_fraction(value: float) -> Fraction:
    """Convert a ``[0, 1]`` float tension into an exact, reproducible rational."""
    return Fraction(round(value * _TENSION_DENOM), _TENSION_DENOM)


def _beat_term(cut: int, beats: tuple[BeatAnchor, ...], weight: Fraction) -> Fraction:
    if not beats or weight == 0:
        return Fraction(0)
    best: Fraction | None = None
    for anchor in beats:
        dist = Fraction((cut - anchor.frame) ** 2) / anchor.salience
        if best is None or dist < best:
            best = dist
    assert best is not None
    return weight * best


def _pace_term(
    duration: int, tension: Fraction, fast: int, slow: int, weight: Fraction
) -> Fraction:
    if weight == 0:
        return Fraction(0)
    delta = Fraction(duration) - _target_frames(tension, fast, slow)
    return weight * delta * delta


def _nearest_beat_residual(cut: int, beats: tuple[BeatAnchor, ...]) -> tuple[int, int]:
    """The nearest beat frame and the signed residual ``cut - beat``."""
    if not beats:
        return (0, 0)
    best = min(beats, key=lambda a: (abs(cut - a.frame), a.frame))
    return (best.frame, cut - best.frame)


def optimize_rhythmic_montage(request: MontageRequest) -> MontageSchedule:
    """Solve the rhythmic-montage DP exactly under the stated assumptions.

    Returns ``PROVEN_OPTIMAL`` only when the complete applicable finite search
    ran; otherwise ``FEASIBLE_NOT_PROVEN_OPTIMAL`` with the best plan found so
    far and an honest explanation. Ties are broken by the lexicographically
    smallest duration tuple, so identical inputs give identical schedules.
    """
    clips = request.clips
    beats = request.beats
    m = len(clips)
    tension = request.tension or (Fraction(0),) * m
    fast, slow = request.pace_target
    w = request.weights
    jump = request.jump_cost or (Fraction(0),) * m

    def clip_cost(i: int, cum_before: int, duration: int, cum_after: int) -> Fraction:
        cost = _beat_term(cum_after, beats, w.beat)
        cost += _pace_term(duration, tension[i], fast, slow, w.pace)
        if i > 0:
            cost += w.continuity * jump[i]
        return cost

    # ``layer[s]`` maps a reachable cumulative frame count to
    # ``(cost, durations_tuple)`` for the best prefix ending at that count.
    layer: dict[int, tuple[Fraction, tuple[int, ...]]] = {0: (Fraction(0), ())}
    states = 0
    transitions = 0
    truncated = False
    for i, clip in enumerate(clips):
        nxt: dict[int, tuple[Fraction, tuple[int, ...]]] = {}
        for cum_before in sorted(layer):
            prev_cost, prev_durs = layer[cum_before]
            for duration in range(clip.min_frames, clip.max_frames + 1):
                cum_after = cum_before + duration
                transitions += 1
                cost = prev_cost + clip_cost(i, cum_before, duration, cum_after)
                key = (cost, prev_durs + (duration,))
                existing = nxt.get(cum_after)
                if existing is None or key < existing:
                    nxt[cum_after] = key
        states += len(nxt)
        if states > request.max_states:
            truncated = True
            break
        layer = nxt

    if not layer:  # pragma: no cover - clips always allow at least one duration
        return MontageSchedule(
            status=SolverStatus.INFEASIBLE,
            states_examined=states,
            transitions_examined=transitions,
            explanation="no reachable duration assignment satisfies the clip windows",
        )

    best_cum = min(layer, key=lambda s: (layer[s][0], layer[s][1]))
    best_cost, best_durs = layer[best_cum]
    cuts: list[int] = []
    running = 0
    for d in best_durs:
        running += d
        cuts.append(running)
    residuals = tuple(_nearest_beat_residual(c, beats) for c in cuts)
    if truncated:
        status = FEASIBLE_BOUNDED
        optimal = False
        explanation = (
            f"state budget {request.max_states} reached after {states} states; "
            "the returned schedule is the best-so-far, not a proven optimum"
        )
    else:
        status = SolverStatus.PROVEN_OPTIMAL
        optimal = True
        explanation = (
            f"complete DP over {states} states and {transitions} transitions; "
            "the schedule is optimal for the stated objective"
        )
    return MontageSchedule(
        status=status,
        clip_ids=tuple(c.clip_id for c in clips),
        durations=best_durs,
        cut_frames=tuple(cuts),
        beat_residuals=residuals,
        total_cost=best_cost,
        total_frames=best_cum,
        optimal=optimal,
        states_examined=states,
        transitions_examined=transitions,
        explanation=explanation,
    )


def compile_montage_to_timeline_dag(schedule: MontageSchedule) -> TimelineDAG:
    """Compile a schedule into a linear ``v1`` TimelineDAG (one clip per entry)."""
    if schedule.status not in (SolverStatus.PROVEN_OPTIMAL, FEASIBLE_BOUNDED):
        raise MontageError(f"cannot compile a schedule with status {schedule.status.value}")
    dag = TimelineDAG()
    track = "v1"
    dag.add_node(Node(id=track, kind=NodeKind.TRACK, label="primary video"))
    prev: str | None = None
    for i, (clip_id, frames) in enumerate(zip(schedule.clip_ids, schedule.durations, strict=True)):
        node_id = f"{track}_clip_{i}"
        dag.add_node(Node(id=node_id, kind=NodeKind.CLIP, label=clip_id, duration_frames=frames))
        dag.add_edge(Edge(track, node_id, "contains"))
        if prev is not None:
            dag.add_edge(Edge(prev, node_id, "finish_to_start"))
        prev = node_id
    return dag


def jump_cost_from_setups(
    sequence: Any,
    actors: tuple[Any, ...] = (),
    *,
    scale: Fraction = Fraction(1),
) -> tuple[Fraction, ...]:
    """Derive ``C_jump`` from a director ``ShotSequence``.

    ``C_jump[0]`` is 0 (no preceding shot). For ``i > 0`` the cost is the
    squared shortfall of the inter-setup camera angle from the 30-degree
    threshold: a cut *below* the threshold (a jump cut) is penalised, one at or
    above it is free. The angle is measured from the shot's subject, which is
    looked up in ``actors`` by ``subject_id``; when an actor is not placed the
    penalty is 0 (unknown, never invented). Only the documented
    ``setups``/``subject_id``/``cam_x``/``cam_y`` attributes are read.
    """
    from nexus_ai_agent.creative.director.shot_planner import (
        MIN_ANGLE_DELTA_DEG,
        camera_angle_deg,
    )

    seq = tuple(getattr(sequence, "setups", ()))
    if not seq:
        return ()
    placed = {a.actor_id: a for a in actors}
    costs: list[Fraction] = [Fraction(0)]
    for prev, nxt in zip(seq, seq[1:], strict=False):
        subject = placed.get(nxt.subject_id)
        if subject is None:
            costs.append(Fraction(0))
            continue
        angle = camera_angle_deg(subject, (prev.cam_x, prev.cam_y), (nxt.cam_x, nxt.cam_y))
        penalty = max(0.0, MIN_ANGLE_DELTA_DEG - angle) ** 2
        costs.append(scale * Fraction(round(penalty * _TENSION_DENOM), _TENSION_DENOM))
    return tuple(costs)


def _start_frames(dag: TimelineDAG) -> dict[str, int]:
    order = dag.topological_order()
    preds: dict[str, list[str]] = {n: [] for n in dag.nodes}
    for e in dag.edges:
        preds[e.dst].append(e.src)
    start: dict[str, int] = {}
    for n in order:
        if not preds[n]:
            start[n] = 0
        else:
            start[n] = max(start[p] + dag.nodes[p].duration_frames for p in preds[n])
    return start


def compile_dag_to_render_spec(
    dag: TimelineDAG,
    *,
    plan: Any | None = None,
    fps: int = 30,
    container: str = "mp4",
) -> dict[str, Any]:
    """Reduce a ``TimelineDAG`` (and an optional ATLAS plan) to an inert,
    JSON-serialisable render spec ready for ``creative/rendering/compiler.py``.

    The spec is a *proposal*: it names video/audio tracks in topological order,
    the composite (Porter-Duff) nodes, any J/L crossfade coverage, and a SHA-256
    content summary. It dispatches nothing and owns no project state.
    """
    if fps < 1:
        raise MontageError("fps must be >= 1")
    order = dag.topological_order()
    starts = _start_frames(dag)
    video: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    composite: list[dict[str, Any]] = []
    for node_id in order:
        node = dag.nodes[node_id]
        entry = {
            "node_id": node_id,
            "label": node.label,
            "start_frame": starts[node_id],
            "duration_frames": node.duration_frames,
        }
        if node.kind is NodeKind.AUDIO_BUS:
            audio.append(entry)
        elif node.kind is NodeKind.TRANSITION:
            composite.append({**entry, "operator": "src_over"})
        elif node.kind is NodeKind.EFFECT:
            composite.append({**entry, "operator": "src_over"})
        elif node.kind is NodeKind.CLIP:
            video.append(entry)
    crossfades = [
        {
            "node_id": n,
            "kind": "crossfade",
            "gains": list(crossfade_gains(0.5)),
        }
        for n in order
        if dag.nodes[n].kind is NodeKind.TRANSITION
    ]
    spec: dict[str, Any] = {
        "schema": "aethel.render_spec.v1",
        "fps": fps,
        "container": container,
        "total_frames": dag.total_frames,
        "topological_order": list(order),
        "video_tracks": video,
        "audio_tracks": audio,
        "composite_nodes": composite,
        "crossfades": crossfades,
    }
    if plan is not None:
        steps = tuple(getattr(plan, "steps", ()))
        spec["plan"] = {
            "status": getattr(getattr(plan, "status", None), "value", None),
            "steps": [getattr(s, "candidate_id", "") for s in steps],
        }
    payload = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    spec["content_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return spec
