"""Unit tests for the three-valued causal engine and continuity findings."""

from __future__ import annotations

import atlas_fixtures as fx
import pytest

from nexus_ai_agent.creative.episode_causality_lab import (
    CausalEngine,
    DeclaredFact,
    EventKind,
    FactKey,
    FactRelation,
    Status,
    TriValue,
)
from nexus_ai_agent.creative.episode_causality_lab.causal import (
    CausalFindingCode,
    knows_fact_key,
)


def _analyze(**overrides):
    doc = fx.base_doc(**overrides)
    return CausalEngine().analyze(doc)


def test_transfer_from_known_non_possessor_is_a_conflict() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.FALSE),),
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),),
    )
    report = CausalEngine().analyze(doc)
    codes = {f.code for f in report.findings}
    assert CausalFindingCode.POSSESSION_CONFLICT in codes
    assert report.status is Status.FAIL


def test_transfer_with_unknown_possession_is_needs_review_not_fail() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),)
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.POSSESSION_UNKNOWN)
    assert report.status is Status.NEEDS_REVIEW


def test_valid_transfer_after_declared_possession_passes() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),),
    )
    report = CausalEngine().analyze(doc)
    assert report.status is Status.PASS
    assert report.final_state.get_tuple("bob", FactRelation.POSSESSES, "gun") is TriValue.TRUE
    assert report.final_state.get_tuple("alice", FactRelation.POSSESSES, "gun") is TriValue.FALSE


def test_reveal_without_declared_knowledge_is_a_conflict() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    knows = knows_fact_key("bob", key)
    doc = fx.base_doc(
        initial_facts=(
            fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),
            DeclaredFact(key=knows, value=TriValue.FALSE, source=fx.SRC),
        ),
        events=(fx.event("e1", EventKind.REVEAL_FACT, actor="bob", target="alice", fact_key=key),),
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.KNOWLEDGE_PREREQUISITE_UNMET)
    assert report.status is Status.FAIL


def test_reveal_with_unknown_knowledge_is_needs_review() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.REVEAL_FACT, actor="bob", target="alice", fact_key=key),)
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.KNOWLEDGE_UNKNOWN)
    assert report.status is Status.NEEDS_REVIEW


def test_learn_then_reveal_passes() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.LEARN_FACT, order=0, actor="bob", fact_key=key),
            fx.event(
                "e2", EventKind.REVEAL_FACT, order=1, actor="bob", target="alice", fact_key=key
            ),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.status is Status.PASS


def test_enter_and_leave_location_track_state() -> None:
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.LEAVE_LOCATION, order=1, actor="alice", target="room"),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.final_state.get_tuple("alice", FactRelation.AT_LOCATION, "room") is TriValue.FALSE


def test_injure_then_heal_track_state() -> None:
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.INJURE, order=0, actor="alice", prop="gun"),
            fx.event("e2", EventKind.HEAL, order=1, actor="alice", prop="gun"),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.final_state.get_tuple("alice", FactRelation.INJURED, "gun") is TriValue.FALSE


def test_set_state_writes_a_declared_fact() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.ALIVE, target_id="alice")
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.SET_STATE, actor="alice", fact_key=key, value=TriValue.FALSE),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.final_state.get(key) is TriValue.FALSE


def test_ambiguous_order_is_reported() -> None:
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=0, actor="bob", target="room"),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.AMBIGUOUS_ORDER)
    assert report.status is Status.NEEDS_REVIEW


def test_flashback_order_is_flagged_not_silently_reordered() -> None:
    doc = fx.base_doc(
        events=(
            fx.event(
                "e1",
                EventKind.ENTER_LOCATION,
                order=0,
                actor="alice",
                target="room",
                story_tick=100,
            ),
            fx.event(
                "e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room", story_tick=50
            ),
        )
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.FLASHBACK_ORDER)
    # the declared edit order is preserved, not silently swapped
    assert report.event_order == ("e1", "e2")


def test_closed_world_makes_absent_facts_false() -> None:
    doc = fx.base_doc(
        closed_world=True,
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),),
    )
    report = CausalEngine().analyze(doc)
    # under a closed world, alice's possession is FALSE, not UNKNOWN -> conflict
    assert report.findings_for(CausalFindingCode.POSSESSION_CONFLICT)
    assert report.status is Status.FAIL


def test_open_world_absent_fact_is_unknown() -> None:
    doc = fx.base_doc(closed_world=False)
    report = CausalEngine().analyze(doc)
    assert report.final_state.get_tuple("alice", FactRelation.POSSESSES, "gun") is TriValue.UNKNOWN


def test_findings_are_deterministic_across_runs() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.FALSE),),
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),),
    )
    first = CausalEngine().analyze(doc)
    second = CausalEngine().analyze(doc)
    assert first.findings == second.findings
    assert first.fact_rows == second.fact_rows


def test_dependency_tracks_last_writer_of_a_fact() -> None:
    doc = fx.base_doc(
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
    report = CausalEngine().analyze(doc)
    assert report.dependencies["e2"] == ("e1",)


def test_second_transfer_from_emptied_holder_is_a_conflict() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
            fx.event(
                "e2", EventKind.TRANSFER_PROP, order=1, actor="alice", target="bob", prop="gun"
            ),
        ),
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.POSSESSION_CONFLICT)


def test_three_valued_negation_and_kleene_ops() -> None:
    assert TriValue.UNKNOWN.negate() is TriValue.UNKNOWN
    assert TriValue.TRUE.negate() is TriValue.FALSE
    assert TriValue.conjunction(TriValue.FALSE, TriValue.UNKNOWN) is TriValue.FALSE
    assert TriValue.conjunction(TriValue.TRUE, TriValue.UNKNOWN) is TriValue.UNKNOWN
    assert TriValue.disjunction(TriValue.TRUE, TriValue.UNKNOWN) is TriValue.TRUE
    assert TriValue.disjunction(TriValue.FALSE, TriValue.UNKNOWN) is TriValue.UNKNOWN


def test_trivalue_is_not_a_boolean() -> None:
    with pytest.raises(TypeError):
        bool(TriValue.TRUE)


def test_unknown_knowledge_reveal_is_needs_review_not_fail() -> None:
    # An actor who is NOT declared to know the fact reveals it; the state is
    # UNKNOWN -> NEEDS_REVIEW (not FAIL, not PASS).
    key = FactKey(subject_id="alice", relation=FactRelation.KNOWS, target_id="secret")
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.REVEAL_FACT, actor="alice", fact_key=key),),
    )
    report = CausalEngine().analyze(doc)
    assert report.findings_for(CausalFindingCode.KNOWLEDGE_UNKNOWN)
    assert report.status is Status.NEEDS_REVIEW
    assert not report.findings_for(CausalFindingCode.KNOWLEDGE_PREREQUISITE_UNMET)
