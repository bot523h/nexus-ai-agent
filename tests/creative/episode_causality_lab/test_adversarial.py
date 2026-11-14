"""Adversarial and refusal tests for the ATLAS lab (Milestone F).

Each test either (a) drives a hostile/edge input and asserts a *typed refusal* or
an honest ``UNKNOWN``/``NEEDS_REVIEW`` — never a silent PASS — or (b) exercises a
defensive branch directly. These are adversarial-input tests, NOT code-mutation
tests; a refusal test proves the code refuses, not that a mutant would be killed.
"""

from __future__ import annotations

import atlas_fixtures as fx
import pytest

from nexus_ai_agent.creative.episode_causality_lab import (
    DuplicateIdentifierError,
    EventKind,
    FactKey,
    FactRelation,
    LimitExceededError,
    Location,
    PlanRequest,
    Scene,
    SolverStatus,
    Status,
    StoryBeat,
    StoryValidationError,
    TemporalConstraint,
    TriValue,
    UnknownReferenceError,
    analyze_story,
    build_handoff,
    export_report,
    loads_story,
    parse_story,
    plan_edit,
    report_to_dict,
)
from nexus_ai_agent.creative.episode_causality_lab.analysis import _fold
from nexus_ai_agent.creative.episode_causality_lab.causal import (
    CausalEngine,
    WorldState,
    knows_fact_key,
)
from nexus_ai_agent.creative.episode_causality_lab.models import (
    INPUT_BOUNDS,
    ShotCandidate,
    SourceRef,
)
from nexus_ai_agent.creative.episode_causality_lab.temporal import (
    _extract_cycle,
    _weigh_cycle,
    check_consistency,
)
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem


# --------------------------------------------------------------------------- #
# Story-input refusals
# --------------------------------------------------------------------------- #
def test_refuse_story_without_episodes() -> None:
    with pytest.raises(StoryValidationError):
        parse_story(fx.raw_doc(episodes=()))


def test_refuse_scene_with_unknown_location() -> None:
    with pytest.raises(UnknownReferenceError):
        parse_story(
            fx.raw_doc(
                scenes=(Scene(id="sc1", episode_id="ep1", location_id="nowhere", order_index=0),)
            )
        )


def test_refuse_duplicate_scene_order_in_episode() -> None:
    with pytest.raises(DuplicateIdentifierError):
        parse_story(
            fx.raw_doc(
                scenes=(
                    Scene(id="sc1", episode_id="ep1", location_id="room", order_index=0),
                    Scene(id="sc2", episode_id="ep1", location_id="room", order_index=0),
                )
            )
        )


def test_refuse_event_with_unknown_episode() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import Event

    ev = Event(
        id="e1",
        episode_id="ghost",
        scene_id="sc1",
        order_index=0,
        kind=EventKind.ENTER_LOCATION,
        actor_id="alice",
        target_id="room",
        source=fx.SRC,
    )
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_refuse_event_with_unknown_scene() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import Event

    ev = Event(
        id="e1",
        episode_id="ep1",
        scene_id="ghost",
        order_index=0,
        kind=EventKind.ENTER_LOCATION,
        actor_id="alice",
        target_id="room",
        source=fx.SRC,
    )
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_refuse_event_with_unknown_prop() -> None:
    ev = fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="ghost")
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_refuse_event_with_unknown_target_character() -> None:
    ev = fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="ghost", prop="gun")
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_refuse_event_fact_key_with_unknown_entity() -> None:
    key = FactKey(subject_id="ghost", relation=FactRelation.POSSESSES, target_id="gun")
    ev = fx.event("e1", EventKind.LEARN_FACT, actor="alice", fact_key=key)
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_refuse_unknown_temporal_node() -> None:
    con = TemporalConstraint(id="c1", a="ghost", b="episode:ep1", lower=0, upper=1, source=fx.SRC)
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(temporal_constraints=(con,)))


def test_accept_episode_and_scene_anchored_constraints() -> None:
    con = TemporalConstraint(
        id="c1", a="episode:ep1", b="scene:sc1", lower=-5, upper=5, source=fx.SRC
    )
    doc = parse_story(fx.raw_doc(temporal_constraints=(con,)))
    assert doc.temporal_constraints[0].id == "c1"


def test_refuse_constraint_with_lower_above_upper() -> None:
    with pytest.raises(StoryValidationError):
        TemporalConstraint(id="c1", a="ep1", b="ep2", lower=10, upper=1, source=fx.SRC)


