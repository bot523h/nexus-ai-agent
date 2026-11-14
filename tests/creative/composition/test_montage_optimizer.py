"""Tests for the AETHEL rhythmic-montage optimizer and render-spec reducer."""

from __future__ import annotations

import itertools
import json
from fractions import Fraction

import pytest

from nexus_ai_agent.creative.composition import (
    CyclicGraphError,
    Edge,
    Node,
    NodeKind,
    TimelineDAG,
    compile_dag_to_render_spec,
    compile_montage_to_timeline_dag,
    optimize_rhythmic_montage,
)
from nexus_ai_agent.creative.composition.montage_optimizer import (
    FEASIBLE_BOUNDED,
    BeatAnchor,
    ClipCandidate,
    MontageError,
    MontageRequest,
    MontageWeights,
    jump_cost_from_setups,
    tension_fraction,
)
from nexus_ai_agent.creative.episode_causality_lab.statuses import SolverStatus


def _brute_force(request: MontageRequest) -> tuple[Fraction, tuple[int, ...]]:
    """An independent exhaustive oracle over the same objective."""
    clips = request.clips
    beats = request.beats
    tension = request.tension or (Fraction(0),) * len(clips)
    fast, slow = request.pace_target
    w = request.weights
    jump = request.jump_cost or (Fraction(0),) * len(clips)
    best: tuple[Fraction, tuple[int, ...]] | None = None
    for durs in itertools.product(*(range(c.min_frames, c.max_frames + 1) for c in clips)):
        cost = Fraction(0)
        cum = 0
        for i, d in enumerate(durs):
            cum += d
            if beats and w.beat != 0:
                cost += w.beat * min(Fraction((cum - a.frame) ** 2) / a.salience for a in beats)
            target = Fraction(slow) - tension[i] * Fraction(slow - fast)
            cost += w.pace * (Fraction(d) - target) ** 2
            if i > 0:
                cost += w.continuity * jump[i]
        key = (cost, durs)
        if best is None or key < best:
            best = key
    assert best is not None
    return best


def test_optimal_matches_brute_force_on_small_fixtures() -> None:
    beats = (BeatAnchor(5), BeatAnchor(11, Fraction(1, 2)), BeatAnchor(20))
    for seed in range(12):
        n = 3 + (seed % 3)
        clips = tuple(ClipCandidate(f"c{i}", 3 + (i % 2), 7 + ((seed + i) % 3)) for i in range(n))
        tension = tuple(tension_fraction(((seed + i) % 5) / 4) for i in range(n))
        jump = tuple(Fraction(0) if i == 0 else Fraction(i % 3) for i in range(n))
        req = MontageRequest(
            clips=clips,
            beats=beats,
            tension=tension,
            pace_target=(6, 15),
            jump_cost=jump,
            weights=MontageWeights(beat=Fraction(2), pace=Fraction(1), continuity=Fraction(3)),
        )
        got = optimize_rhythmic_montage(req)
        exp_cost, exp_durs = _brute_force(req)
        assert got.status is SolverStatus.PROVEN_OPTIMAL
        assert got.optimal is True
        assert got.total_cost == exp_cost
        assert got.durations == exp_durs


def test_tie_break_is_deterministic() -> None:
    # Two clips, symmetric beats -> multiple equal-cost optima; the lexicographic
    # smallest duration tuple must always win.
    req = MontageRequest(
        clips=(ClipCandidate("a", 4, 6), ClipCandidate("b", 4, 6)),
        beats=(),
        tension=(Fraction(0), Fraction(0)),
        pace_target=(5, 5),
        weights=MontageWeights(beat=Fraction(0), pace=Fraction(1), continuity=Fraction(0)),
    )
    first = optimize_rhythmic_montage(req)
    for _ in range(5):
        assert optimize_rhythmic_montage(req).durations == first.durations
    assert first.durations == (5, 5)


def test_beat_residuals_and_cut_frames() -> None:
    req = MontageRequest(
        clips=(ClipCandidate("a", 4, 8), ClipCandidate("b", 4, 8)),
        beats=(BeatAnchor(6), BeatAnchor(12)),
        tension=(Fraction(0), Fraction(0)),
        pace_target=(6, 6),
        weights=MontageWeights(beat=Fraction(1), pace=Fraction(1), continuity=Fraction(0)),
    )
    s = optimize_rhythmic_montage(req)
    running = 0
    assert len(s.cut_frames) == 2
    for cut, d in zip(s.cut_frames, s.durations, strict=True):
        running += d
        assert cut == running
    for (beat_frame, residual), cut in zip(s.beat_residuals, s.cut_frames, strict=True):
        assert residual == cut - beat_frame


def test_budget_yields_feasible_bounded_not_optimal() -> None:
    clips = tuple(ClipCandidate(f"c{i}", 1, 60) for i in range(8))
    req = MontageRequest(clips=clips, beats=(BeatAnchor(120),), max_states=50)
    s = optimize_rhythmic_montage(req)
    assert s.status is FEASIBLE_BOUNDED
    assert s.status is SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL
    assert s.optimal is False
    assert "budget" in s.explanation


