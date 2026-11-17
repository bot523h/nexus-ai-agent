"""Tests for the Fountain -> ATLAS StoryDocument bridge."""

from __future__ import annotations

import director_fixtures as fx
import pytest

from nexus_ai_agent.creative.director import (
    InputBoundError,
    compile_fountain_to_story_document,
    parse_fountain,
    slugify_identifier,
)
from nexus_ai_agent.creative.episode_causality_lab import (
    CausalEngine,
    EventKind,
    check_consistency,
)


def test_compile_produces_versioned_document() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.SIMPLE_SCRIPT), "ep1")
    assert doc.schema_version == 1
    assert len(doc.scenes) == 2
    assert len(doc.episodes) == 1
    assert doc.episodes[0].id == "ep1"
    assert {c.id for c in doc.characters} == {"mara", "ivo"}


def test_events_carry_source_lines_and_kinds() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.SIMPLE_SCRIPT), "ep1")
    kinds = {e.kind for e in doc.events}
    assert EventKind.ENTER_LOCATION in kinds
    assert EventKind.TRANSFER_PROP in kinds
    assert all(e.source.line > 0 for e in doc.events)


def test_named_take_emits_set_state() -> None:
    script = "INT. ROOM - DAY\n\nMara takes the brass key from the table.\n"
    doc = compile_fountain_to_story_document(parse_fountain(script), "ep5")
    assert [e.kind for e in doc.events] == [EventKind.SET_STATE]
    assert doc.events[0].actor_id == "mara"


def test_pronoun_subject_stays_unknown() -> None:
    # "She takes the key" has no established antecedent -> no event emitted.
    script = "INT. ROOM - DAY\n\nShe takes the brass key.\n"
    doc = compile_fountain_to_story_document(parse_fountain(script), "ep6")
    assert doc.events == ()


def test_prop_transfer_is_extracted() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.SIMPLE_SCRIPT), "ep1")
    transfers = [e for e in doc.events if e.kind is EventKind.TRANSFER_PROP]
    assert len(transfers) == 1
    assert transfers[0].prop_id == "brass_key"
    assert transfers[0].actor_id == "mara"
    assert transfers[0].target_id == "ivo"


def test_temporal_anchors_prove_scene_order() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.SIMPLE_SCRIPT), "ep1")
    result = check_consistency(doc.temporal_constraints)
    assert result.consistent is True
    assert result.status.value == "PASS"


def test_document_analyzes_without_crash() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.SIMPLE_SCRIPT), "ep1")
    report = CausalEngine().analyze(doc)
    assert report.status.value in {"PASS", "NEEDS_REVIEW", "UNKNOWN"}


def test_beats_one_per_scene() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.MULTI_SCENE_SCRIPT), "ep2")
    assert len(doc.beats) == len(doc.scenes) == 3


def test_slugify_is_safe_and_deterministic() -> None:
    assert slugify_identifier("Mara Voss!") == "mara_voss"
    assert slugify_identifier("9 Lives") == "n_9_lives"
    assert slugify_identifier("   ") == "unnamed"


def test_slugify_rejects_overlong() -> None:
    with pytest.raises(InputBoundError):
        slugify_identifier("x" * 200)


def test_no_matching_pattern_leaves_no_event() -> None:
    script = "INT. ROOM - DAY\n\nAlice ponders the nature of time.\n"
    doc = compile_fountain_to_story_document(parse_fountain(script), "ep3")
    assert doc.events == ()


def test_three_actor_document() -> None:
    doc = compile_fountain_to_story_document(parse_fountain(fx.three_actor_script()), "ep4")
    assert {c.id for c in doc.characters} == {"alice", "bob", "carol"}
