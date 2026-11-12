"""Bounded, deterministic edit-plan optimizer (Milestone C).

Given an ordered sequence of story beats and a set of explicitly-provided shot
candidates, select a *feasible* plan that covers every beat in order without
exceeding a declared frame budget, minimising a user-supplied integer
preference.

The search is a dynamic program over the acyclic state ``(beat_index,
last_candidate_id)``. A candidate chosen at beat ``i`` covers a contiguous run
of beats ``beats[i:j]`` that are all in its ``covers_beats`` set; the DP
advances to ``j``. Because ``beat_index`` strictly increases, the state graph is
a DAG and a complete sweep yields a *proven* optimum for this precisely defined
finite problem — but only when the whole applicable search actually ran within
the declared state/transition budget. When a budget is hit the result is
``RESOURCE_LIMIT``; a partial exploration is never reported as optimal.

Tie-breaking is stable: equal-cost plans are compared by ``(preference,
frames, step_count, candidate-id tuple)`` so identical inputs always produce the
identical proposal.

Unknown evidence is never treated as verified. A candidate whose external
continuity evidence is not an explicit ``TRUE`` with a non-empty
``evidence_ref`` is either excluded (when the request demands verified evidence)
or admitted but flagged ``NEEDS_REVIEW`` — it is never silently trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ShotCandidate
from .statuses import SolverStatus, TriValue

__all__ = [
    "PlanRequest",
    "PlanStep",
    "RejectedCandidate",
    "PlanResult",
    "plan_edit",
    "DEFAULT_MAX_STATES",
    "DEFAULT_MAX_TRANSITIONS",
]

DEFAULT_MAX_STATES = 2_000_000
DEFAULT_MAX_TRANSITIONS = 20_000_000

#: Cost key: (preference, frames, step_count, steps). Lower is better.
_CostKey = tuple[int, int, int, tuple[str, ...]]


@dataclass(frozen=True)
class PlanRequest:
    """Inputs to the planner. All fields are explicit; nothing is inferred."""

    beats: tuple[str, ...]
    candidates: tuple[ShotCandidate, ...]
    frame_budget: int
    require_verified_evidence: bool = False

    def __post_init__(self) -> None:
        if self.frame_budget < 0:
            raise ValueError("frame_budget must be non-negative")
        if len(set(self.beats)) != len(self.beats):
            raise ValueError("beats contains duplicates")


@dataclass(frozen=True)
class PlanStep:
    candidate_id: str
    asset_ref: str
    covers: tuple[str, ...]
    frames: int
    preference: int
    evidence_status: TriValue
    evidence_ref: str

    @property
    def evidence_verified(self) -> bool:
        return self.evidence_status is TriValue.TRUE and bool(self.evidence_ref)


@dataclass(frozen=True)
class RejectedCandidate:
    candidate_id: str
    reason: str


@dataclass
class PlanResult:
    status: SolverStatus
    steps: tuple[PlanStep, ...] = ()
    total_frames: int = 0
    total_preference: int = 0
    rejected: tuple[RejectedCandidate, ...] = ()
    explanation: str = ""
    states_examined: int = 0
    transitions_examined: int = 0
    #: True when the returned plan is optimal for the fully-explored finite
    #: problem. ``status is PROVEN_OPTIMAL`` implies this; a partial result may
    #: still carry a best-so-far plan with ``optimal=False``.
    optimal: bool = False

    @property
    def covers(self) -> tuple[str, ...]:
        out: list[str] = []
        for step in self.steps:
            out.extend(step.covers)
        return tuple(out)


def _verified_ok(cand: ShotCandidate, *, require: bool) -> tuple[bool, str]:
    verified = cand.evidence_status is TriValue.TRUE and bool(cand.evidence_ref)
    if verified:
        return True, ""
    if require:
        return False, (
            "external continuity evidence is not an explicit TRUE with a "
            "non-empty evidence_ref; the request requires verified evidence"
        )
    return True, ""


def plan_edit(
    request: PlanRequest,
    *,
    max_states: int = DEFAULT_MAX_STATES,
    max_transitions: int = DEFAULT_MAX_TRANSITIONS,
) -> PlanResult:
    """Return a feasible plan, a refusal, or an honest resource-limited result."""
    beats = request.beats
    n = len(beats)

    # Index candidates by the first beat they can cover, with deterministic
    # ordering by id.
    usable: list[ShotCandidate] = []
    rejected: list[RejectedCandidate] = []
    for cand in sorted(request.candidates, key=lambda c: c.id):
        ok, reason = _verified_ok(cand, require=request.require_verified_evidence)
        if ok:
            usable.append(cand)
        else:
            rejected.append(RejectedCandidate(cand.id, reason))

    if n == 0:
        return PlanResult(
            status=SolverStatus.PROVEN_OPTIMAL,
            explanation="no beats to cover; the empty plan is optimal",
            optimal=True,
            rejected=tuple(rejected),
        )

    beat_index = {b: i for i, b in enumerate(beats)}
    by_first: dict[int, list[ShotCandidate]] = {i: [] for i in range(n)}
    for cand in usable:
        positions = sorted(beat_index[b] for b in cand.covers_beats if b in beat_index)
        if not _is_contiguous(positions):
            rejected.append(
                RejectedCandidate(
                    cand.id,
                    "covers_beats is not a contiguous run in the beat order; the "
                    "interval-partition model does not support gapped coverage",
                )
            )
            continue
        for pos in positions:
            by_first[pos].append(cand)

    # A beat no candidate can cover makes the plan infeasible immediately.
    for i, beat in enumerate(beats):
        if not by_first[i]:
            return PlanResult(
                status=SolverStatus.INFEASIBLE,
                explanation=f"no candidate covers beat {beat!r}",
                rejected=tuple(rejected),
            )

    # DP over (index, last_candidate_id). Each step selects a candidate and
    # covers its *full* contiguous run, so the frontier strictly advances past
    # that run and a candidate is used at most once. ``best[key]`` is the best
    # cost key to have covered beats[0:index] with ``last`` chosen last.
    best: dict[tuple[int, str | None], _CostKey] = {(0, None): (0, 0, 0, ())}
    back: dict[tuple[int, str | None], tuple[int, str | None, ShotCandidate]] = {}
    states = 0
    transitions = 0

    for i in range(n):
        frontier = [(key, cost) for key, cost in best.items() if key[0] == i]
        for (idx, last), cost in sorted(frontier, key=lambda kv: (kv[0][0], kv[0][1] or "")):
            states += 1
            if states > max_states:
                partial = _reconstruct(best, back, beats, n)
                return _resource_limited(partial, states, transitions, rejected, "state budget")
            for cand in by_first[idx]:
                run = _run_length(beats, cand, idx)
                nxt = idx + run
                new_frames = cost[1] + cand.frames
                transitions += 1
                if transitions > max_transitions:
                    partial = _reconstruct(best, back, beats, n)
                    return _resource_limited(
                        partial, states, transitions, rejected, "transition budget"
                    )
                if new_frames > request.frame_budget:
                    continue
                new_cost: _CostKey = (
                    cost[0] + cand.preference,
                    new_frames,
                    cost[2] + 1,
                    cost[3] + (cand.id,),
                )
                key = (nxt, cand.id)
                prev = best.get(key)
                if prev is None or new_cost < prev:
                    best[key] = new_cost
                    back[key] = (idx, last, cand)

    finals = [(key, cost) for key, cost in best.items() if key[0] == n]
    if not finals:
        return PlanResult(
            status=SolverStatus.INFEASIBLE,
            explanation=(
                f"no plan covers all beats within the {request.frame_budget}-frame budget"
            ),
            rejected=tuple(rejected),
            states_examined=states,
            transitions_examined=transitions,
        )

    best_key, best_cost = min(finals, key=lambda kv: (kv[1], kv[0][1] or ""))
    steps = _walk(back, beats, n, best_key[1])
    flagged = any(not s.evidence_verified for s in steps)
    explanation = (
        f"optimal plan of {len(steps)} step(s), {best_cost[1]} frames, "
        f"preference {best_cost[0]} (complete finite search)"
    )
    if flagged:
        explanation += (
            "; NOTE at least one chosen candidate lacks verified external "
            "continuity evidence — NEEDS_REVIEW"
        )
    return PlanResult(
        status=SolverStatus.PROVEN_OPTIMAL,
        steps=steps,
        total_frames=best_cost[1],
        total_preference=best_cost[0],
        rejected=tuple(rejected),
        explanation=explanation,
        states_examined=states,
        transitions_examined=transitions,
        optimal=True,
    )


def _is_contiguous(positions: list[int]) -> bool:
    """True when the sorted positions form an unbroken run (possibly empty)."""
    if len(positions) < 2:
        return True
    return positions[-1] - positions[0] == len(positions) - 1


def _run_length(beats: tuple[str, ...], cand: ShotCandidate, start: int) -> int:
    covers = set(cand.covers_beats)
    length = 0
    i = start
    while i < len(beats) and beats[i] in covers:
        length += 1
        i += 1
    return length


def _walk(
    back: dict[tuple[int, str | None], tuple[int, str | None, ShotCandidate]],
    beats: tuple[str, ...],
    n: int,
    last: str | None,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    key: tuple[int, str | None] = (n, last)
    while key in back:
        idx, prev_last, cand = back[key]
        steps.append(
            PlanStep(
                candidate_id=cand.id,
                asset_ref=cand.asset_ref,
                covers=beats[idx : key[0]],
                frames=cand.frames,
                preference=cand.preference,
                evidence_status=cand.evidence_status,
                evidence_ref=cand.evidence_ref,
            )
        )
        key = (idx, prev_last)
    steps.reverse()
    return tuple(steps)


def _reconstruct(
    best: dict[tuple[int, str | None], _CostKey],
    back: dict[tuple[int, str | None], tuple[int, str | None, ShotCandidate]],
    beats: tuple[str, ...],
    n: int,
) -> tuple[PlanStep, ...] | None:
    finals = [k for k in best if k[0] == n]
    if not finals:
        return None
    best_key = min(finals, key=lambda k: (best[k], k[1] or ""))
    return _walk(back, beats, n, best_key[1])


def _resource_limited(
    partial: tuple[PlanStep, ...] | None,
    states: int,
    transitions: int,
    rejected: list[RejectedCandidate],
    which: str,
) -> PlanResult:
    """Honest result when a budget stopped the search short.

    If a complete, feasible plan was found before the budget ran out, the
    result is ``FEASIBLE_NOT_PROVEN_OPTIMAL`` (a real plan, not proven optimal).
    If no complete plan was found, the result is ``RESOURCE_LIMIT`` — we cannot
    even claim feasibility. Neither case is ever ``PROVEN_OPTIMAL``.
    """
    steps = partial or ()
    complete = bool(steps) and _plan_covers(partial)
    status = SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL if complete else SolverStatus.RESOURCE_LIMIT
    return PlanResult(
        status=status,
        steps=steps,
        total_frames=sum(s.frames for s in steps),
        total_preference=sum(s.preference for s in steps),
        rejected=tuple(rejected),
        explanation=(
            f"{which} reached; the search is incomplete and the result is NOT proven "
            f"optimal (states={states}, transitions={transitions})"
        ),
        states_examined=states,
        transitions_examined=transitions,
        optimal=False,
    )


def _plan_covers(partial: tuple[PlanStep, ...] | None) -> bool:
    """A partial plan is complete when its steps' covered beats are non-empty."""
    if not partial:
        return False
    return all(step.covers for step in partial)