def test_empty_beat_grid_is_pure_pace() -> None:
    req = MontageRequest(
        clips=(ClipCandidate("a", 2, 20),),
        beats=(),
        tension=(Fraction(1),),
        pace_target=(4, 30),
        weights=MontageWeights(beat=Fraction(1), pace=Fraction(1), continuity=Fraction(1)),
    )
    s = optimize_rhythmic_montage(req)
    # T=1 -> the fast target (4 frames), not the slow one.
    assert s.durations == (4,)
    assert s.beat_residuals == ((0, 0),)


def test_nonuniform_beat_ticks() -> None:
    beats = (BeatAnchor(3, Fraction(1, 4)), BeatAnchor(17), BeatAnchor(29, Fraction(1, 3)))
    req = MontageRequest(
        clips=(ClipCandidate("a", 1, 40),),
        beats=beats,
        tension=(Fraction(0),),
        pace_target=(17, 17),
        weights=MontageWeights(beat=Fraction(1), pace=Fraction(0), continuity=Fraction(0)),
    )
    s = optimize_rhythmic_montage(req)
    # three zero-cost optima (cum=3, 17, 29); the lexicographic tie-break wins.
    assert s.durations == (3,)
    assert s.beat_residuals[0] == (3, 0)


def test_jump_cost_from_shot_sequence() -> None:
    from nexus_ai_agent.creative.director import (
        ActorPlacement,
        ShotPlanRequest,
        plan_shot_sequence,
    )

    actors = (ActorPlacement("a", 0.0, 0.0), ActorPlacement("b", 2.0, 0.0))
    seq = plan_shot_sequence(ShotPlanRequest(scene_index=0, actors=actors, primary_pair=("a", "b")))
    costs = jump_cost_from_setups(seq, actors)
    assert len(costs) == len(seq.setups)
    assert costs[0] == 0
    assert all(c >= 0 for c in costs)
    # an unplaced subject must yield an unknown (0) penalty, never an invented one.
    assert jump_cost_from_setups(seq) == tuple(Fraction(0) for _ in seq.setups)


def test_compile_montage_to_dag() -> None:
    req = MontageRequest(
        clips=(ClipCandidate("a", 4, 4), ClipCandidate("b", 6, 6)),
        pace_target=(4, 6),
    )
    s = optimize_rhythmic_montage(req)
    dag = compile_montage_to_timeline_dag(s)
    assert dag.total_frames == 10
    assert set(dag.nodes) == {"v1", "v1_clip_0", "v1_clip_1"}


def test_compile_montage_rejects_bad_status() -> None:
    from nexus_ai_agent.creative.composition.montage_optimizer import MontageSchedule

    bad = MontageSchedule(status=SolverStatus.INFEASIBLE)
    with pytest.raises(MontageError):
        compile_montage_to_timeline_dag(bad)


def test_render_spec_is_deterministic_and_hashed() -> None:
    dag = TimelineDAG()
    dag.add_node(Node("v1", NodeKind.TRACK))
    dag.add_node(Node("c0", NodeKind.CLIP, "a", 10))
    dag.add_node(Node("t0", NodeKind.TRANSITION, "xfade", 5))
    dag.add_node(Node("a0", NodeKind.AUDIO_BUS, "mix", 15))
    dag.add_node(Node("e0", NodeKind.EFFECT, "grade", 5))
    dag.add_edge(Edge("v1", "c0", "contains"))
    dag.add_edge(Edge("c0", "t0", "finish_to_start"))
    dag.add_edge(Edge("v1", "a0", "contains"))
    dag.add_edge(Edge("c0", "e0", "contains"))
    spec1 = compile_dag_to_render_spec(dag)
    spec2 = compile_dag_to_render_spec(dag)
    assert spec1["content_sha256"] == spec2["content_sha256"]
    assert len(spec1["content_sha256"]) == 64
    assert spec1["topological_order"] == list(dag.topological_order())
    assert all(n["operator"] == "src_over" for n in spec1["composite_nodes"])
    assert {n["label"] for n in spec1["composite_nodes"]} == {"xfade", "grade"}
    assert spec1["crossfades"][0]["kind"] == "crossfade"
    # the digest covers the spec without the digest field itself
    body = dict(spec1)
    digest = body.pop("content_sha256")
    recomputed = (
        __import__("hashlib")
        .sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        .hexdigest()
    )
    assert recomputed == digest


def test_render_spec_with_plan_and_bad_fps() -> None:
    dag = TimelineDAG()
    dag.add_node(Node("v1", NodeKind.TRACK))
    spec = compile_dag_to_render_spec(dag, plan=None, fps=24)
    assert spec["fps"] == 24
    with pytest.raises(MontageError):
        compile_dag_to_render_spec(dag, fps=0)


