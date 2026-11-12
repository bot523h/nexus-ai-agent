"""Tests for the bounded, deterministic edit-plan optimizer.

The optimizer is checked against an independent exhaustive enumeration on many
small deterministic fixtures: the DP's optimum must equal the brute-force
optimum, and the chosen plan must actually cover every beat in order within the
budget.
"""

from __future__ import annotations

import atlas_fixtures as fx

from nexus_ai_agent.creative.episode_causality_lab import (
    PlanRequest,
    SolverStatus,
    TriValue,
    check_consistency,
    plan_edit,
)
from nexus_ai_agent.creative.episode_causality_lab.plan import (
    DEFAULT_MAX_STATES,
    DEFAULT_MAX_TRANSITIONS,
)


def _brute_force_optimum(beats, candidates, budget, require_verified):
    """Exhaustively enumerate contiguous-run partitions; return (cost, plan)."""
    n = len(beats)
    best = None
    usable = [
        c
        for c in candidates
        if (not require_verified) or (c.evidence_status is TriValue.TRUE and bool(c.evidence_ref))
    ]

    # For every candidate, the set of starting positions where its full run is
    # contiguous and every covered beat is in its set.
    def run_from(cand, start):
        covers = set(cand.covers_beats)
        length = 0
        i = start
        while i < n and beats[i] in covers:
            length += 1
            i += 1
        return length

    # Enumerate sequences of (start, candidate) partitions.
    def rec(pos, frames, pref, used, steps):
        nonlocal best
        if pos == n:
            cost = (pref, frames, len(steps), tuple(s.id for s in steps))
            if best is None or cost < best[0]:
                best = (cost, tuple(steps))
            return
        for cand in usable:
            if cand.id in used:
                continue
            run = run_from(cand, pos)
            if run == 0:
                continue
            if frames + cand.frames > budget:
                continue
            rec(
                pos + run,
                frames + cand.frames,
                pref + cand.preference,
                used | {cand.id},
                steps + (cand,),
            )

    rec(0, 0, 0, frozenset(), ())
    return best