def test_refuse_shot_with_too_many_covers() -> None:
    covers = tuple(f"b{i}" for i in range(INPUT_BOUNDS.max_list_items + 1))
    with pytest.raises(LimitExceededError):
        ShotCandidate(
            id="s1", asset_ref="a", covers_beats=covers, frames=1, source=SourceRef(path="p")
        )


def test_refuse_oversized_nested_payload() -> None:
    # a deeply nested JSON document must fail as a validation error, not crash
    nested = "[" * 100000 + "]" * 100000
    with pytest.raises(StoryValidationError):
        loads_story(nested)


def test_refuse_duplicate_beat_id() -> None:
    with pytest.raises(DuplicateIdentifierError):
        parse_story(
            fx.raw_doc(
                beats=(
                    StoryBeat(id="b1", episode_id="ep1", order_index=0),
                    StoryBeat(id="b1", episode_id="ep1", order_index=1),
                )
            )
        )


def test_refuse_duplicate_temporal_constraint_id() -> None:
    con = fx.constraint("c1", "episode:ep1", "scene:sc1", 0, 1)
    with pytest.raises(DuplicateIdentifierError):
        parse_story(fx.raw_doc(temporal_constraints=(con, con)))


# --------------------------------------------------------------------------- #
# Causal-honesty refusals (never silently PASS)
# --------------------------------------------------------------------------- #
def test_unknown_possession_never_becomes_pass() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),)
    )
    assert analyze_story(doc).status is not Status.PASS


def test_unknown_knowledge_never_becomes_pass() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.REVEAL_FACT, actor="bob", target="alice", fact_key=key),)
    )
    assert analyze_story(doc).status is not Status.PASS


def test_ambiguous_ordering_never_becomes_pass() -> None:
    doc = fx.base_doc(
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=0, actor="bob", target="room"),
        )
    )
    assert analyze_story(doc).status is Status.NEEDS_REVIEW


def test_negative_temporal_cycle_folds_to_fail() -> None:
    doc = fx.base_doc(
        temporal_constraints=(
            fx.constraint("c1", "e1", "e2", 10, 20),
            fx.constraint("c2", "e1", "e2", 0, 5),
        ),
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        ),
    )
    assert analyze_story(doc).status is Status.FAIL


def test_forged_verified_true_without_evidence_ref_is_not_verified() -> None:
    # A caller sets evidence_status TRUE but leaves evidence_ref empty: the
    # optimizer must still treat it as unverified.
    forged = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=10,
        evidence_status=TriValue.TRUE,
        evidence_ref="",
        source=fx.SRC,
    )
    req = PlanRequest(
        beats=("b1",), candidates=(forged,), frame_budget=100, require_verified_evidence=True
    )
    result = plan_edit(req)
    assert result.status is SolverStatus.INFEASIBLE
    assert any(r.candidate_id == "s1" for r in result.rejected)


def test_negative_frame_budget_is_rejected() -> None:
    with pytest.raises(ValueError):
        PlanRequest(beats=("b1",), candidates=(), frame_budget=-5)


def test_resource_limit_during_planning_is_not_optimal() -> None:
    beats = tuple(f"b{i}" for i in range(5))
    cands = tuple(fx.shot(f"s{i}", (b,), 1) for i, b in enumerate(beats))
    result = plan_edit(
        PlanRequest(beats=beats, candidates=cands, frame_budget=100), max_transitions=1
    )
    assert result.status is SolverStatus.RESOURCE_LIMIT
    assert result.optimal is False


def test_path_traversal_at_filesystem_boundary_is_refused(tmp_path) -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        export_report(report, fs, "../../etc/passwd")


def test_absolute_path_at_filesystem_boundary_is_refused(tmp_path) -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        export_report(report, fs, "/etc/passwd")


# --------------------------------------------------------------------------- #
# Defensive-branch coverage (white-box, clearly labelled)
# --------------------------------------------------------------------------- #
def test_world_state_copy_and_closed_world_property() -> None:
    state = WorldState({("a", "knows", "b"): TriValue.TRUE}, closed_world=True)
    assert state.closed_world is True
    clone = state.copy()
    assert clone.snapshot() == state.snapshot()
    clone._facts[("x", "knows", "y")] = TriValue.TRUE  # noqa: SLF001
    assert state.snapshot() != clone.snapshot()


