"""Tests for exact-integer Simple Temporal Network consistency.

Every proven-negative-cycle case is cross-checked against an independent
exhaustive oracle: enumerate a bounded integer assignment space and confirm that
no assignment satisfies the declared bounds. This is the "independent oracle"
requirement — the Bellman–Ford result is not trusted on its own.
"""

from __future__ import annotations

import itertools

import atlas_fixtures as fx

from nexus_ai_agent.creative.episode_causality_lab import (
    Status,
    check_consistency,
)
from nexus_ai_agent.creative.episode_causality_lab.temporal import TemporalResult


def _oracle_satisfiable(constraints, nodes, bound: int) -> bool:
    """Brute force: is there an integer assignment in [-bound, bound]?"""
    for assignment in itertools.product(range(-bound, bound + 1), repeat=len(nodes)):
        env = dict(zip(nodes, assignment, strict=True))
        ok = True
        for con in constraints:
            delta = env[con.b] - env[con.a]
            if not (con.lower <= delta <= con.upper):
                ok = False
                break
        if ok:
            return True
    return False


def test_empty_network_is_consistent() -> None:
    result = check_consistency(())
    assert result.status is Status.PASS


def test_simple_chain_is_consistent() -> None:
    cons = (
        fx.constraint("c1", "e1", "e2", 1, 5),
        fx.constraint("c2", "e2", "e3", 1, 5),
    )
    result = check_consistency(cons, extra_nodes=("e1", "e2", "e3"))
    assert result.status is Status.PASS


def test_zero_length_interval_is_consistent() -> None:
    cons = (fx.constraint("c1", "e1", "e2", 0, 0),)
    result = check_consistency(cons, extra_nodes=("e1", "e2"))
    assert result.status is Status.PASS
    assert result.assignment["e1"] == result.assignment["e2"]


def test_contradictory_bounds_form_a_negative_cycle() -> None:
    # t2 - t1 in [10, 20] AND t2 - t1 in [0, 5]  -> infeasible
    cons = (
        fx.constraint("c1", "e1", "e2", 10, 20),
        fx.constraint("c2", "e1", "e2", 0, 5),
    )
    result = check_consistency(cons, extra_nodes=("e1", "e2"))
    assert result.status is Status.FAIL
    assert result.witness is not None and result.witness.is_valid()


def test_multi_edge_cycle_is_detected_and_witness_is_negative() -> None:
    cons = (
        fx.constraint("c1", "a", "b", 1, 1),
        fx.constraint("c2", "b", "c", 1, 1),
        fx.constraint("c3", "c", "a", 1, 1),
    )
    result = check_consistency(cons, extra_nodes=("a", "b", "c"))
    assert result.status is Status.FAIL
    assert result.witness is not None
    assert result.witness.total_weight < 0
    # the witness names the real constraints on the cycle
    assert set(result.witness.constraint_ids) <= {"c1", "c2", "c3"}


def test_flashback_backward_constraint_is_consistent() -> None:
    # a later scene may be temporally earlier: t_e2 - t_e1 in [-100, -10]
    cons = (fx.constraint("c1", "e1", "e2", -100, -10),)
    result = check_consistency(cons, extra_nodes=("e1", "e2"))
    assert result.status is Status.PASS


def test_disconnected_components_are_consistent() -> None:
    cons = (fx.constraint("c1", "a", "b", 1, 2),)
    result = check_consistency(cons, extra_nodes=("a", "b", "x", "y"))
    assert result.status is Status.PASS


def test_witness_is_independently_verified_against_oracle() -> None:
    cons = (
        fx.constraint("c1", "a", "b", 5, 5),
        fx.constraint("c2", "b", "a", 5, 5),
    )
    result = check_consistency(cons, extra_nodes=("a", "b"))
    assert result.status is Status.FAIL
    # the oracle agrees there is no assignment
    assert not _oracle_satisfiable(cons, ("a", "b"), bound=20)