def test_single_candidate_covers_all_beats() -> None:
    req = PlanRequest(
        beats=("b1", "b2", "b3"),
        candidates=(fx.shot("s1", ("b1", "b2", "b3"), 30),),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert result.status is SolverStatus.PROVEN_OPTIMAL
    assert result.covers == ("b1", "b2", "b3")
    assert result.total_frames == 30


def test_cheaper_candidate_is_preferred() -> None:
    req = PlanRequest(
        beats=("b1",),
        candidates=(
            fx.shot("s1", ("b1",), 10, preference=5),
            fx.shot("s2", ("b1",), 10, preference=1),
        ),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert result.steps[0].candidate_id == "s2"


def test_infeasible_when_budget_too_small() -> None:
    req = PlanRequest(beats=("b1",), candidates=(fx.shot("s1", ("b1",), 100),), frame_budget=10)
    result = plan_edit(req)
    assert result.status is SolverStatus.INFEASIBLE


def test_infeasible_when_a_beat_has_no_candidate() -> None:
    req = PlanRequest(
        beats=("b1", "b2"), candidates=(fx.shot("s1", ("b1",), 10),), frame_budget=100
    )
    result = plan_edit(req)
    assert result.status is SolverStatus.INFEASIBLE
    assert "b2" in result.explanation


def test_missing_candidate_reported_in_explanation() -> None:
    req = PlanRequest(beats=("b1",), candidates=(), frame_budget=10)
    result = plan_edit(req)
    assert result.status is SolverStatus.INFEASIBLE


def test_empty_beats_is_trivially_optimal() -> None:
    result = plan_edit(PlanRequest(beats=(), candidates=(), frame_budget=0))
    assert result.status is SolverStatus.PROVEN_OPTIMAL
    assert result.optimal


def test_unknown_evidence_is_not_treated_as_verified() -> None:
    req = PlanRequest(
        beats=("b1",),
        candidates=(fx.shot("s1", ("b1",), 10, verified=False),),
        frame_budget=100,
        require_verified_evidence=True,
    )
    result = plan_edit(req)
    assert result.status is SolverStatus.INFEASIBLE
    assert any(r.candidate_id == "s1" for r in result.rejected)


def test_unknown_evidence_admitted_when_not_required_but_flagged() -> None:
    req = PlanRequest(
        beats=("b1",),
        candidates=(fx.shot("s1", ("b1",), 10, verified=False),),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert result.status is SolverStatus.PROVEN_OPTIMAL
    assert result.steps[0].evidence_verified is False
    assert "NEEDS_REVIEW" in result.explanation


def test_gapped_coverage_is_rejected() -> None:
    req = PlanRequest(
        beats=("b1", "b2", "b3"),
        candidates=(fx.shot("s1", ("b1", "b3"), 10), fx.shot("s2", ("b2",), 5)),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert any(r.candidate_id == "s1" for r in result.rejected)


def test_resource_limit_is_honest_not_optimal() -> None:
    # Force a tiny transition budget on a problem that needs exploration.
    beats = tuple(f"b{i}" for i in range(6))
    cands = tuple(fx.shot(f"s{i}", (b,), 1) for i, b in enumerate(beats))
    req = PlanRequest(beats=beats, candidates=cands, frame_budget=100)
    result = plan_edit(req, max_transitions=1)
    assert result.status is SolverStatus.RESOURCE_LIMIT
    assert result.optimal is False


def test_state_budget_is_honest() -> None:
    beats = tuple(f"b{i}" for i in range(4))
    cands = tuple(fx.shot(f"s{i}", (b,), 1) for i, b in enumerate(beats))
    req = PlanRequest(beats=beats, candidates=cands, frame_budget=100)
    result = plan_edit(req, max_states=0)
    assert result.status is SolverStatus.RESOURCE_LIMIT


def test_complete_plan_found_before_budget_is_feasible_not_optimal() -> None:
    # A single candidate covers both beats; a tight transition budget cuts the
    # search after that complete plan is already known. It is feasible, but must
    # NOT be claimed optimal.
    req = PlanRequest(
        beats=("b0", "b1"),
        candidates=(
            fx.shot("s0", ("b0",), 1),
            fx.shot("s1", ("b0", "b1"), 2),
            fx.shot("s2", ("b1",), 1),
        ),
        frame_budget=100,
    )
    result = plan_edit(req, max_transitions=2)
    assert result.status is SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL
    assert result.optimal is False
    assert result.covers == ("b0", "b1")


def test_feasible_not_optimal_folds_to_resource_limit_in_analysis() -> None:
    req = PlanRequest(
        beats=("b0", "b1"),
        candidates=(
            fx.shot("s0", ("b0",), 1),
            fx.shot("s1", ("b0", "b1"), 2),
            fx.shot("s2", ("b1",), 1),
        ),
        frame_budget=100,
    )
    from nexus_ai_agent.creative.episode_causality_lab.analysis import _collect_notes
    from nexus_ai_agent.creative.episode_causality_lab.plan import plan_edit as pe

    plan = pe(req, max_transitions=2)
    notes = _collect_notes(
        check_consistency(()),
        plan,
    )
    assert any("incomplete" in n for n in notes)


def test_deterministic_tie_break_by_candidate_id() -> None:
    req = PlanRequest(
        beats=("b1",),
        candidates=(
            fx.shot("zeta", ("b1",), 10, preference=1),
            fx.shot("alpha", ("b1",), 10, preference=1),
        ),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert result.steps[0].candidate_id == "alpha"


def test_determinism_across_repeated_runs() -> None:
    req = PlanRequest(
        beats=("b1", "b2", "b3"),
        candidates=(
            fx.shot("s1", ("b1", "b2"), 20, preference=2),
            fx.shot("s2", ("b2", "b3"), 20, preference=2),
            fx.shot("s3", ("b1", "b2", "b3"), 60, preference=3),
        ),
        frame_budget=100,
    )
    first = plan_edit(req)
    second = plan_edit(req)
    assert [s.candidate_id for s in first.steps] == [s.candidate_id for s in second.steps]


def test_plan_equals_brute_force_on_small_fixtures() -> None:
    import random

    rng = random.Random(4242)
    for trial in range(60):
        n = rng.randint(1, 5)
        beats = tuple(f"b{i}" for i in range(n))
        cands = []
        for c in range(rng.randint(1, 5)):
            start = rng.randrange(n)
            length = rng.randint(1, n - start)
            covers = tuple(beats[start : start + length])
            cands.append(
                fx.shot(
                    f"s{c}",
                    covers,
                    rng.randint(0, 20),
                    preference=rng.randint(0, 5),
                    verified=rng.random() > 0.2,
                )
            )
        budget = rng.randint(0, 60)
        req = PlanRequest(beats=beats, candidates=tuple(cands), frame_budget=budget)
        result = plan_edit(req)
        oracle = _brute_force_optimum(beats, tuple(cands), budget, require_verified=False)
        if oracle is None:
            assert result.status is SolverStatus.INFEASIBLE, f"trial {trial}"
        else:
            assert result.status is SolverStatus.PROVEN_OPTIMAL, f"trial {trial}"
            assert result.total_frames == oracle[0][1], f"trial {trial}"
            assert result.total_preference == oracle[0][0], f"trial {trial}"


def test_plan_covers_every_beat_in_order() -> None:
    req = PlanRequest(
        beats=("b1", "b2", "b3", "b4"),
        candidates=(
            fx.shot("s1", ("b1", "b2"), 20),
            fx.shot("s2", ("b3", "b4"), 20),
        ),
        frame_budget=100,
    )
    result = plan_edit(req)
    assert result.covers == ("b1", "b2", "b3", "b4")


def test_duplicate_beats_are_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        PlanRequest(beats=("b1", "b1"), candidates=(), frame_budget=1)


def test_negative_budget_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        PlanRequest(beats=("b1",), candidates=(), frame_budget=-1)


def test_defaults_are_positive() -> None:
    assert DEFAULT_MAX_STATES > 0
    assert DEFAULT_MAX_TRANSITIONS > 0
