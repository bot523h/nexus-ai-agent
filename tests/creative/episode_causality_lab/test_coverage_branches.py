"""Targeted coverage for defensive and under-exercised branches.

These tests are white-box: they drive specific guard paths so that the package's
defensive behaviour is exercised, not merely present. They are labelled
``coverage`` in intent; each still asserts real behaviour, never just "no crash".
"""

from __future__ import annotations

import atlas_fixtures as fx

from nexus_ai_agent.creative.episode_causality_lab import (
    EventKind,
    FactKey,
    FactRelation,
    Status,
    TemporalConstraint,
    TriValue,
    parse_story,
    suggest_repairs,
)
from nexus_ai_agent.creative.episode_causality_lab.analysis import _collect_notes
from nexus_ai_agent.creative.episode_causality_lab.causal import (
    CausalEngine,
    Finding,
    knows_fact_key,
)
from nexus_ai_agent.creative.episode_causality_lab.plan import PlanResult
from nexus_ai_agent.creative.episode_causality_lab.statuses import SolverStatus
from nexus_ai_agent.creative.episode_causality_lab.temporal import (
    TemporalResult,
    _weigh_cycle,
    check_consistency,
)


def test_transfer_without_prop_is_a_noop() -> None:
    ev = fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob")
    report = CausalEngine().analyze(fx.base_doc(events=(ev,)))
    assert report.findings == ()


def test_reveal_without_fact_key_is_a_noop() -> None:
    ev = fx.event("e1", EventKind.REVEAL_FACT, actor="alice", target="bob")
    report = CausalEngine().analyze(fx.base_doc(events=(ev,)))
    assert report.findings == ()


def test_reveal_grants_knowledge_to_target() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.REVEAL_FACT, actor="alice", target="bob", fact_key=key),),
    )
    report = CausalEngine().analyze(doc)
    assert report.final_state.get(knows_fact_key("bob", key)) is TriValue.TRUE


def test_injure_and_heal_without_actor_are_noops() -> None:
    events = (
        fx.event("e1", EventKind.INJURE, order=0),
        fx.event("e2", EventKind.HEAL, order=1),
    )
    report = CausalEngine().analyze(fx.base_doc(events=events))
    assert report.findings == ()


def test_set_state_without_value_is_a_noop() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.ALIVE, target_id="alice")
    ev = fx.event("e1", EventKind.SET_STATE, actor="alice", fact_key=key)
    report = CausalEngine().analyze(fx.base_doc(events=(ev,)))
    assert report.findings == ()


def test_learn_fact_without_actor_is_a_noop() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    ev = fx.event("e1", EventKind.LEARN_FACT, fact_key=key)
    report = CausalEngine().analyze(fx.base_doc(events=(ev,)))
    assert report.findings == ()


def test_enter_location_without_target_is_a_noop() -> None:
    ev = fx.event("e1", EventKind.ENTER_LOCATION, actor="alice")
    report = CausalEngine().analyze(fx.base_doc(events=(ev,)))
    assert report.findings == ()


def test_temporal_node_empty_is_refused() -> None:
    import pytest

    from nexus_ai_agent.creative.episode_causality_lab import StoryValidationError

    with pytest.raises(StoryValidationError):
        TemporalConstraint(id="c1", a="", b="episode:ep1", lower=0, upper=1, source=fx.SRC)


def test_temporal_node_bad_prefix_is_refused() -> None:
    import pytest

    from nexus_ai_agent.creative.episode_causality_lab import StoryValidationError

    with pytest.raises(StoryValidationError):
        TemporalConstraint(id="c1", a="bogus:ep1", b="episode:ep1", lower=0, upper=1, source=fx.SRC)


def test_suggestions_cover_conflict_and_flashback_and_cycle() -> None:
    conflict = Finding(
        code="POSSESSION_CONFLICT",
        status=Status.FAIL,
        message="x",
        source=fx.SRC,
        event_ids=("e1",),
    )
    flashback = Finding(
        code="FLASHBACK_ORDER",
        status=Status.NEEDS_REVIEW,
        message="y",
        source=fx.SRC,
        event_ids=("e1", "e2"),
    )
    cycle = check_consistency(
        (fx.constraint("c1", "a", "b", 5, 5), fx.constraint("c2", "b", "a", 5, 5)),
        extra_nodes=("a", "b"),
    )
    suggestions = suggest_repairs((conflict, flashback), cycle)
    codes = {s.addresses_code for s in suggestions}
    assert "POSSESSION_CONFLICT" in codes
    assert "FLASHBACK_ORDER" in codes
    assert "TEMPORAL_NEGATIVE_CYCLE" in codes


def test_suggestions_empty_for_unrecognised_code() -> None:
    unknown = Finding(
        code="UNDECLARED_POSSESSION_CHANGE",
        status=Status.FAIL,
        message="z",
        source=fx.SRC,
        event_ids=("e1",),
    )
    assert suggest_repairs((unknown,)) == ()


def test_collect_notes_for_unknown_and_resource_limited() -> None:
    unknown_temporal = TemporalResult(status=Status.UNKNOWN, reason="undecided")
    notes = _collect_notes(unknown_temporal, None)
    assert "temporal consistency could not be decided" in notes

    limited_temporal = TemporalResult(status=Status.RESOURCE_LIMIT, reason="too big")
    limited_plan = PlanResult(status=SolverStatus.RESOURCE_LIMIT, explanation="budget")
    notes = _collect_notes(limited_temporal, limited_plan)
    assert "too big" in notes and "budget" in notes


def test_weigh_cycle_skips_nodes_without_predecessor() -> None:
    ordered, ids, total = _weigh_cycle(["a", "b"], {"a": ("b", 3, "c1")})
    assert total == 3
    assert ids == ["c1"]
    assert ordered == ["a", "b", "a"]


def test_trivalue_negate_unknown() -> None:
    assert TriValue.UNKNOWN.negate() is TriValue.UNKNOWN


def test_analysis_with_plan_resource_limit_notes() -> None:
    cands = tuple(fx.shot(f"s{i}", (f"b{i}",), 1) for i in range(4))
    from nexus_ai_agent.creative.episode_causality_lab import PlanRequest, plan_edit

    req = PlanRequest(beats=tuple(f"b{i}" for i in range(4)), candidates=cands, frame_budget=100)
    # sanity: the clean plan is optimal; the notes path is covered elsewhere
    assert plan_edit(req).status is SolverStatus.PROVEN_OPTIMAL


def test_parse_document_with_fact_key_event_refs() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    ev = fx.event("e1", EventKind.LEARN_FACT, actor="bob", fact_key=key)
    doc = parse_story(fx.raw_doc(events=(ev,)))
    assert doc.events[0].fact_key == key