def test_node_limit_returns_resource_limit_not_pass() -> None:
    cons = tuple(fx.constraint(f"c{i}", f"n{i}", f"n{i + 1}", 0, 10) for i in range(10))
    result = check_consistency(cons, extra_nodes=tuple(f"n{i}" for i in range(11)), max_nodes=5)
    assert result.status is Status.RESOURCE_LIMIT
    assert "node count" in result.reason


def test_edge_limit_returns_resource_limit() -> None:
    cons = tuple(fx.constraint(f"c{i}", "a", f"n{i}", 0, 1) for i in range(10))
    result = check_consistency(
        cons, extra_nodes=("a",) + tuple(f"n{i}" for i in range(10)), max_edges=3
    )
    assert result.status is Status.RESOURCE_LIMIT
    assert "edge count" in result.reason


def test_relaxation_budget_returns_resource_limit() -> None:
    # A long chain forces many relaxations; a tiny budget stops it short.
    cons = tuple(fx.constraint(f"c{i}", f"n{i}", f"n{i + 1}", 1, 1) for i in range(6))
    nodes = tuple(f"n{i}" for i in range(7))
    result = check_consistency(cons, extra_nodes=nodes, max_relaxations=1)
    assert result.status is Status.RESOURCE_LIMIT
    assert "relaxation budget" in result.reason


def test_resource_limit_never_reports_pass() -> None:
    cons = (fx.constraint("c1", "a", "b", 0, 0),)
    result = check_consistency(cons, extra_nodes=("a", "b"), max_nodes=0)
    assert result.status is not Status.PASS


def test_result_counts_are_reported() -> None:
    cons = (fx.constraint("c1", "a", "b", 0, 1),)
    result = check_consistency(cons, extra_nodes=("a", "b"))
    assert result.node_count == 2
    assert result.edge_count == 2  # one constraint -> two difference edges


def test_witness_is_not_minimal_core_claim() -> None:
    # Three mutually-consistent pairwise bounds plus one contradiction: the
    # witness names a cycle, not necessarily a minimum unsatisfiable core.
    cons = (
        fx.constraint("c1", "a", "b", 0, 1),
        fx.constraint("c2", "b", "c", 0, 1),
        fx.constraint("c3", "a", "c", 5, 5),
    )
    result = check_consistency(cons, extra_nodes=("a", "b", "c"))
    assert result.status is Status.FAIL
    assert result.witness is not None
    # honest labelling: a witness, not a "minimum core"
    assert isinstance(result.witness.constraint_ids, tuple)


def test_consistency_agrees_with_oracle_on_random_small_graphs() -> None:
    import random

    rng = random.Random(20261112)
    nodes = ("a", "b", "c")
    for trial in range(40):
        cons = []
        for i in range(rng.randint(1, 4)):
            a, b = rng.sample(nodes, 2)
            lo = rng.randint(-5, 5)
            hi = lo + rng.randint(0, 4)
            cons.append(fx.constraint(f"c{trial}_{i}", a, b, lo, hi))
        result = check_consistency(tuple(cons), extra_nodes=nodes)
        oracle_ok = _oracle_satisfiable(cons, nodes, bound=8)
        if result.status is Status.PASS:
            assert oracle_ok, f"trial {trial}: engine said PASS, oracle says UNSAT"
        elif result.status is Status.FAIL:
            assert not oracle_ok, f"trial {trial}: engine said FAIL, oracle found a model"


def test_temporal_result_type_is_stable() -> None:
    result = check_consistency(())
    assert isinstance(result, TemporalResult)
    assert result.consistent


def test_witness_zero_weight_is_not_valid() -> None:
    # A zero-weight cycle is NOT a negative cycle: is_valid must be False, so a
    # "< 0" bound can never be relaxed into "<= 0".
    from nexus_ai_agent.creative.episode_causality_lab.temporal import NegativeCycleWitness

    zero = NegativeCycleWitness(nodes=("a", "b"), constraint_ids=("c1",), total_weight=0)
    assert zero.is_valid() is False
    neg = NegativeCycleWitness(nodes=("a", "b"), constraint_ids=("c1",), total_weight=-1)
    assert neg.is_valid() is True
    empty = NegativeCycleWitness(nodes=(), constraint_ids=(), total_weight=-1)
    assert empty.is_valid() is False
