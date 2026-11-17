"""Cross-module adversarial / forgery probes for the AETHEL pipeline (Wave 6).

These are *input/refusal* tests (not code-mutation tests). Each one exercises a
real boundary spanning two or more modules:
``Fountain -> StoryDocument -> CausalEngine -> TemporalNetwork -> ShotPlanner ->
MontageOptimizer -> TimelineDAG``. Every case asserts a typed refusal, an honest
``UNKNOWN``/``NEEDS_REVIEW``/``FAIL`` status, or a proven property — never a
silent PASS.

The document builders are local to this module so it depends on no sibling
fixture module (see ``tests/architecture/test_test_suite_hygiene.py``).
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

import director_fixtures as dfx
import pytest

from nexus_ai_agent.creative.composition import (
    ClipCandidate,
    MontageError,
    MontageRequest,
    MontageWeights,
    optimize_rhythmic_montage,
)
from nexus_ai_agent.creative.director import (
    ActorPlacement,
    ShotPlanRequest,
    compile_fountain_to_story_document,
    parse_fountain,
    plan_shot_sequence,
)
from nexus_ai_agent.creative.director.shot_planner import check_jump_cut
from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    CausalEngine,
    Character,
    DeclaredFact,
    Episode,
    Event,
    EventKind,
    FactKey,
    FactRelation,
    Location,
    Prop,
    Scene,
    SourceRef,
    Status,
    StoryDocument,
    TemporalConstraint,
    TimeOfDay,
    TriValue,
    check_consistency,
    loads_story,
    parse_story,
    plan_edit,
)
from nexus_ai_agent.creative.episode_causality_lab.plan import PlanRequest
from nexus_ai_agent.creative.episode_causality_lab.temporal import TemporalNetwork

_SRC = SourceRef(path="ep1/sc1", line=1)


def _fact(subject: str, relation: FactRelation, target: str, value: TriValue) -> DeclaredFact:
    return DeclaredFact(
        key=FactKey(subject_id=subject, relation=relation, target_id=target),
        value=value,
        source=_SRC,
    )


def _event(
    eid: str,
    kind: EventKind,
    *,
    order: int = 0,
    actor: str | None = None,
    target: str | None = None,
    prop: str | None = None,
    fact_key: FactKey | None = None,
    scene: str = "sc1",
) -> Event:
    return Event(
        id=eid,
        episode_id="ep1",
        scene_id=scene,
        order_index=order,
        kind=kind,
        actor_id=actor,
        target_id=target,
        prop_id=prop,
        fact_key=fact_key,
        source=_SRC,
    )


def _doc(**overrides: Any) -> StoryDocument:
    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "characters": (Character(id="alice"), Character(id="bob")),
        "props": (Prop(id="gun"), Prop(id="key")),
        "locations": (Location(id="room"), Location(id="street")),
        "episodes": (Episode(id="ep1", order_index=0), Episode(id="ep2", order_index=1)),
        "scenes": (
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="room",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
            Scene(
                id="sc2",
                episode_id="ep2",
                location_id="street",
                order_index=0,
                time_of_day=TimeOfDay.NIGHT,
            ),
        ),
    }
    fields.update(overrides)
    return StoryDocument(**fields)


def _constraint(cid: str, a: str, b: str, lower: int, upper: int) -> TemporalConstraint:
    return TemporalConstraint(id=cid, a=a, b=b, lower=lower, upper=upper, source=_SRC)


def _raw(**overrides: Any) -> dict[str, Any]:
    return _doc(**overrides).model_dump(mode="json")


# 1. A character declared dead cannot act in a later episode without a typed finding.
def test_character_after_death_cannot_transfer_prop() -> None:
    doc = _doc(
        initial_facts=(_fact("alice", FactRelation.ALIVE, "self", TriValue.FALSE),),
        events=(
            _event(
                "e1",
                EventKind.TRANSFER_PROP,
                actor="alice",
                target="bob",
                prop="gun",
                scene="sc2",
            ),
        ),
    )
    report = CausalEngine().analyze(doc)
    assert report.status is not Status.PASS


# 2. A fact revealed by a character declared not to know it is a conflict.
def test_reveal_of_undeclared_knowledge_across_scenes_is_a_conflict() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import knows_fact_key

    fact = FactKey(subject_id="bob", relation=FactRelation.POSSESSES, target_id="gun")
    knows = knows_fact_key("alice", fact)
    doc = _doc(
        initial_facts=(_fact(knows.subject_id, knows.relation, knows.target_id, TriValue.FALSE),),
        events=(_event("e1", EventKind.REVEAL_FACT, actor="alice", fact_key=fact),),
    )
    report = CausalEngine().analyze(doc)
    assert report.status is Status.FAIL


# 3. A negative STN cycle injected through flash-forward anchors is FAIL, with a witness.
def test_flash_forward_anchor_negative_cycle_has_witness() -> None:
    cons = (
        _constraint("c1", "scene:a", "scene:b", 10, 20),
        _constraint("c2", "scene:b", "scene:a", 10, 20),
    )
    result = check_consistency(cons, extra_nodes=("scene:a", "scene:b"))
    assert result.status is Status.FAIL
    assert result.witness is not None and result.witness.is_valid()


# 4. A shot sequence keeps every setup on the declared axis side (no silent flip).
def test_axis_flip_sequence_is_flagged_not_silently_accepted() -> None:
    actors = (ActorPlacement("a", 0.0, 0.0), ActorPlacement("b", 2.0, 0.0))
    seq = plan_shot_sequence(ShotPlanRequest(scene_index=0, actors=actors, primary_pair=("a", "b")))
    assert all(s.axis_side == seq.axis_side for s in seq.setups)


# 5. A degenerate montage request (negative duration window) is refused.
def test_negative_duration_window_is_refused() -> None:
    with pytest.raises(MontageError):
        ClipCandidate("a", 10, 4)
    with pytest.raises(MontageError):
        MontageRequest(clips=(ClipCandidate("a", 1, 1),), pace_target=(-1, 5))


# 6. An empty beat grid is honest: no invented beat alignment.
def test_empty_beat_grid_is_pure_pace_not_beat_alignment() -> None:
    req = MontageRequest(
        clips=(ClipCandidate("a", 2, 9),),
        beats=(),
        pace_target=(5, 5),
        weights=MontageWeights(beat=Fraction(1), pace=Fraction(1), continuity=Fraction(1)),
    )
    s = optimize_rhythmic_montage(req)
    assert s.durations == (5,)
    assert s.beat_residuals == ((0, 0),)  # documented "no beat" sentinel


# 7. Negative penalty weights are refused (no reward for a jump cut).
def test_negative_penalty_weight_is_refused() -> None:
    with pytest.raises(MontageError):
        MontageWeights(beat=Fraction(-1))
    with pytest.raises(MontageError):
        MontageWeights(continuity=Fraction(-1))


# 8. A non-monotone time warp is refused (no invented reverse without a declaration).
def test_non_monotone_time_warp_is_refused() -> None:
    from nexus_ai_agent.creative.composition import TimeWarp, TimeWarpError

    with pytest.raises(TimeWarpError):
        TimeWarp(0.2, 0.8, 0.8, 0.2)  # tau decreasing without reverse=True
    TimeWarp(0.2, 0.8, 0.8, 0.2, reverse=True)  # explicitly declared -> allowed


# 9. Budget honesty: a truncated montage search is FEASIBLE_NOT_PROVEN_OPTIMAL.
def test_montage_budget_honesty() -> None:
    clips = tuple(ClipCandidate(f"c{i}", 1, 40) for i in range(8))
    s = optimize_rhythmic_montage(MontageRequest(clips=clips, max_states=10))
    assert s.optimal is False
    assert s.status.value == "FEASIBLE_NOT_PROVEN_OPTIMAL"


# 10. A forged verified=True in raw JSON with no evidence_ref is not trusted.
def test_forged_verified_flag_without_evidence_ref_is_rejected() -> None:
    payload = json.loads(json.dumps(_raw()))
    payload["shot_candidates"] = [
        {
            "id": "s1",
            "asset_ref": "asset://s1",
            "covers_beats": ["b1"],
            "frames": 8,
            "evidence_status": "TRUE",
            "evidence_ref": "",
            "source": {"path": "ep1/sc1", "line": 0, "note": ""},
        }
    ]
    payload["beats"] = [{"id": "b1", "episode_id": "ep1", "order_index": 0, "description": ""}]
    doc = loads_story(json.dumps(payload))
    result = plan_edit(
        PlanRequest(
            beats=("b1",),
            candidates=doc.shot_candidates,
            frame_budget=100,
            require_verified_evidence=True,
        )
    )
    assert result.status.value == "INFEASIBLE"


# 11. A beat no candidate covers is a typed refusal at the plan boundary.
def test_plan_rejects_missing_beat_coverage() -> None:
    result = plan_edit(PlanRequest(beats=("b_missing",), candidates=(), frame_budget=10))
    assert result.status.value == "INFEASIBLE"


# 12. Duplicate ids in a raw document are refused by the parser.
def test_duplicate_character_id_refused_at_parse() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import DuplicateIdentifierError

    payload = json.loads(json.dumps(_raw()))
    payload["characters"] = [{"id": "alice"}, {"id": "alice"}]
    with pytest.raises(DuplicateIdentifierError):
        parse_story(payload)


# 13. An unsupported schema version is refused.
def test_unsupported_schema_version_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import UnsupportedSchemaError

    payload = json.loads(json.dumps(_raw()))
    payload["schema_version"] = 999
    with pytest.raises(UnsupportedSchemaError):
        loads_story(json.dumps(payload))


# 14. Excessive input nesting is refused before it can exhaust memory.
def test_excessive_nesting_is_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import StoryValidationError

    deep = "[" * 5000 + "]" * 5000
    with pytest.raises((StoryValidationError, ValueError)):
        loads_story(deep)


# 15. A non-string Fountain input is refused, never silently parsed.
def test_malformed_fountain_is_refused() -> None:
    from nexus_ai_agent.creative.director import ScreenplayParseError

    with pytest.raises(ScreenplayParseError):
        parse_fountain(12345)  # type: ignore[arg-type]


# 16. A downstream conflict must not silently pass.
def test_downstream_edit_status_is_honest() -> None:
    doc = _doc(
        initial_facts=(_fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            _event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),
            # a second transfer from alice, who no longer holds the gun -> conflict
            _event("e2", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),
        ),
    )
    report = CausalEngine().analyze(doc)
    assert report.status is Status.FAIL


# 17. Zero-length interval is consistent (not a false negative cycle).
def test_zero_length_interval_is_consistent() -> None:
    result = check_consistency((_constraint("c1", "e1", "e2", 0, 0),), extra_nodes=("e1", "e2"))
    assert result.status is Status.PASS


# 18. A resource-limited STN never reports PASS.
def test_stn_resource_limit_never_reports_pass() -> None:
    cons = (_constraint("c1", "a", "b", 1, 2),)
    result = check_consistency(cons, extra_nodes=("a", "b"), max_nodes=0)
    assert result.status is Status.RESOURCE_LIMIT


# 19. The temporal network edge directions are the documented ones (upper / -lower).
def test_temporal_edge_directions_are_documented() -> None:
    net = TemporalNetwork()
    net.add_constraint(_constraint("c1", "a", "b", 3, 7))
    weights = {(a, b): w for a, b, w, _cid in net._edges}
    assert weights[("a", "b")] == 7  # upper
    assert weights[("b", "a")] == -3  # -lower


# 20. A jump cut below 30 degrees between two setups is a typed refusal.
def test_jump_cut_between_consecutive_setups_refused() -> None:
    from nexus_ai_agent.creative.director import CameraSetup, JumpCutError, ShotSize

    subject = ActorPlacement("a", 0.0, 0.0)
    prev = CameraSetup("s1", ShotSize.MEDIUM, "a", 1.0, 0.0, 50, 1)
    nxt = CameraSetup("s2", ShotSize.MEDIUM, "a", 1.0, 0.1, 50, 1)  # tiny angle, same focal
    with pytest.raises(JumpCutError):
        check_jump_cut(prev, nxt, subject)


# 21. The Fountain -> ATLAS bridge produces a consistent STN for a valid script.
def test_bridge_produces_consistent_stn() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(dfx.SIMPLE_SCRIPT), "ep1")
    extra = tuple(sorted({e.id for e in doc.events}))
    result = check_consistency(doc.temporal_constraints, extra_nodes=extra)
    assert result.status is Status.PASS
