"""Tests for the TimelineDAG, time warp and J/L cuts."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.composition import (
    CyclicGraphError,
    Edge,
    JLCut,
    Node,
    NodeKind,
    TimelineDAG,
    TimeWarp,
    TimeWarpError,
    UnknownNodeError,
    compile_atlas_plan_to_dag,
    crossfade_gains,
)


def _chain() -> TimelineDAG:
    dag = TimelineDAG()
    dag.add_node(Node("a", NodeKind.CLIP, duration_frames=10))
    dag.add_node(Node("b", NodeKind.CLIP, duration_frames=20))
    dag.add_node(Node("c", NodeKind.TRANSITION, duration_frames=5))
    dag.add_edge(Edge("a", "b"))
    dag.add_edge(Edge("b", "c"))
    return dag


def test_topological_order() -> None:
    assert _chain().topological_order() == ("a", "b", "c")


def test_total_frames_critical_path() -> None:
    assert _chain().total_frames == 35


def test_empty_graph_total_frames() -> None:
    assert TimelineDAG().total_frames == 0


def test_cycle_is_reported_with_nodes() -> None:
    dag = _chain()
    dag.add_edge(Edge("c", "a"))
    with pytest.raises(CyclicGraphError) as exc:
        dag.topological_order()
    assert "a" in str(exc.value)


def test_unknown_node_edge_refused() -> None:
    dag = _chain()
    with pytest.raises(UnknownNodeError):
        dag.add_edge(Edge("a", "ghost"))
    with pytest.raises(UnknownNodeError):
        dag.add_edge(Edge("ghost", "a"))


def test_time_warp_identity_like() -> None:
    # control points on the diagonal -> identity warp
    w = TimeWarp(0.333, 0.333, 0.667, 0.667)
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert w.tau(t) == pytest.approx(t, abs=1e-9)


def test_time_warp_inverse_round_trip() -> None:
    w = TimeWarp(0.2, 0.1, 0.8, 0.9)
    for tau in (0.0, 0.3, 0.5, 0.7, 1.0):
        t = w.t_of_tau(tau)
        assert w.tau(t) == pytest.approx(tau, abs=1e-7)


def test_time_warp_slow_motion_rate() -> None:
    # a very slow segment: tau rises slowly near t=0.5
    w = TimeWarp(0.4, 0.05, 0.6, 0.1)
    assert w.rate(0.5) < 1.0


def test_time_warp_rejects_out_of_range() -> None:
    with pytest.raises(TimeWarpError):
        TimeWarp(1.5, 0.0, 0.5, 0.5)
    w = TimeWarp(0.3, 0.3, 0.7, 0.7)
    with pytest.raises(TimeWarpError):
        w.tau(1.5)
    with pytest.raises(TimeWarpError):
        w.t_of_tau(-0.1)


def test_time_warp_monotonicity_enforced() -> None:
    with pytest.raises(TimeWarpError):
        TimeWarp(0.3, 0.7, 0.7, 0.3)  # tau decreases
    TimeWarp(0.3, 0.7, 0.7, 0.3, reverse=True)  # allowed when reverse declared


def test_crossfade_constant_power() -> None:
    g0 = crossfade_gains(0.0)
    g1 = crossfade_gains(1.0)
    gm = crossfade_gains(0.5)
    assert g0 == pytest.approx((1.0, 0.0))
    assert g1 == pytest.approx((0.0, 1.0))
    # constant power: squares sum to 1
    assert gm[0] ** 2 + gm[1] ** 2 == pytest.approx(1.0)
    with pytest.raises(TimeWarpError):
        crossfade_gains(1.5)


def test_jl_cut_flags() -> None:
    j = JLCut(video_start_frame=100, audio_start_frame=90, overlap_frames=12)
    assert j.is_j_cut is True and j.is_l_cut is False
    assert j.audio_offset == 10
    lcut = JLCut(video_start_frame=100, audio_start_frame=110, overlap_frames=12)
    assert lcut.is_l_cut is True and lcut.is_j_cut is False
    assert lcut.audio_offset == -10


def test_jl_cut_gains() -> None:
    j = JLCut(video_start_frame=100, audio_start_frame=90, overlap_frames=10)
    out, inc = j.gains_at(5)
    assert out**2 + inc**2 == pytest.approx(1.0)
    # zero-overlap cut is a hard switch
    hard = JLCut(100, 100, 0)
    assert hard.gains_at(99) == (1.0, 0.0)
    assert hard.gains_at(100) == (0.0, 1.0)


class _FakeStep:
    def __init__(self, cid: str, frames: int) -> None:
        self.candidate_id = cid
        self.frames = frames


class _FakePlan:
    def __init__(self, steps: list[_FakeStep]) -> None:
        self.steps = steps
        self.total_frames = sum(s.frames for s in steps)


def test_compile_atlas_plan_to_dag() -> None:
    plan = _FakePlan([_FakeStep("s1", 10), _FakeStep("s2", 20)])
    dag = compile_atlas_plan_to_dag(plan)
    order = dag.topological_order()
    assert "v1" in order
    assert order[0] == "v1"
    assert dag.nodes["v1_clip_0"].duration_frames == 10
    assert dag.total_frames == 30


def test_time_warp_requires_monotone_t() -> None:
    with pytest.raises(TimeWarpError):
        TimeWarp(0.8, 0.2, 0.2, 0.8)  # t2 < t1


def test_time_warp_bisection_fallback_on_flat_derivative() -> None:
    # t1 = 0 -> the Bezier derivative is 0 at s=0, forcing the bisection path
    w = TimeWarp(0.0, 0.0, 1.0, 1.0)
    assert w.tau(0.0) == pytest.approx(0.0, abs=1e-9)
    assert w.tau(1.0) == pytest.approx(1.0, abs=1e-9)
    assert w.tau(0.5) == pytest.approx(0.5, abs=1e-6)


def test_time_warp_rate_zero_dt() -> None:
    w = TimeWarp(0.3, 0.3, 0.7, 0.7)
    assert w.rate(0.5, dt=0.0) == 0.0


def test_solve_bisection_fallback_when_derivative_vanishes(monkeypatch) -> None:
    """The bisection fallback runs when Newton cannot make progress."""
    w = TimeWarp(0.3, 0.3, 0.7, 0.7)
    # force the Newton step to bail immediately by reporting a zero derivative
    monkeypatch.setattr(TimeWarp, "_bezier_d", staticmethod(lambda s, p1, p2: 0.0))
    # Newton bails (df<=0); the bisection fallback must still return a valid root
    root = w._solve(0.3, w.t1, w.t2)
    assert 0.0 <= root <= 1.0
    assert w._bezier(root, w.t1, w.t2) == pytest.approx(0.3, abs=1e-6)


def test_solve_returns_immediately_when_already_exact() -> None:
    w = TimeWarp(0.0, 0.0, 1.0, 1.0)
    # target 0.0 is already an exact root at s=0
    assert w._solve(0.0, 0.0, 1.0) == pytest.approx(0.0, abs=1e-12)