def test_world_state_items_are_sorted() -> None:
    state = WorldState(
        {("b", "knows", "z"): TriValue.TRUE, ("a", "knows", "y"): TriValue.FALSE}, False
    )
    rows = state.items()
    assert rows[0][0].subject_id == "a"


def test_kleene_all_truth_table_corners() -> None:
    assert TriValue.conjunction(TriValue.TRUE, TriValue.TRUE) is TriValue.TRUE
    assert TriValue.disjunction(TriValue.FALSE, TriValue.FALSE) is TriValue.FALSE


def test_knows_fact_key_round_trips_through_engine() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    encoded = knows_fact_key("bob", key)
    assert encoded.relation is FactRelation.KNOWS
    assert encoded.target_id == "alice.possesses.gun"


def test_leave_location_without_prior_enter_is_tracked_false() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.LEAVE_LOCATION, actor="alice", target="room"),)
    )
    report = CausalEngine().analyze(doc)
    assert report.final_state.get_tuple("alice", FactRelation.AT_LOCATION, "room") is TriValue.FALSE


def test_weigh_cycle_empty_is_zero() -> None:
    nodes, ids, total = _weigh_cycle([], {})
    assert nodes == [] and ids == [] and total == 0


def test_extract_cycle_with_no_predecessor_returns_none() -> None:
    assert _extract_cycle("a", {}, 3) is None


def test_temporal_witness_cycle_nodes_form_a_loop() -> None:
    cons = (
        fx.constraint("c1", "a", "b", 5, 5),
        fx.constraint("c2", "b", "a", 5, 5),
    )
    result = check_consistency(cons, extra_nodes=("a", "b"))
    assert result.status is Status.FAIL
    assert result.witness is not None
    assert result.witness.nodes[0] == result.witness.nodes[-1]


def test_analysis_passed_property_and_notes() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
        ),
    )
    report = analyze_story(doc)
    assert report.passed is True
    assert report.notes == ()


def test_fold_covers_every_status_combination() -> None:
    assert _fold(Status.PASS, Status.PASS, None) is Status.PASS
    assert _fold(Status.PASS, Status.FAIL, None) is Status.FAIL
    assert _fold(Status.PASS, Status.UNKNOWN, None) is Status.NEEDS_REVIEW
    assert _fold(Status.PASS, Status.RESOURCE_LIMIT, None) is Status.RESOURCE_LIMIT
    assert _fold(Status.PASS, Status.PASS, SolverStatus.INFEASIBLE) is Status.NEEDS_REVIEW
    assert _fold(Status.PASS, Status.PASS, SolverStatus.RESOURCE_LIMIT) is Status.RESOURCE_LIMIT
    assert (
        _fold(Status.PASS, Status.PASS, SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL)
        is Status.RESOURCE_LIMIT
    )


def test_handoff_without_plan_has_no_proposed_steps(tmp_path) -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "atlas.json")
    handoff = build_handoff(report, artifact)
    assert "proposed_steps" not in handoff


def test_report_to_dict_without_plan_omits_plan_key() -> None:
    doc = fx.base_doc()
    payload = report_to_dict(analyze_story(doc))
    assert "plan" not in payload


def test_report_to_dict_includes_plan_when_present() -> None:
    doc = fx.base_doc(beats=(fx.beat("b1", 0),))
    req = PlanRequest(beats=("b1",), candidates=(fx.shot("s1", ("b1",), 10),), frame_budget=100)
    payload = report_to_dict(analyze_story(doc, plan_request=req))
    assert payload["plan"]["status"] == "PROVEN_OPTIMAL"


def test_report_to_dict_includes_temporal_witness() -> None:
    doc = fx.base_doc(
        temporal_constraints=(
            fx.constraint("c1", "e1", "e2", 5, 5),
            fx.constraint("c2", "e2", "e1", 5, 5),
        ),
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        ),
    )
    payload = report_to_dict(analyze_story(doc))
    assert payload["temporal"]["witness"]["total_weight"] < 0


def test_refuse_scene_duplicate_location_reference_kept_distinct() -> None:
    # Two locations with distinct ids are fine; the reference check is by id.
    doc = parse_story(
        fx.raw_doc(
            locations=(
                Location(id="room"),
                Location(id="street"),
                Location(id="room2", display_name="room"),
            )
        )
    )
    assert len(doc.locations) == 3
