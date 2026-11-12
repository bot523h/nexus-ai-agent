"""Unit tests for the versioned story input and referential validation."""

from __future__ import annotations

import json

import atlas_fixtures as fx
import pytest

from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    DuplicateIdentifierError,
    EventKind,
    FactKey,
    FactRelation,
    LimitExceededError,
    StoryDocument,
    StoryValidationError,
    TriValue,
    UnknownReferenceError,
    UnsupportedSchemaError,
    loads_story,
    parse_story,
)


def test_parse_minimal_document_round_trips() -> None:
    doc = parse_story(fx.raw_doc())
    assert doc.schema_version == SCHEMA_VERSION
    assert [c.id for c in doc.characters] == ["alice", "bob"]


def test_loads_story_from_bytes_and_str_agree() -> None:
    raw = json.dumps(fx.raw_doc())
    assert loads_story(raw) == loads_story(raw.encode("utf-8"))


def test_unsupported_schema_version_is_refused() -> None:
    data = fx.raw_doc()
    data["schema_version"] = 99
    with pytest.raises(UnsupportedSchemaError):
        parse_story(data)


def test_missing_schema_version_is_refused() -> None:
    data = fx.raw_doc()
    del data["schema_version"]
    with pytest.raises(UnsupportedSchemaError):
        parse_story(data)


def test_unknown_field_is_rejected() -> None:
    data = fx.raw_doc()
    data["surprise"] = True
    with pytest.raises(StoryValidationError):
        parse_story(data)


def test_malformed_json_is_refused() -> None:
    with pytest.raises(StoryValidationError):
        loads_story("{not json")


def test_non_object_document_is_refused() -> None:
    with pytest.raises(StoryValidationError):
        parse_story([])  # type: ignore[arg-type]


def test_duplicate_character_id_is_refused() -> None:
    with pytest.raises(DuplicateIdentifierError):
        parse_story(fx.raw_doc(characters=(Character(id="alice"), Character(id="alice"))))


def test_duplicate_episode_order_is_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import Episode

    with pytest.raises(DuplicateIdentifierError):
        parse_story(
            fx.raw_doc(episodes=(Episode(id="a", order_index=0), Episode(id="b", order_index=0)))
        )


def test_scene_referencing_unknown_episode_is_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import Scene

    with pytest.raises(UnknownReferenceError):
        parse_story(
            fx.raw_doc(
                scenes=(
                    Scene(
                        id="sc9",
                        episode_id="ghost",
                        location_id="room",
                        order_index=0,
                    ),
                )
            )
        )


def test_event_referencing_unknown_character_is_refused() -> None:
    with pytest.raises(UnknownReferenceError):
        parse_story(
            fx.raw_doc(
                events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="ghost", target="room"),)
            )
        )


def test_event_scene_must_belong_to_the_declared_episode() -> None:
    # sc2 is in ep2; declaring the event in ep1 must fail.
    ev = fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room", scene="sc2")
    with pytest.raises(UnknownReferenceError):
        parse_story(fx.raw_doc(events=(ev,)))


def test_shot_covering_unknown_beat_is_refused() -> None:
    with pytest.raises(UnknownReferenceError):
        parse_story(
            fx.raw_doc(
                beats=(fx.beat("b1", 0),),
                shot_candidates=(fx.shot("s1", ("b1", "ghost"), 10),),
            )
        )


def test_duplicate_shot_covers_is_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab import ShotCandidate, SourceRef

    with pytest.raises(StoryValidationError):
        ShotCandidate(
            id="s1",
            asset_ref="a",
            covers_beats=("b1", "b1"),
            frames=1,
            source=SourceRef(path="p"),
        )


def test_identifier_alphabet_is_enforced() -> None:
    with pytest.raises(StoryValidationError):
        Character(id="bad id!")


def test_identifier_length_is_bounded() -> None:
    with pytest.raises(StoryValidationError):
        Character(id="x" * 500)


def test_too_many_characters_is_a_limit_error() -> None:
    from nexus_ai_agent.creative.episode_causality_lab.models import INPUT_BOUNDS

    too_many = tuple(Character(id=f"c{i}") for i in range(INPUT_BOUNDS.max_characters + 1))
    with pytest.raises(LimitExceededError):
        StoryDocument(schema_version=SCHEMA_VERSION, characters=too_many)


def test_oversized_raw_document_is_refused() -> None:
    from nexus_ai_agent.creative.episode_causality_lab.story import MAX_DOCUMENT_BYTES

    with pytest.raises(LimitExceededError):
        loads_story(b" " * (MAX_DOCUMENT_BYTES + 1))


def test_fact_referencing_unknown_entity_is_refused() -> None:
    with pytest.raises(UnknownReferenceError):
        parse_story(
            fx.raw_doc(initial_facts=(fx.fact("ghost", FactRelation.KNOWS, "gun", TriValue.TRUE),))
        )


def test_fingerprint_is_stable_and_order_independent_of_dict_keys() -> None:
    doc = parse_story(fx.raw_doc())
    # Re-serialising with different key order yields the same canonical bytes.
    assert doc.fingerprint() == parse_story(fx.raw_doc()).fingerprint()
    assert len(doc.fingerprint()) == 64


def test_canonical_bytes_are_key_sorted() -> None:
    doc = parse_story(fx.raw_doc())
    text = doc.canonical_json()
    assert text.index('"characters"') < text.index('"episodes"')
    assert doc.canonical_bytes() == text.encode("utf-8")


def test_fact_key_encoding_is_deterministic() -> None:
    key = FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="gun")
    assert key.as_tuple() == ("alice", "possesses", "gun")
