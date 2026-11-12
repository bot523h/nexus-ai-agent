"""Tests for incremental continuity analysis.

The load-bearing invariant is ``incremental_result == full_recomputation_result``
for the same input, including findings, statuses, fact rows and event order. It
is checked both directly and against seeded random mutations across episodes.
"""

from __future__ import annotations

import random

import atlas_fixtures as fx

from nexus_ai_agent.creative.episode_causality_lab import (
    CausalEngine,
    EventKind,
    FactRelation,
    IncrementalEngine,
    Status,
    TriValue,
    full_recompute,
)
from nexus_ai_agent.creative.episode_causality_lab.causal import CausalReport


def _report_eq(a: CausalReport, b: CausalReport) -> bool:
    return (
        a.findings == b.findings
        and a.status is b.status
        and a.event_order == b.event_order
        and a.fact_rows == b.fact_rows
        and a.dependencies == b.dependencies
    )


def test_first_analysis_without_previous_falls_back_to_full() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    result = IncrementalEngine().update(None, None, doc)
    assert result.mode == "full_fallback"
    assert _report_eq(result.report, full_recompute(doc))


def test_no_change_reuses_previous_report() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    engine = IncrementalEngine()
    first = full_recompute(doc)
    result = engine.update(doc, first, doc)
    assert result.mode == "incremental"
    assert result.report is first


def test_content_change_matches_full_recomputation() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        )
    )
    cur = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="alice", target="street"),
        )
    )
    prev_report = full_recompute(prev)
    result = engine.update(prev, prev_report, cur)
    assert result.mode == "incremental"
    assert result.suffix_start == 1
    assert _report_eq(result.report, full_recompute(cur))


def test_change_in_episode_one_affects_later_episode() -> None:
    """A change early must be reflected in analysis far later."""
    engine = IncrementalEngine()
    prev = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
            # in ep2 (scene sc2): bob now transfers the gun back
            fx.event(
                "e2",
                EventKind.TRANSFER_PROP,
                order=0,
                actor="bob",
                target="alice",
                prop="gun",
                scene="sc2",
            ),
        ),
    )
    # Flip the very first transfer so alice never possessed the gun.
    cur = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.FALSE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
            fx.event(
                "e2",
                EventKind.TRANSFER_PROP,
                order=0,
                actor="bob",
                target="alice",
                prop="gun",
                scene="sc2",
            ),
        ),
    )
    result = engine.update(prev, full_recompute(prev), cur)
    assert _report_eq(result.report, full_recompute(cur))
    # the episode-2 conflict must appear
    assert result.report.status is Status.FAIL


def test_initial_fact_change_falls_back_to_full() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc()
    cur = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),)
    )
    result = engine.update(prev, full_recompute(prev), cur)
    assert result.mode == "full_fallback"
    assert _report_eq(result.report, full_recompute(cur))


def test_closed_world_change_falls_back_to_full() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc(closed_world=False)
    cur = fx.base_doc(closed_world=True)
    result = engine.update(prev, full_recompute(prev), cur)
    assert result.mode == "full_fallback"


def test_event_set_change_falls_back_to_full() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    cur = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        )
    )
    result = engine.update(prev, full_recompute(prev), cur)
    assert result.mode == "full_fallback"


def test_incomplete_checkpoints_fall_back_to_full() -> None:
    engine = IncrementalEngine()
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    report = full_recompute(doc)
    truncated = CausalReport(
        status=report.status,
        findings=report.findings,
        final_state=report.final_state,
        event_order=report.event_order,
        fact_rows=report.fact_rows,
        dependencies=report.dependencies,
        checkpoints=(),
    )
    result = engine.update(doc, truncated, doc)
    assert result.mode == "full_fallback"


def test_seeded_random_mutations_match_full_recomputation() -> None:
    engine = IncrementalEngine()
    rng = random.Random(777)
    for trial in range(30):
        n_events = rng.randint(1, 6)
        prev_events = []
        cur_events = []
        for i in range(n_events):
            kind = rng.choice([EventKind.ENTER_LOCATION, EventKind.TRANSFER_PROP, EventKind.INJURE])
            actor = rng.choice(["alice", "bob"])
            if kind is EventKind.ENTER_LOCATION:
                e_prev = fx.event(f"e{i}", kind, order=i, actor=actor, target="room")
                e_cur = fx.event(
                    f"e{i}", kind, order=i, actor=actor, target=rng.choice(["room", "street"])
                )
            elif kind is EventKind.TRANSFER_PROP:
                e_prev = fx.event(f"e{i}", kind, order=i, actor=actor, target="bob", prop="gun")
                e_cur = fx.event(f"e{i}", kind, order=i, actor=actor, target="alice", prop="gun")
            else:
                e_prev = fx.event(f"e{i}", kind, order=i, actor=actor, prop="gun")
                e_cur = fx.event(f"e{i}", kind, order=i, actor=actor, prop="key")
            prev_events.append(e_prev)
            cur_events.append(e_cur)
        prev = fx.base_doc(
            initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
            events=tuple(prev_events),
        )
        cur = fx.base_doc(
            initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
            events=tuple(cur_events),
        )
        result = engine.update(prev, full_recompute(prev), cur)
        assert _report_eq(result.report, full_recompute(cur)), f"trial {trial} diverged"


def test_incremental_suffix_reproduces_dependency_map() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
            fx.event(
                "e2", EventKind.TRANSFER_PROP, order=1, actor="bob", target="alice", prop="gun"
            ),
        ),
    )
    cur = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
            # e2 changed target only; the dependency on e1 must still hold
            fx.event("e2", EventKind.TRANSFER_PROP, order=1, actor="bob", target="bob", prop="gun"),
        ),
    )
    result = engine.update(prev, full_recompute(prev), cur)
    assert result.report.dependencies["e2"] == ("e1",)


def test_incremental_result_is_exact_by_construction() -> None:
    doc = fx.base_doc()
    result = IncrementalEngine().update(None, None, doc)
    assert result.exact is True


def test_change_at_index_zero_replays_whole_suffix() -> None:
    engine = IncrementalEngine()
    prev = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    cur = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="street"),)
    )
    result = engine.update(prev, full_recompute(prev), cur)
    assert result.suffix_start == 0
    assert _report_eq(result.report, full_recompute(cur))


def test_repeated_incremental_updates_stay_exact() -> None:
    engine = IncrementalEngine()
    docs = []
    base_events = [
        fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
        fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        fx.event("e3", EventKind.TRANSFER_PROP, order=2, actor="alice", target="bob", prop="gun"),
    ]
    docs.append(fx.base_doc(events=tuple(base_events)))
    for _round in range(1, 4):
        modified = list(base_events)
        modified[-1] = fx.event(
            "e3", EventKind.TRANSFER_PROP, order=2, actor="alice", target="bob", prop="key"
        )
        docs.append(fx.base_doc(events=tuple(modified)))

    prev_doc = docs[0]
    prev_report = full_recompute(prev_doc)
    for doc in docs[1:]:
        result = engine.update(prev_doc, prev_report, doc)
        assert _report_eq(result.report, full_recompute(doc))
        prev_doc, prev_report = doc, result.report


def test_engine_uses_the_same_transition_as_full() -> None:
    # A direct structural check: the incremental engine delegates to the same
    # CausalEngine instance, so no second transition implementation exists.
    engine = IncrementalEngine()
    assert isinstance(engine._engine, CausalEngine)  # noqa: SLF001