def test_render_spec_propagates_cycle_error() -> None:
    dag = TimelineDAG()
    dag.add_node(Node("a", NodeKind.CLIP))
    dag.add_node(Node("b", NodeKind.CLIP))
    dag.add_edge(Edge("a", "b"))
    dag.add_edge(Edge("b", "a"))
    with pytest.raises(CyclicGraphError):
        compile_dag_to_render_spec(dag)


def test_validation_errors_are_typed() -> None:
    with pytest.raises(MontageError):
        ClipCandidate("", 1, 2)
    with pytest.raises(MontageError):
        ClipCandidate("a", 0, 2)
    with pytest.raises(MontageError):
        ClipCandidate("a", 5, 2)
    with pytest.raises(MontageError):
        BeatAnchor(-1)
    with pytest.raises(MontageError):
        BeatAnchor(1, Fraction(0))
    with pytest.raises(MontageError):
        BeatAnchor(1, Fraction(2))
    with pytest.raises(MontageError):
        MontageWeights(beat=Fraction(-1))
    with pytest.raises(MontageError):
        MontageRequest(clips=())
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), tension=(Fraction(0), Fraction(0)))
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), jump_cost=(Fraction(0), Fraction(0)))
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), max_states=0)
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), pace_target=(0, 5))
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), tension=(Fraction(2),))
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 2),), jump_cost=(Fraction(-1),))


def test_schedule_as_dict_round_trip() -> None:
    req = MontageRequest(
        clips=(ClipCandidate("a", 4, 4),),
        beats=(BeatAnchor(4),),
        tension=(Fraction(0),),
        pace_target=(4, 4),
    )
    s = optimize_rhythmic_montage(req)
    d = s.as_dict()
    assert d["status"] == SolverStatus.PROVEN_OPTIMAL.value
    assert d["durations"] == [4]
    assert d["clip_ids"] == ["a"]
    assert d["total_cost"] == str(s.total_cost)
    assert d["beat_residuals"] == [[4, 0]]


def test_jump_cost_from_empty_sequence() -> None:
    assert jump_cost_from_setups(object()) == ()


def test_render_spec_with_plan_branch() -> None:
    from nexus_ai_agent.creative.episode_causality_lab.models import ShotCandidate, SourceRef
    from nexus_ai_agent.creative.episode_causality_lab.plan import (
        PlanResult,
        PlanStep,
        plan_edit,
    )
    from nexus_ai_agent.creative.episode_causality_lab.statuses import TriValue

    cand = ShotCandidate(
        id="s0",
        asset_ref="asset://x",
        covers_beats=("b0",),
        frames=8,
        preference=0,
        evidence_status=TriValue.TRUE,
        evidence_ref="ev://1",
        source=SourceRef(path="ep1/sc1"),
    )
    plan = plan_edit(
        __import__(
            "nexus_ai_agent.creative.episode_causality_lab.plan", fromlist=["PlanRequest"]
        ).PlanRequest(beats=("b0",), candidates=(cand,), frame_budget=10)
    )
    assert isinstance(plan, PlanResult)
    assert plan.steps and isinstance(plan.steps[0], PlanStep)
    dag = compile_montage_to_timeline_dag(
        optimize_rhythmic_montage(MontageRequest(clips=(ClipCandidate("a", 4, 4),)))
    )
    spec = compile_dag_to_render_spec(dag, plan=plan)
    assert spec["plan"]["steps"] == ["s0"]
    assert spec["plan"]["status"] == plan.status.value


def test_render_spec_track_only_node() -> None:
    dag = TimelineDAG()
    dag.add_node(Node("v1", NodeKind.TRACK, "track"))
    spec = compile_dag_to_render_spec(dag)
    assert spec["video_tracks"] == []
    assert spec["audio_tracks"] == []


def test_first_jump_cost_is_ignored_but_later_ones_are_charged() -> None:
    # C_jump[0] has no predecessor and must be ignored; every later entry is
    # charged exactly once (the term is applied only for i > 0).
    base = MontageRequest(
        clips=(ClipCandidate("a", 4, 4), ClipCandidate("b", 4, 4)),
        tension=(Fraction(0), Fraction(0)),
        pace_target=(4, 4),
        weights=MontageWeights(beat=Fraction(0), pace=Fraction(0), continuity=Fraction(1)),
    )
    zero = optimize_rhythmic_montage(base)

    def with_jump(j0: int, j1: int) -> Fraction:
        return optimize_rhythmic_montage(
            MontageRequest(
                clips=base.clips,
                tension=base.tension,
                pace_target=base.pace_target,
                jump_cost=(Fraction(j0), Fraction(j1)),
                weights=base.weights,
            )
        ).total_cost

    assert with_jump(7, 0) == zero.total_cost  # jump[0] ignored
    assert with_jump(0, 7) - zero.total_cost == Fraction(7)  # jump[1] charged
